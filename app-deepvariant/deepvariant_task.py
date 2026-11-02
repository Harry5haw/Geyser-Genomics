# app-deepvariant/deepvariant_task.py
"""DeepVariant variant-calling stage: an alternative to the bcftools 'variants' stage.

Runs in its own image (app-deepvariant/Dockerfile, built on google/deepvariant) with:
    /opt/geyser-venv/bin/python deepvariant_task.py <sample_id> <reference_name>

Same storage contract as the other stages (app/storage.py, via app/tasks.py):
    reads  alignments/{id}.cram, or a BAM if that is all there is (tasks.alignment_input),
           and reference/{reference_name} (+ optional .fai)
    writes variants_deepvariant/{id}.vcf.gz   (beside, not instead of, variants/{id}.vcf.gz)

DeepVariant runs three steps: make_examples (CPU), call_variants (the only GPU step) and
postprocess_variants (CPU). Each is timed separately and recorded as its own metric, so the
GPU's real contribution can be reported honestly.

Pangenome-aware mode (--pangenome <name>): runs Google's pangenome-aware DeepVariant
(run_pangenome_aware_deepvariant, in the pangenome_aware_deepvariant image) on the same
linear-reference alignment, adding a pangenome graph as a second input:
    reads  alignments/{id}.cram (or .bam), reference/{reference_name} and pangenome/{name} (a .gbz)
    writes variants_pangenome/{id}.vcf.gz
The graph must be built on the same reference as the alignment (e.g. HPRC v1.1 on GRCh38).

Environment:
    DEEPVARIANT_MODEL_TYPE   default WGS
    DEEPVARIANT_SHARDS       make_examples parallelism, default: CPUs in the container
    DEEPVARIANT_EXTRA_ARGS   extra run_deepvariant flags, e.g. --disable_small_model=true
    DEEPVARIANT_TIMEOUT_SECONDS       stop the wrapper after this long (default: no limit)
    DEEPVARIANT_IDLE_TIMEOUT_SECONDS  stop it after this long with no output (default: no limit)
    DEEPVARIANT_CHECKPOINT            0 turns off keeping make_examples output for a retry (default on)
    DEEPVARIANT_FATAL_GRACE_SECONDS   after a fatal error line (e.g. GPU out of memory), stop it once it has been
                                      silent this long without exiting (default 120; the wrapper can hang)
    RUN_DEEPVARIANT          path to run_deepvariant (default: the image's copy)
    RUN_PANGENOME_DEEPVARIANT  path to run_pangenome_aware_deepvariant (default: the image's copy)
"""

import argparse
import hashlib
import io
import json
import os
import queue
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time

import provenance
from tasks import (
    _log_summary,
    _run_stats_tool,
    ensure_reference_local,
    fetch_alignment,
    index_alignment,
    keys_for,
    metrics,
    parse_bcftools_stats,
    storage,
    timed_stage,
    upload_output,
    work_dir,
)

RUN_DEEPVARIANT = os.environ.get("RUN_DEEPVARIANT", "/opt/deepvariant/bin/run_deepvariant")
RUN_PANGENOME_DEEPVARIANT = os.environ.get(
    "RUN_PANGENOME_DEEPVARIANT", "/opt/deepvariant/bin/run_pangenome_aware_deepvariant"
)

# run_deepvariant prints this banner, then the command, before each step.
BANNER = "***** Running the command:*****"
# Match the binary path: later commands also mention 'make_examples' / 'call_variants' in file names.
# The pangenome-aware image may suffix the binary names (e.g. make_examples_pangenome_aware_dv).
STEP_RE = re.compile(r"/(make_examples|call_variants|postprocess_variants)\w*(?:\s|$)")
STEP_SUFFIXES = {
    "make_examples": "MakeExamples",
    "call_variants": "CallVariants",
    "postprocess_variants": "PostprocessVariants",
}
STAGE_NAME = {False: "DeepVariant", True: "PangenomeDeepVariant"}
# The caller names a run is started with (the state machine's "caller", run_local.py --caller).
CALLER_NAME = {False: "deepvariant", True: "pangenome"}
STEP_METRICS = {step: f"{STAGE_NAME[False]}.{suffix}" for step, suffix in STEP_SUFFIXES.items()}
PANGENOME_STEP_METRICS = {step: f"{STAGE_NAME[True]}.{suffix}" for step, suffix in STEP_SUFFIXES.items()}


def deepvariant_keys(sample_id: str, pangenome: str = None) -> dict:
    # "bam" is kept for callers that list a sample's keys; the stage itself reads whatever
    # tasks.alignment_input() resolves (the align stage's CRAM, else a BAM).
    sample_keys = keys_for(sample_id)
    keys = {"bam": sample_keys["bam"]}
    if pangenome:
        keys["vcf"] = f"variants_pangenome/{sample_id}.vcf.gz"
        keys["pangenome"] = f"pangenome/{pangenome}"
    else:
        keys["vcf"] = f"variants_deepvariant/{sample_id}.vcf.gz"
    # This caller's PASS-call stats, always; and the sample's own stats file when this is the run's caller.
    keys["caller_stats"] = f"stats/{sample_id}/bcftools.stats.{CALLER_NAME[bool(pangenome)]}.txt"
    keys["bcftools_stats"] = sample_keys["bcftools_stats"]
    return keys


def pass_stats_command(ref: str, vcf: str, out: str) -> str:
    """bcftools stats over the PASS calls only.

    DeepVariant's VCF also keeps the candidates it rejected (FILTER=RefCall), mostly sequencing errors.
    Counted with the calls they would swamp the numbers: on HG003 chr20 Ti/Tv is 0.86 over all records
    and 1.96 over PASS calls, and the quality gate would fail every good run (gate/README.md).
    """
    q = shlex.quote
    return f"bcftools stats -f PASS -F {q(ref)} -s - {q(vcf)} > {q(out)}"


def pipeline_caller() -> str:
    """The caller this run was started with (PIPELINE_CALLER, set by the state machine and run_local.py), or ''."""
    return os.environ.get("PIPELINE_CALLER", "").strip().lower()


# Written as the sample's stats when the run's caller couldn't produce any: an earlier run's file would
# otherwise stay in place (no s3:DeleteObject) and, inherited through the run record, pass for this run's.
NO_STATS_NOTE = "# No bcftools stats: {caller} was this run's caller and its stats step failed (see the job log).\n"


def caller_stats(ref: str, vcf: str, wd: str, keys: dict, pangenome=None) -> list:
    """(local file, key) pairs to upload: PASS-call stats as stats/{id}/bcftools.stats.<caller>.txt, and,
    when this is the run's caller (pipeline_caller), also as the sample's stats/{id}/bcftools.stats.txt,
    which the quality gate and the dashboard's quality widgets read. Otherwise the bcftools stage owns
    that file, so a benchmark running both callers never overwrites it. Best effort, like all stats:
    if they fail for the run's caller, the sample's file becomes NO_STATS_NOTE, which the gate rejects
    as not bcftools output instead of accepting an earlier run's numbers."""
    caller = CALLER_NAME[bool(pangenome)]
    primary = pipeline_caller() == caller
    provenance.note_tool_as("bcftools (DeepVariant image)", "bcftools")
    out = os.path.join(wd, os.path.basename(keys["caller_stats"]))
    if not _run_stats_tool(f"bcftools stats ({caller}, PASS calls)", pass_stats_command(ref, vcf, out)):
        if not primary:
            return []
        note = os.path.join(wd, "bcftools.stats.none.txt")
        with open(note, "w") as fh:
            fh.write(NO_STATS_NOTE.format(caller=caller))
        return [(note, keys["bcftools_stats"])]
    # "bcftools stats: ..." is the line the quality widgets parse (scripts/monitoring.py QUALITY_PARSE).
    _log_summary("bcftools stats" if primary else f"bcftools stats {caller}", parse_bcftools_stats, out)
    return [(out, keys["caller_stats"])] + ([(out, keys["bcftools_stats"])] if primary else [])


def build_run_deepvariant_cmd(
    ref, bam, vcf, intermediate_dir, sample_name, num_shards, model_type="WGS", extra_args="", pangenome=None
):
    """run_deepvariant, or run_pangenome_aware_deepvariant when pangenome (a local .gbz path) is given."""
    cmd = [
        RUN_PANGENOME_DEEPVARIANT if pangenome else RUN_DEEPVARIANT,
        f"--model_type={model_type}",
        f"--ref={ref}",
        f"--reads={bam}",
    ]
    if pangenome:
        cmd.append(f"--pangenome={pangenome}")
    return (
        cmd
        + [
            f"--output_vcf={vcf}",
            f"--intermediate_results_dir={intermediate_dir}",
            # run_pangenome_aware_deepvariant (1.9.0) has no --sample_name: it names the reads' sample separately
            # from the pangenome panel's (--sample_name_pangenome, left to its default).
            f"--sample_name_reads={sample_name}" if pangenome else f"--sample_name={sample_name}",
            f"--num_shards={num_shards}",
        ]
        + shlex.split(extra_args or "")
    )


def parse_step_timings(events, end_time):
    """Wall-clock seconds per step from timestamped output lines.

    events: [(timestamp, line), ...] in order. A step starts at its banner and ends at the
    next banner (or end_time). Returns {step_name: seconds} in run order.
    """
    timings = {}
    current, started, banner_at = None, None, None
    for ts, line in events:
        if BANNER in line:
            if current is not None:
                timings[current] = timings.get(current, 0.0) + (ts - started)
                current = None
            banner_at = ts
            continue
        if banner_at is not None:
            match = STEP_RE.search(line)
            if match:
                current, started, banner_at = match.group(1), banner_at, None
    if current is not None:
        timings[current] = timings.get(current, 0.0) + (end_time - started)
    return timings


EXAMPLES_RE = re.compile(r"^\s*(\d+) examples written")
SMALL_MODEL_RE = re.compile(r"^\s*(\d+) small model examples called")


def parse_example_counts(events):
    """Totals from make_examples' per-shard summaries: how much work reached the GPU.

    'examples' go to call_variants (the GPU model); 'small_model_calls' were already called on the
    CPU inside make_examples, so call_variants never sees them.
    """
    examples = small = 0
    for _, line in events:
        m = EXAMPLES_RE.match(line)
        if m:
            examples += int(m.group(1))
            continue
        m = SMALL_MODEL_RE.match(line)
        if m:
            small += int(m.group(1))
    return {"examples": examples, "small_model_calls": small}


# A fatal error inside the wrapper's steps. Google's wrappers can hang after one instead of exiting (seen 1 Nov 2026:
# call_variants ran out of GPU memory at batch 1024 and run_pangenome_aware_deepvariant never returned). TensorFlow's
# "Allocator (GPU_0_bfc) ran out of memory trying to allocate ..." lines are NOT fatal: they are warnings that also
# appear in successful runs, so they are deliberately not matched.
FATAL_RE = re.compile(r"ResourceExhaustedError|RESOURCE_EXHAUSTED")


class WrapperStopped(RuntimeError):
    """The wrapper was stopped by run_streaming: overall timeout, no output for too long, or a fatal error after
    which it went quiet without exiting. events: the output it printed before that, as run_streaming returns it."""

    def __init__(self, message, events=None):
        super().__init__(message)
        self.events = events or []


LIMIT_VARS = {
    "timeout": "DEEPVARIANT_TIMEOUT_SECONDS",
    "idle": "DEEPVARIANT_IDLE_TIMEOUT_SECONDS",
    "fatal_grace": "DEEPVARIANT_FATAL_GRACE_SECONDS",
}


class WrapperLimits:
    """When run_streaming gives up on the wrapper. Seconds; 0 or None turns a limit off.

    timeout       whole run (DEEPVARIANT_TIMEOUT_SECONDS). Default off: local whole-genome runs take hours, and
                  the cloud job sets it below its Batch attempt timeout.
    idle          no output at all (DEEPVARIANT_IDLE_TIMEOUT_SECONDS). Default off: loading a whole-genome graph
                  or a long sort can be silent for a long time; the cloud job sets it.
    fatal_grace   after a FATAL_RE line, how long the wrapper may stay silent before it counts as hung
                  (DEEPVARIANT_FATAL_GRACE_SECONDS, default 120). Still printing means still alive.
    """

    def __init__(self, timeout=None, idle=None, fatal_grace=120):
        self.timeout, self.idle, self.fatal_grace = timeout or None, idle or None, fatal_grace or None

    @classmethod
    def from_env(cls, env=None):
        """Limits from the environment. ValueError names a variable that is not a non-negative number of seconds."""
        env = os.environ if env is None else env
        values = {"timeout": None, "idle": None, "fatal_grace": 120}
        for field, name in LIMIT_VARS.items():
            raw = (env.get(name) or "").strip()
            if not raw:
                continue
            try:
                seconds = float(raw)
            except ValueError:
                raise ValueError(f"{name}={raw!r}: expected a number of seconds (0 = no limit)") from None
            if seconds < 0:
                raise ValueError(f"{name}={raw!r}: must not be negative (0 = no limit)")
            values[field] = seconds
        return cls(**values)


def stop_reason(limits, now, started, last_output, fatal_at):
    """Why the wrapper should be stopped now, or None."""
    if limits.timeout and now - started > limits.timeout:
        return f"did not finish within {limits.timeout:.0f} s (DEEPVARIANT_TIMEOUT_SECONDS)"
    if limits.idle and now - last_output > limits.idle:
        return f"printed nothing for {limits.idle:.0f} s (DEEPVARIANT_IDLE_TIMEOUT_SECONDS): it looks hung"
    if fatal_at is not None and limits.fatal_grace and now - last_output > limits.fatal_grace:
        return (
            f"reported a fatal error ({FATAL_RE.pattern}; usually out of GPU memory: lower call_variants batch_size) "
            f"and then printed nothing for {limits.fatal_grace:.0f} s without exiting"
        )
    return None


def signal_group(proc, sig_name):
    """Send a signal to the wrapper's whole process group (POSIX), or to the wrapper itself elsewhere.
    False once there is nothing left to signal."""
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, getattr(signal, sig_name, 9 if sig_name == "SIGKILL" else 15))
        elif proc.poll() is None:
            if sig_name == "SIGTERM":
                proc.terminate()
            else:
                proc.kill()
        else:
            return False
    except ProcessLookupError:
        return False
    return True


def kill_process_group(proc, wait=10):
    """Stop the wrapper and every step it started (GNU parallel workers, call_variants): TERM to the group, then
    KILL to the group. The group is signalled even if the wrapper itself has already exited: a stuck step can
    outlive it and keep holding the GPU."""
    if not signal_group(proc, "SIGTERM"):
        return
    try:
        proc.wait(timeout=wait)
    except subprocess.TimeoutExpired:
        pass
    signal_group(proc, "SIGKILL")


def run_streaming(cmd, limits=None, clock=time.monotonic, poll=1.0, drain=5.0, stop_wait=30.0):
    """Run cmd, echo its output live, and return (returncode, [(timestamp, line)], end_time).

    The wrapper runs in its own process group, so it can be stopped with everything it started. If it runs past
    limits (WrapperLimits.from_env() by default), the group is killed and WrapperStopped is raised with the
    reason. Once the wrapper has exited, output is read for at most `drain` more seconds: a leftover process
    holding the pipe must not turn a finished run into a hang; any such leftovers are then stopped too.
    """
    limits = limits or WrapperLimits.from_env()
    print("$ " + " ".join(shlex.quote(c) for c in cmd), flush=True)
    events = []
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        encoding="utf-8",
        errors="replace",  # a stray byte in tool logs must not fail the stage
        start_new_session=True,
    )
    lines = queue.Queue()

    def read():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=read, daemon=True).start()
    started = last_output = clock()
    fatal_at, reason, exited_at = None, None, None
    while True:
        try:
            line = lines.get(timeout=poll)
        except queue.Empty:
            line = ""
        if line is None:
            break
        now = clock()
        if line:
            events.append((time.time(), line.rstrip("\n")))
            print(line, end="", flush=True)
            last_output = now
            if fatal_at is None and FATAL_RE.search(line):
                fatal_at = now
                if limits.fatal_grace:
                    print(
                        f"[geyser] fatal error reported; stopping if it then stays silent for "
                        f"{limits.fatal_grace:.0f} s",
                        flush=True,
                    )
        if proc.poll() is not None:
            exited_at = exited_at if exited_at is not None else now
            if not line and now - exited_at > drain:
                break  # exited; a leftover process is keeping the pipe open
            continue
        reason = stop_reason(limits, now, started, last_output, fatal_at)
        if reason:
            print(f"[geyser] stopping {os.path.basename(cmd[0])}: it {reason}", flush=True)
            kill_process_group(proc)
            break
    try:
        code = proc.wait(timeout=stop_wait)
    except subprocess.TimeoutExpired:
        raise WrapperStopped(
            f"{os.path.basename(cmd[0])} {reason or 'did not exit'}, and could not be stopped within {stop_wait:.0f} s",
            events,
        ) from None
    if reason:
        raise WrapperStopped(f"{os.path.basename(cmd[0])} {reason} (exit code {code} after being stopped)", events)
    # Steps left behind by a finished run (they may hold the GPU even after the pipe closes): stop the group.
    # A no-op once the group is gone.
    signal_group(proc, "SIGKILL")
    return code, events, time.time()


def index_bam(path):
    """Kept as this module's hook (tests replace it); the command itself is tasks.index_alignment."""
    index_alignment(path)


def gpu_name():
    """The GPU visible to this container, or None. With docker --gpus, nvidia-smi is mounted in."""
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    names = out.stdout.strip().splitlines() if out.returncode == 0 else []
    return names[0].strip() if names else None


# ---- resuming after make_examples (#51) ----
# Neither wrapper can skip a step, and make_examples is most of the run (936 of 1,241 s for pangenome on HG003
# chr20). So when the wrapper fails or is stopped after make_examples finished, the intermediate files it made and
# the remaining step commands, exactly as the wrapper printed them, are kept under CHECKPOINT_PREFIX. The next run of
# the same stage on the same inputs and settings replays only call_variants and postprocess_variants. The prefix is
# under intermediate/, which the data lake's lifecycle rule expires. The cloud role cannot delete objects, so a used
# (or broken, or superseded) checkpoint is retired by overwriting its manifest.
CHECKPOINT_PREFIX = "intermediate/deepvariant-checkpoints/{sample_id}/{caller}/"
MANIFEST = "manifest.json"
CHECKPOINT_VERSION = 1
# Every step a wrapper can print, in run order (the pangenome wrapper loads the graph first): STEP_RE's steps plus
# the graph loader, also matching a step binary followed by a quote.
STEP_NAME_RE = re.compile(
    r"/(load_gbz_into_shared_memory|make_examples|call_variants|postprocess_variants)\w*(?:\s|\"|$)"
)
RESUME_FROM = "call_variants"
# call_variants' own output is redone on a retry; every other intermediate file (examples, small-model calls,
# gVCF records, ...) comes from make_examples and is kept.
REDONE_PREFIX = "call_variants_output"
# How much of each end of an input file goes into its fingerprint (with its size): a re-aligned CRAM or a patched
# reference differs in its header or tail, without hashing a whole 30 GB alignment.
SAMPLE_BYTES = 64 * 1024 * 1024


def checkpoints_enabled():
    """DEEPVARIANT_CHECKPOINT=0 turns checkpoints off (default on)."""
    return os.environ.get("DEEPVARIANT_CHECKPOINT", "1").strip() not in ("0", "false", "no", "off")


def checkpoint_prefix(sample_id, pangenome=None):
    return CHECKPOINT_PREFIX.format(sample_id=sample_id, caller=CALLER_NAME[bool(pangenome)])


def step_commands(events):
    """[(step, command)] the wrapper started, in order: after each banner, the first line naming a step, without
    a leading 'time ' (a stray log line between banner and command is skipped, as in parse_step_timings)."""
    steps, after_banner = [], False
    for _, line in events:
        if BANNER in line:
            after_banner = True
            continue
        if after_banner:
            command = line.strip()
            match = STEP_NAME_RE.search(command)
            if match:
                steps.append((match.group(1), command[5:] if command.startswith("time ") else command))
                after_banner = False
    return steps


def planned_steps(cmd):
    """Every step the wrapper will run for cmd, from `<cmd> --dry_run=true` (both wrappers print the commands in
    the same banner + 'time ...' form as a real run, without running them). [] if that fails: then only a run that
    already printed postprocess_variants can be checkpointed."""
    try:
        out = subprocess.run(list(cmd) + ["--dry_run=true"], capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError):
        return []
    lines = (out.stdout + "\n" + out.stderr).splitlines()
    return step_commands([(0.0, line) for line in lines]) if out.returncode == 0 else []


def remaining_steps(started, plan):
    """The steps a retry must run, from RESUME_FROM on, or None if they are not known.

    The plan (planned_steps) is used when the steps that did run are exactly its first entries; otherwise only the
    run's own output, and only if it already shows the last step (a run stopped in call_variants never prints
    postprocess_variants, so its own output alone is not enough)."""
    names = [n for n, _ in plan]
    if RESUME_FROM in names and started == plan[: len(started)]:
        return plan[names.index(RESUME_FROM) :]
    live = [n for n, _ in started]
    if RESUME_FROM in live and live[-1] == "postprocess_variants":
        return started[live.index(RESUME_FROM) :]
    return None


def make_examples_finished(steps):
    """True if the wrapper got past make_examples: it starts the next step only after one has succeeded."""
    names = [name for name, _ in steps]
    return "make_examples" in names and RESUME_FROM in names[names.index("make_examples") :]


def wrapper_version(wrapper):
    """'1.10.0' from `<wrapper> --version` (both wrappers print 'DeepVariant version 1.10.0' in 1.10.0), or None if
    it cannot be told: then no checkpoint is saved or used."""
    try:
        out = subprocess.run([wrapper, "--version"], capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"version (\S+)", out.stdout + out.stderr)
    return match.group(1) if match else None


def file_identity(path):
    """[name, size, sha256 of the first and last SAMPLE_BYTES], or None for no file."""
    if not path or not os.path.exists(path):
        return None
    size = os.path.getsize(path)
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        digest.update(fh.read(SAMPLE_BYTES))
        if size > SAMPLE_BYTES:
            fh.seek(max(SAMPLE_BYTES, size - SAMPLE_BYTES))
            digest.update(fh.read(SAMPLE_BYTES))
    return [os.path.basename(path), size, digest.hexdigest()]


def run_fingerprint(wrapper, alignment, ref, graph, shards, model_type, extra_args):
    """What a checkpoint is only valid for: the same DeepVariant, inputs and settings. None if the version is
    unknown."""
    version = wrapper_version(wrapper)
    if version is None:
        return None
    return {
        "wrapper": os.path.basename(wrapper),
        "version": version,
        "alignment": file_identity(alignment),
        "reference": file_identity(ref),
        "graph": file_identity(graph),
        "shards": shards,
        "model_type": model_type,
        "extra_args": extra_args or "",
    }


def write_manifest(prefix, manifest):
    storage().upload_stream(io.BytesIO(json.dumps(manifest, indent=2).encode("utf-8")), prefix + MANIFEST)


def read_manifest(prefix):
    if not storage().exists(prefix + MANIFEST):
        return None
    with storage().open_read(prefix + MANIFEST) as fh:
        return json.load(fh)


def retire(prefix, why):
    """Overwrite a checkpoint's manifest so no run uses it (the cloud role cannot delete objects). Best effort."""
    try:
        if storage().exists(prefix + MANIFEST):
            write_manifest(prefix, {"checkpoint_version": CHECKPOINT_VERSION, "consumed": True, "retired": why})
    except Exception as err:  # noqa: BLE001 - best effort
        print(f"[geyser] WARNING: could not retire the checkpoint: {err}", flush=True)


def save_checkpoint(prefix, events, wd, intermediate, fingerprint, plan=()):
    """Keep make_examples' intermediate files and the remaining steps, if the wrapper got past make_examples.
    Best effort: never hides the original failure. Returns True if a checkpoint was saved."""
    steps = step_commands(events)
    if not make_examples_finished(steps):
        print("[geyser] no checkpoint: the run failed before make_examples finished", flush=True)
        return False
    if fingerprint is None:
        print("[geyser] no checkpoint: could not tell the DeepVariant version", flush=True)
        return False
    remaining = remaining_steps(steps, list(plan))
    if remaining is None:
        print("[geyser] no checkpoint: the remaining step commands are not known (no --dry_run plan)", flush=True)
        return False
    try:
        retire(prefix, "being replaced")  # first: an older manifest must never describe the files about to be written
        files = sorted(
            f
            for f in os.listdir(intermediate)
            if os.path.isfile(os.path.join(intermediate, f)) and not f.startswith(REDONE_PREFIX)
        )
        size = sum(os.path.getsize(os.path.join(intermediate, f)) for f in files)
        started = time.monotonic()
        for name in files:
            storage().upload(os.path.join(intermediate, name), prefix + name)
        manifest = {
            "checkpoint_version": CHECKPOINT_VERSION,
            "fingerprint": fingerprint,
            "work_dir": wd,
            "steps": remaining,
            "files": files,
            "consumed": False,
        }
        write_manifest(prefix, manifest)  # last: a manifest means its files are all there
        print(
            f"[geyser] saved a checkpoint after make_examples: {len(files)} files ({size / 1e6:,.0f} MB, "
            f"{time.monotonic() - started:.0f} s) under {storage().describe(prefix)}; the next run on the same inputs "
            f"resumes at {RESUME_FROM}",
            flush=True,
        )
        return True
    except Exception as err:  # noqa: BLE001 - a checkpoint must never replace the real error
        print(f"[geyser] WARNING: could not save a checkpoint: {err}", flush=True)
        return False


def load_checkpoint(prefix, wd, intermediate, fingerprint):
    """The remaining [(step, command)] with paths moved to this run's work dir, after downloading the kept files,
    or None to run in full: no checkpoint, a used one, one made for other inputs/settings, or a broken one (which
    is then retired, so it is not tried again)."""
    try:
        manifest = read_manifest(prefix)
        if not manifest or manifest.get("consumed") or manifest.get("checkpoint_version") != CHECKPOINT_VERSION:
            return None
        if fingerprint is None or manifest.get("fingerprint") != fingerprint:
            old = manifest.get("fingerprint") or {}
            changed = sorted(k for k in fingerprint or {} if old.get(k) != fingerprint[k]) or ["version unknown"]
            print(f"[geyser] checkpoint under {storage().describe(prefix)} not used: {', '.join(changed)} differ")
            return None
        if not manifest["steps"] or manifest["steps"][-1][0] != "postprocess_variants":
            raise ValueError("its steps do not end with postprocess_variants, so it could never write the VCF")
        for name in manifest["files"]:
            storage().download(prefix + name, os.path.join(intermediate, name))
        old_wd = manifest["work_dir"]
        steps = [(name, command.replace(old_wd, wd)) for name, command in manifest["steps"]]
    except Exception as err:  # noqa: BLE001 - a broken checkpoint must not block the stage
        print(f"[geyser] checkpoint under {storage().describe(prefix)} could not be used ({err}); running in full")
        for name in os.listdir(intermediate):
            os.remove(os.path.join(intermediate, name))
        retire(prefix, f"unusable: {err}")
        return None
    print(
        f"[geyser] resuming from the checkpoint under {storage().describe(prefix)}: make_examples skipped, "
        f"running {', '.join(n for n, _ in steps)}",
        flush=True,
    )
    return steps


def run_steps(steps, limits):
    """Run the remaining steps one by one under the stop limits, sharing one overall time budget between them.
    Returns what run_streaming returns, with a banner and the command before each step so parse_step_timings and
    step_commands see them."""
    events, code, started = [], 0, time.monotonic()
    for _, command in steps:
        step_limits = limits
        if limits.timeout:
            left = limits.timeout - (time.monotonic() - started)
            if left <= 0:
                raise WrapperStopped(
                    f"resumed steps did not finish within {limits.timeout:.0f} s (DEEPVARIANT_TIMEOUT_SECONDS)",
                    events,
                )
            step_limits = WrapperLimits(timeout=left, idle=limits.idle, fatal_grace=limits.fatal_grace)
        now = time.time()
        events += [(now, BANNER), (now, "time " + command)]
        try:
            code, step_events, _ = run_streaming(["bash", "-o", "pipefail", "-c", command], limits=step_limits)
        except WrapperStopped as err:
            raise WrapperStopped(str(err), events + err.events) from None
        events += step_events
        if code != 0:
            break
    return code, events, time.time()


def _call_variants(sample_id, reference_name, pangenome=None):
    """Shared body of the linear and pangenome-aware stages. pangenome: graph name under pangenome/."""
    label = STAGE_NAME[bool(pangenome)]
    step_metrics = PANGENOME_STEP_METRICS if pangenome else STEP_METRICS
    keys = deepvariant_keys(sample_id, pangenome)
    wd = work_dir(sample_id)
    try:
        limits = WrapperLimits.from_env()  # a bad value fails here, before any download
        vcf = os.path.join(wd, f"{sample_id}.{'pangenome' if pangenome else 'deepvariant'}.vcf.gz")
        intermediate = os.path.join(wd, "intermediate")
        os.makedirs(intermediate)

        # The CRAM (or an imported BAM: DeepVariant reads both), indexed here for random access.
        bam = fetch_alignment(sample_id, wd, need_index=True, indexer=index_bam)
        ref = ensure_reference_local(reference_name, wd, need_bwa_index=False)
        graph = None
        if pangenome:
            graph = os.path.join(wd, os.path.basename(pangenome))
            print(f"Fetching pangenome graph {storage().describe(keys['pangenome'])}", flush=True)
            storage().download(keys["pangenome"], graph)

        shards = int(os.environ.get("DEEPVARIANT_SHARDS") or os.cpu_count() or 1)
        gpu = gpu_name()
        model_type = os.environ.get("DEEPVARIANT_MODEL_TYPE", "WGS")
        extra_args = os.environ.get("DEEPVARIANT_EXTRA_ARGS", "")
        cmd = build_run_deepvariant_cmd(
            ref,
            bam,
            vcf,
            intermediate,
            sample_id,
            shards,
            model_type=model_type,
            extra_args=extra_args,
            pangenome=graph,
        )
        prefix = checkpoint_prefix(sample_id, pangenome)
        # Before the run: a timed-out run must not spend its last minutes asking the wrapper for its version.
        fingerprint = (
            run_fingerprint(cmd[0], bam, ref, graph, shards, model_type, extra_args) if checkpoints_enabled() else None
        )
        resumed = load_checkpoint(prefix, wd, intermediate, fingerprint) if checkpoints_enabled() else None
        # The wrapper's own plan of all its steps, so a run stopped in call_variants can still be resumed
        # (its output never shows the postprocess_variants command).
        plan = planned_steps(cmd) if checkpoints_enabled() and not resumed else []
        try:
            code, events, end_time = run_steps(resumed, limits) if resumed else run_streaming(cmd, limits=limits)
        except WrapperStopped as err:
            if checkpoints_enabled() and not resumed:
                save_checkpoint(prefix, err.events, wd, intermediate, fingerprint, plan)
            raise
        if code != 0:
            if checkpoints_enabled() and not resumed:
                save_checkpoint(prefix, events, wd, intermediate, fingerprint, plan)
            raise RuntimeError(f"{'resumed steps' if resumed else os.path.basename(cmd[0])} exited with code {code}")

        timings = parse_step_timings(events, end_time)
        print(f"\n{label} step timings for {sample_id} (shards={shards}, gpu={gpu or 'none'}):")
        for step, seconds in timings.items():
            print(f"  {step:<22}{seconds:9.1f}s")
            try:
                metrics().record(step_metrics[step], sample_id, "Success", seconds)
            except Exception as err:  # noqa: BLE001 - metrics are best effort
                print(f"WARNING: could not record metric: {err}", flush=True)
        counts = parse_example_counts(events)
        print(
            f"  examples sent to call_variants: {counts['examples']}; "
            f"called by the small model in make_examples (CPU): {counts['small_model_calls']}"
        )
        missing = [s for s in step_metrics if s not in timings and not (resumed and s == "make_examples")]
        if missing:
            print(f"WARNING: no timing found for {missing}; {os.path.basename(cmd[0])} output format may have changed")

        if not os.path.isfile(vcf) or os.path.getsize(vcf) == 0:
            raise RuntimeError(f"{label} produced no VCF at {vcf}")
        for local, key in caller_stats(ref, vcf, wd, keys, pangenome):  # before the VCF, which marks the stage done
            upload_output(local, key)
        upload_output(vcf, keys["vcf"])
        print(f"Uploaded {storage().describe(keys['vcf'])}")
        if resumed:
            retire(prefix, "used")
    finally:
        shutil.rmtree(wd, ignore_errors=True)


@timed_stage(STAGE_NAME[False])
def deepvariant_task(sample_id, reference_name):
    _call_variants(sample_id, reference_name)


@timed_stage(STAGE_NAME[True])
def pangenome_deepvariant_task(sample_id, reference_name, pangenome):
    _call_variants(sample_id, reference_name, pangenome)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Call variants with DeepVariant (one pipeline stage).")
    parser.add_argument("srr_id", help="Sample ID, e.g. SRR062634")
    parser.add_argument("reference_name", help="Reference FASTA name under reference/, e.g. chr20.fa")
    parser.add_argument(
        "--pangenome",
        metavar="GBZ",
        help="pangenome graph name under pangenome/ (e.g. hprc-v1.1-mc-grch38.gbz): "
        "run pangenome-aware DeepVariant and write variants_pangenome/<id>.vcf.gz",
    )
    args = parser.parse_args(argv)
    if args.pangenome:
        pangenome_deepvariant_task(args.srr_id, args.reference_name, args.pangenome)
        task = "pangenome"
    else:
        deepvariant_task(args.srr_id, args.reference_name)
        task = "deepvariant"
    print(f"Task '{task}' completed for sample '{args.srr_id}'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
