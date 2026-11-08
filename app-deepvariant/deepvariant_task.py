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

Split mode (v1.5: make_examples on CPU jobs, the rest on the GPU job; contract in docs/design/deepvariant-split.md):
    deepvariant_task.py <id> <ref> --make-examples-part P --parts J   (CPU image, J jobs in parallel)
        runs make_examples for its share of the DEEPVARIANT_SHARDS tasks and uploads the examples to
        intermediate/dv/{id}/{RUN_NAME}/shard-P/ (manifest.json last)
    deepvariant_task.py <id> <ref> --from-examples                     (GPU image, after every part)
        checks the J manifests, downloads the examples, runs call_variants and postprocess_variants, and
        writes the same outputs as the single job (variants_deepvariant/{id}.vcf.gz, stats, the GPU figures)

Environment:
    DEEPVARIANT_MODEL_TYPE   default WGS
    DEEPVARIANT_SHARDS       make_examples parallelism, default: CPUs in the container (split mode: tasks in all,
                             default parts x CPUs; the GPU job takes it from the parts' manifests)
    DEEPVARIANT_EXTRA_ARGS   extra run_deepvariant flags, e.g. --disable_small_model=true
    DEEPVARIANT_TIMEOUT_SECONDS       stop the wrapper after this long (default: no limit)
    DEEPVARIANT_IDLE_TIMEOUT_SECONDS  stop it after this long with no output (default: no limit)
    DEEPVARIANT_CHECKPOINT            0 turns off keeping make_examples output for a retry (default on)
    DEEPVARIANT_FATAL_GRACE_SECONDS   after a fatal error line (e.g. GPU out of memory), stop it once it has been
                                      silent this long without exiting (default 120; the wrapper can hang)
    DEEPVARIANT_GPU_SAMPLE_SECONDS    seconds between GPU utilisation/memory samples (default 2; 0 = off)
    RUN_DEEPVARIANT          path to run_deepvariant (default: the image's copy)
    RUN_PANGENOME_DEEPVARIANT  path to run_pangenome_aware_deepvariant (default: the image's copy)
"""

import argparse
import hashlib
import io
import json
import math
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
    _safe_name,
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


def build_vcf_index(vcf: str, tbi: str) -> None:
    """bcftools index -t: the tabix index of a bgzipped VCF."""
    subprocess.run(["bcftools", "index", "-t", "-f", "-o", tbi, vcf], check=True)


def upload_vcf_with_index(vcf: str, key: str) -> None:
    """Upload the VCF's tabix index (key + ".tbi") and then the VCF, which marks the stage done.

    postprocess_variants writes a .tbi beside a .vcf.gz; if it's missing it is built with bcftools. The index is a
    convenience for readers (hap.py, IGV, region queries), so failing to build it warns rather than failing the stage.
    """
    tbi = vcf + ".tbi"
    if not os.path.isfile(tbi) or os.path.getsize(tbi) == 0:
        try:
            build_vcf_index(vcf, tbi)
        except (OSError, subprocess.CalledProcessError) as err:
            print(
                f"WARNING: could not index {os.path.basename(vcf)} ({err}); uploading the VCF without a .tbi",
                flush=True,
            )
            tbi = None
    if tbi:
        upload_output(tbi, key + ".tbi")
    upload_output(vcf, key)


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


def parse_step_windows(events, end_time):
    """When each step ran, from timestamped output lines: [(step_name, start, end), ...] in run order.

    events: [(timestamp, line), ...] in order. A step starts at its banner and ends at the
    next banner (or end_time).
    """
    windows = []
    current, started, banner_at = None, None, None
    for ts, line in events:
        if BANNER in line:
            if current is not None:
                windows.append((current, started, ts))
                current = None
            banner_at = ts
            continue
        if banner_at is not None:
            match = STEP_RE.search(line)
            if match:
                current, started, banner_at = match.group(1), banner_at, None
    if current is not None:
        windows.append((current, started, end_time))
    return windows


def parse_step_timings(events, end_time):
    """Wall-clock seconds per step ({step_name: seconds} in run order; see parse_step_windows)."""
    timings = {}
    for step, start, end in parse_step_windows(events, end_time):
        timings[step] = timings.get(step, 0.0) + (end - start)
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


# ---- GPU utilisation and memory (#139) ----
# While the wrapper runs, nvidia-smi samples the GPU every few seconds. The run record's part gets the GPU model and
# the mean/peak utilisation and peak memory, for the whole run and per step (only call_variants uses the GPU, so the
# other steps show how long it sat idle); each step's metric (DeepVariant.CallVariants ...) carries the same figures.
# - utilization.gpu is the share of the sample period in which any kernel ran, not how much of the GPU it used.
# - memory.used is the whole device's: TensorFlow reserves (nearly) all free memory at start-up, so the peak shows
#   what was reserved, not what the model needed; on a desktop it also includes the display's own use.
# Without nvidia-smi (CPU image, CI, no --gpus) nothing is sampled or recorded; measuring never fails the stage.
GPU_SAMPLE_VAR = "DEEPVARIANT_GPU_SAMPLE_SECONDS"
GPU_SAMPLE_DEFAULT = 2.0
GPU_QUERY = "index,utilization.gpu,memory.used,memory.total"


def gpu_sample_interval(env=None):
    """Seconds between GPU samples: DEEPVARIANT_GPU_SAMPLE_SECONDS (default 2; 0 turns sampling off). A bad value
    turns sampling off with a warning rather than failing the stage."""
    env = os.environ if env is None else env
    raw = (env.get(GPU_SAMPLE_VAR) or "").strip()
    if not raw:
        return GPU_SAMPLE_DEFAULT
    try:
        seconds = float(raw)
    except ValueError:
        seconds = -1
    if not math.isfinite(seconds) or seconds < 0:
        print(f"WARNING: {GPU_SAMPLE_VAR}={raw!r} is not a number of seconds; GPU sampling is off", flush=True)
        return 0.0
    return seconds


def parse_gpu_line(line):
    """One line of `nvidia-smi --query-gpu=index,utilization.gpu,memory.used,memory.total
    --format=csv,noheader,nounits` -> (index, utilisation %, memory used MiB, memory total MiB). A value the GPU
    doesn't report ([N/A], [Not Supported]) is None; a line that isn't a sample (a warning, a blank line) gives None."""
    fields = [f.strip() for f in line.split(",")]
    if len(fields) != 4:
        return None
    try:
        index = int(fields[0])
    except ValueError:
        return None

    def number(text):
        try:
            return float(text)
        except ValueError:
            return None

    util, used, total = (number(f) for f in fields[1:])
    if util is None and used is None:
        return None
    return index, util, used, total


class GpuSampler:
    """Runs nvidia-smi in looping mode in the background and keeps [(timestamp, index, util, used, total), ...].

    start() does nothing when interval is 0 or nvidia-smi isn't there; stop() is safe to call more than once.
    Neither raises: a GPU that can't be measured must not fail the stage."""

    def __init__(self, interval, clock=time.time):
        self.interval = interval
        self.clock = clock
        self.samples = []
        self._proc = None
        self._reader = None

    @property
    def effective_interval(self):
        """Seconds between samples as nvidia-smi is asked for them (at least 0.1 s), or None when off."""
        return max(int(self.interval * 1000), 100) / 1000 if self.interval else None

    def command(self):
        ms = int(self.effective_interval * 1000)
        return ["nvidia-smi", f"--query-gpu={GPU_QUERY}", "--format=csv,noheader,nounits", "-lms", str(ms)]

    def start(self):
        try:
            if not self.interval or not math.isfinite(self.interval):
                return self
            cmd = self.command()
            if not shutil.which(cmd[0]):
                return self
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
            )
        except Exception as err:  # noqa: BLE001 - measuring is best effort
            print(f"WARNING: could not start GPU sampling: {err}", flush=True)
            self._proc = None
            return self
        self._reader = threading.Thread(target=self._read, args=(self._proc.stdout,), daemon=True)
        self._reader.start()
        return self

    def _read(self, stdout):
        try:
            for line in stdout:
                sample = parse_gpu_line(line)
                if sample:
                    self.samples.append((self.clock(), *sample))
        except Exception as err:  # noqa: BLE001 - measuring is best effort
            print(f"WARNING: GPU sampling stopped: {err}", flush=True)

    def stop(self):
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            kill_process_group(proc, wait=5)
            proc.wait(timeout=5)
        except Exception as err:  # noqa: BLE001
            print(f"WARNING: could not stop GPU sampling cleanly: {err}", flush=True)
        if self._reader is not None:
            self._reader.join(timeout=5)
        try:
            proc.stdout.close()
        except Exception:  # noqa: BLE001, S110 - only freeing the pipe
            pass


def _gpu_stats(rows):
    utils = [r[2] for r in rows if r[2] is not None]
    used = [r[3] for r in rows if r[3] is not None]
    return {
        "samples": len(rows),
        "utilization_mean_pct": round(sum(utils) / len(utils), 1) if utils else None,
        "utilization_peak_pct": max(utils) if utils else None,
        "memory_peak_mib": max(used) if used else None,
    }


def summarise_gpu(samples, windows=()):
    """GPU figures for the whole run and per step (windows: parse_step_windows), or None without samples.
    With several GPUs, every GPU's samples count (the job definition asks for one)."""
    if not samples:
        return None
    totals = [s[4] for s in samples if s[4] is not None]
    per_step = {}
    windows = list(windows)
    for i, (step, start, end) in enumerate(windows):
        last = i == len(windows) - 1  # half-open, so a sample on a boundary counts once; the last step keeps its end
        per_step.setdefault(step, []).extend(s for s in samples if start <= s[0] < end or (last and s[0] == end))
    return {
        "gpus": len({s[1] for s in samples}),
        "memory_total_mib": max(totals) if totals else None,
        **_gpu_stats(samples),
        "steps": {step: _gpu_stats(rows) for step, rows in per_step.items() if rows},
    }


def gpu_metric_usage(stats):
    """A step's GPU figures as metrics.USAGE_METRICS keys (for metrics().record(..., usage=...))."""
    if not stats:
        return None
    usage = {
        "gpu_utilization_mean_pct": stats.get("utilization_mean_pct"),
        "gpu_utilization_peak_pct": stats.get("utilization_peak_pct"),
        "gpu_memory_peak_mib": stats.get("memory_peak_mib"),
    }
    return {k: v for k, v in usage.items() if v is not None} or None


def note_gpu(model, sampler, events, end_time):
    """Put the GPU figures into the run record's part and print them; returns the per-step figures ({} without).
    Never raises."""
    if not model:
        return {}
    try:
        samples = list(sampler.samples)  # a snapshot: the reader thread may still be appending
        summary = summarise_gpu(samples, parse_step_windows(events, end_time)) or {"samples": 0}
        provenance.note(gpu={"model": model, "sample_seconds": sampler.effective_interval, **summary})
        if summary["samples"]:
            print(
                f"GPU {model}: utilisation mean {summary['utilization_mean_pct']}% / peak "
                f"{summary['utilization_peak_pct']}%, memory peak {summary['memory_peak_mib']} of "
                f"{summary['memory_total_mib']} MiB ({summary['samples']} samples)",
                flush=True,
            )
            for step, stats in summary["steps"].items():
                print(
                    f"  {step:<22}utilisation mean {stats['utilization_mean_pct']}% / peak "
                    f"{stats['utilization_peak_pct']}%, memory peak {stats['memory_peak_mib']} MiB",
                    flush=True,
                )
        return summary.get("steps", {})
    except Exception as err:  # noqa: BLE001 - measuring is best effort
        print(f"WARNING: could not summarise GPU use: {err}", flush=True)
        return {}


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
        sampler = GpuSampler(gpu_sample_interval() if gpu else 0).start()
        try:
            code, events, end_time = run_steps(resumed, limits) if resumed else run_streaming(cmd, limits=limits)
        except WrapperStopped as err:
            sampler.stop()
            note_gpu(gpu, sampler, err.events, time.time())  # a run stopped on GPU memory is worth seeing
            if checkpoints_enabled() and not resumed:
                save_checkpoint(prefix, err.events, wd, intermediate, fingerprint, plan)
            raise
        finally:
            sampler.stop()
        gpu_steps = note_gpu(gpu, sampler, events, end_time)
        if code != 0:
            if checkpoints_enabled() and not resumed:
                save_checkpoint(prefix, events, wd, intermediate, fingerprint, plan)
            raise RuntimeError(f"{'resumed steps' if resumed else os.path.basename(cmd[0])} exited with code {code}")

        timings = parse_step_timings(events, end_time)
        print(f"\n{label} step timings for {sample_id} (shards={shards}, gpu={gpu or 'none'}):")
        for step, seconds in timings.items():
            print(f"  {step:<22}{seconds:9.1f}s")
            try:
                metrics().record(
                    step_metrics[step], sample_id, "Success", seconds, usage=gpu_metric_usage(gpu_steps.get(step))
                )
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
        upload_vcf_with_index(vcf, keys["vcf"])
        print(f"Uploaded {storage().describe(keys['vcf'])}")
        if resumed:
            retire(prefix, "used")
    finally:
        shutil.rmtree(wd, ignore_errors=True)


# ---- Split mode (v1.5, docs/design/deepvariant-split.md): make_examples on CPU jobs, the rest on the GPU job ----
# make_examples is ~84% of the stage and CPU-only (4 Nov 2026, REAL-HG003-30x-chr20-DV: 1,007 of 1,200 s on a
# g6.xlarge, GPU idle), so it can run on cheap CPU capacity. Each part job runs a contiguous range of the
# wrapper's make_examples tasks (the wrapper's own command, with `seq 0 N-1` narrowed to the range) and uploads
# what they wrote; the GPU job runs the wrapper's own call_variants and postprocess_variants commands on all of it.
# Examples are per run (RUN_NAME), like the bcftools parts: a re-run never mixes in another run's files, and
# intermediate/ expires.
EXAMPLES_PREFIX = "intermediate/dv/{sample_id}/{run}/"
PART_DIR = "shard-{part}/"
SPLIT_VERSION = 1
EXAMPLES_STAGE = "DeepVariantExamples"  # the part jobs' metric; they write no run-record part (one per task name)
SEQ_RE = re.compile(r"\bseq (\d+) (\d+) \|")
CPUS_RE = re.compile(r'--cpus "?\d+"?')
# Fields every part's manifest must share, with each other and (all but the inputs) with the GPU job.
SPLIT_KEYS = ("deepvariant", "shards", "parts", "model_type", "extra_args", "reference", "alignment")
# The files make_examples writes per task t of N: the examples (to call_variants) and the small model's calls.
TASK_FILE_RE = re.compile(r"-(\d{5})-of-(\d{5})")


def examples_prefix(sample_id):
    return EXAMPLES_PREFIX.format(sample_id=sample_id, run=_safe_name(os.environ.get("RUN_NAME") or "unnamed"))


def part_tasks(part, parts, shards):
    """(first, last) task numbers of part `part` of `parts`, contiguous and as even as possible over `shards` tasks."""
    if not 0 < parts <= shards:
        raise ValueError(f"need 1 <= parts <= DEEPVARIANT_SHARDS, got {parts} parts for {shards} tasks")
    if not 0 <= part < parts:
        raise ValueError(f"part {part} is not one of 0..{parts - 1}")
    return part * shards // parts, (part + 1) * shards // parts - 1


def part_command(command, first, last, shards):
    """The wrapper's make_examples command for tasks first..last only. Its form (DeepVariant 1.10.0):
    `seq 0 N-1 | parallel ... make_examples ... --examples ...@N.gz ... --task {}`; anything else is a ValueError
    rather than a silent run of the wrong tasks."""
    match = SEQ_RE.search(command)
    if not match or match.groups() != ("0", str(shards - 1)) or "--task {}" not in command:
        raise ValueError(
            f"unexpected make_examples command (expected `seq 0 {shards - 1} | ... --task {{}}`): {command}"
        )
    return command[: match.start()] + f"seq {first} {last} |" + command[match.end() :]


def split_settings(env=None):
    env = os.environ if env is None else env
    return env.get("DEEPVARIANT_MODEL_TYPE", "WGS"), env.get("DEEPVARIANT_EXTRA_ARGS", "")


def task_files(names, shards):
    """{task: [names]} of make_examples' per-task files among names; a file for another shard count is an error."""
    found = {}
    for name in names:
        match = TASK_FILE_RE.search(name)
        if not match:
            continue
        if int(match.group(2)) != shards:
            raise ValueError(f"{name} is from a run with {int(match.group(2))} tasks, not {shards}")
        found.setdefault(int(match.group(1)), []).append(name)
    return found


def _make_examples_part(sample_id, reference_name, part, parts):
    wd = work_dir(sample_id)
    try:
        limits = WrapperLimits.from_env()
        shards = int(os.environ.get("DEEPVARIANT_SHARDS") or parts * (os.cpu_count() or 1))
        first, last = part_tasks(part, parts, shards)
        model_type, extra_args = split_settings()
        intermediate = os.path.join(wd, "intermediate")
        os.makedirs(intermediate)
        bam = fetch_alignment(sample_id, wd, need_index=True, indexer=index_bam)
        ref = ensure_reference_local(reference_name, wd, need_bwa_index=False)
        vcf = os.path.join(wd, f"{sample_id}.deepvariant.vcf.gz")  # never written: only make_examples runs here
        cmd = build_run_deepvariant_cmd(
            ref, bam, vcf, intermediate, sample_id, shards, model_type=model_type, extra_args=extra_args
        )
        version = wrapper_version(cmd[0])
        if version is None:
            raise RuntimeError(f"could not tell the DeepVariant version of {cmd[0]}: parts must record it")
        commands = [c for n, c in planned_steps(cmd) if n == "make_examples"]
        if len(commands) != 1:
            raise RuntimeError(f"{os.path.basename(cmd[0])} --dry_run gave {len(commands)} make_examples commands")
        command = part_command(commands[0], first, last, shards)
        print(f"[geyser] make_examples part {part} of {parts}: tasks {first}-{last} of {shards}", flush=True)
        code, events, end_time = run_steps([("make_examples", command)], limits)
        if code != 0:
            raise RuntimeError(f"make_examples (tasks {first}-{last}) exited with code {code}")
        seconds = parse_step_timings(events, end_time).get("make_examples")
        counts = parse_example_counts(events)
        names = sorted(f for f in os.listdir(intermediate) if os.path.isfile(os.path.join(intermediate, f)))
        per_task = task_files(names, shards)
        missing = [t for t in range(first, last + 1) if t not in per_task]
        stray = sorted(t for t in per_task if not first <= t <= last)
        if missing or stray:
            raise RuntimeError(f"make_examples wrote files for tasks {sorted(per_task)}; expected {first}-{last}")
        prefix = examples_prefix(sample_id) + PART_DIR.format(part=part)
        files = []
        for name in names:
            storage().upload(os.path.join(intermediate, name), prefix + name)
            files.append({"name": name, "bytes": os.path.getsize(os.path.join(intermediate, name))})
        manifest = {
            "split_version": SPLIT_VERSION,
            "sample_id": sample_id,
            "run_name": os.environ.get("RUN_NAME") or None,
            "deepvariant": version,
            "shards": shards,
            "parts": parts,
            "part": part,
            "tasks": [first, last],
            "model_type": model_type,
            "extra_args": extra_args,
            "reference": file_identity(ref),
            "alignment": file_identity(bam),
            "files": files,
            "examples": counts["examples"],
            "small_model_calls": counts["small_model_calls"],
            "make_examples_seconds": round(seconds, 1) if seconds is not None else None,
            "cpus": os.cpu_count(),
        }
        storage().upload_stream(io.BytesIO(json.dumps(manifest, indent=2).encode("utf-8")), prefix + MANIFEST)
        total = sum(f["bytes"] for f in files)
        print(
            f"[geyser] part {part}: {len(files)} files ({total / 1e6:,.1f} MB), {counts['examples']} examples, "
            f"make_examples {seconds or 0:.1f} s -> {storage().describe(prefix)}",
            flush=True,
        )
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def read_part_manifests(prefix):
    """Every part's manifest under the run's examples prefix, by part number."""
    manifests = {}
    for key in storage().list_keys(prefix):
        if key.endswith("/" + MANIFEST):
            with storage().open_read(key) as fh:
                manifest = json.load(fh)
            manifests[manifest.get("part")] = manifest
    return manifests


def check_parts(manifests, version, model_type, extra_args):
    """The split's settings if the parts are complete and consistent, else a ValueError saying what is wrong:
    no parts, a part missing, parts that disagree (DeepVariant version, task count, model, flags, inputs), tasks
    not covered exactly once, or settings that differ from this (GPU) job's."""
    if not manifests:
        raise ValueError("no make_examples parts (run the --make-examples-part jobs first)")
    if any(not isinstance(p, int) for p in manifests):
        raise ValueError("a manifest has no part number")
    bad = [p for p, m in manifests.items() if m.get("split_version") != SPLIT_VERSION]
    if bad:
        raise ValueError(f"parts {bad} have split_version != {SPLIT_VERSION}")
    first = manifests[min(manifests)]
    for key in SPLIT_KEYS:
        values = {json.dumps(m.get(key), sort_keys=True) for m in manifests.values()}
        if len(values) > 1:
            raise ValueError(f"the parts disagree on {key}: {sorted(values)}")
    parts, shards = first["parts"], first["shards"]
    if sorted(manifests) != list(range(parts)):
        raise ValueError(f"parts {sorted(p for p in range(parts) if p not in manifests)} of {parts} are missing")
    covered = sorted(t for m in manifests.values() for t in range(m["tasks"][0], m["tasks"][1] + 1))
    if covered != list(range(shards)):
        raise ValueError(f"the parts' tasks don't cover 0..{shards - 1} exactly once")
    own = {"deepvariant": version, "model_type": model_type, "extra_args": extra_args}
    differ = [f"{k} ({first[k]!r} in the parts, {v!r} here)" for k, v in own.items() if first[k] != v]
    if differ:
        raise ValueError("this job's settings differ from the parts': " + "; ".join(differ))
    return first


def gpu_steps_from_plan(plan, cpus):
    """The wrapper's call_variants and postprocess_variants commands, postprocess_variants' --cpus set to this
    job's CPUs (the wrapper puts the task count there, which the GPU job may not have)."""
    names = [n for n, _ in plan]
    if names[-2:] != ["call_variants", "postprocess_variants"] or "make_examples" not in names:
        raise RuntimeError(f"unexpected DeepVariant plan: {names}")
    steps = plan[names.index("call_variants") :]
    return [(n, CPUS_RE.sub(f'--cpus "{cpus}"', c) if n == "postprocess_variants" else c) for n, c in steps]


def _call_from_examples(sample_id, reference_name):
    keys = deepvariant_keys(sample_id)
    wd = work_dir(sample_id)
    try:
        limits = WrapperLimits.from_env()
        prefix = examples_prefix(sample_id)
        manifests = read_part_manifests(prefix)
        model_type, extra_args = split_settings()
        vcf = os.path.join(wd, f"{sample_id}.deepvariant.vcf.gz")
        intermediate = os.path.join(wd, "intermediate")
        os.makedirs(intermediate)
        version = wrapper_version(RUN_DEEPVARIANT)
        split = check_parts(manifests, version, model_type, extra_args)  # before any download
        ref = ensure_reference_local(reference_name, wd, need_bwa_index=False)
        if file_identity(ref) != split["reference"]:
            raise ValueError(f"reference/{reference_name} differs from the one the parts used")
        shards, parts = split["shards"], split["parts"]
        started = time.monotonic()
        total = 0
        for part in range(parts):
            for f in manifests[part]["files"]:
                storage().download(
                    prefix + PART_DIR.format(part=part) + f["name"], os.path.join(intermediate, f["name"])
                )
                total += f["bytes"]
        print(
            f"[geyser] {parts} make_examples parts, {shards} tasks: {total / 1e6:,.1f} MB of examples downloaded in "
            f"{time.monotonic() - started:.0f} s",
            flush=True,
        )
        provenance.note_input(prefix)
        # The alignment itself isn't read here; the wrapper's dry run doesn't need the file to exist.
        reads = os.path.join(wd, split["alignment"][0] if split["alignment"] else "alignment.cram")
        cmd = build_run_deepvariant_cmd(
            ref, reads, vcf, intermediate, sample_id, shards, model_type=model_type, extra_args=extra_args
        )
        steps = gpu_steps_from_plan(planned_steps(cmd), os.cpu_count() or 1)
        gpu = gpu_name()
        sampler = GpuSampler(gpu_sample_interval() if gpu else 0).start()
        try:
            code, events, end_time = run_steps(steps, limits)
        except WrapperStopped as err:
            sampler.stop()
            note_gpu(gpu, sampler, err.events, time.time())
            raise
        finally:
            sampler.stop()
        gpu_steps = note_gpu(gpu, sampler, events, end_time)
        if code != 0:
            ran = parse_step_windows(events, end_time)
            raise RuntimeError(f"{ran[-1][0] if ran else steps[0][0]} exited with code {code}")
        timings = parse_step_timings(events, end_time)
        seconds = [m.get("make_examples_seconds") for _, m in sorted(manifests.items())]
        if all(s is not None for s in seconds):
            timings = {"make_examples": max(seconds), **timings}  # the parts run in parallel: the slowest counts
        print(
            f"\nDeepVariant (split, {parts} parts) step timings for {sample_id} (shards={shards}, gpu={gpu or 'none'}):"
        )
        for step, value in timings.items():
            print(f"  {step:<22}{value:9.1f}s")
            try:
                metrics().record(
                    STEP_METRICS[step], sample_id, "Success", value, usage=gpu_metric_usage(gpu_steps.get(step))
                )
            except Exception as err:  # noqa: BLE001 - metrics are best effort
                print(f"WARNING: could not record metric: {err}", flush=True)
        examples = sum(m.get("examples") or 0 for m in manifests.values())
        small = sum(m.get("small_model_calls") or 0 for m in manifests.values())
        print(
            f"  examples sent to call_variants: {examples}; called by the small model in make_examples (CPU): {small}"
        )
        provenance.note(
            make_examples={
                "split": True,
                "parts": parts,
                "shards": shards,
                "examples": examples,
                "small_model_calls": small,
                "part_seconds": seconds,
                "part_cpus": [m.get("cpus") for _, m in sorted(manifests.items())],
                "bytes": total,
            }
        )
        if not os.path.isfile(vcf) or os.path.getsize(vcf) == 0:
            raise RuntimeError(f"DeepVariant produced no VCF at {vcf}")
        for local, key in caller_stats(ref, vcf, wd, keys):  # before the VCF, which marks the stage done
            upload_output(local, key)
        upload_vcf_with_index(vcf, keys["vcf"])
        print(f"Uploaded {storage().describe(keys['vcf'])}")
    finally:
        shutil.rmtree(wd, ignore_errors=True)


@timed_stage(EXAMPLES_STAGE, run_record=False)
def make_examples_part_task(sample_id, reference_name, part, parts):
    _make_examples_part(sample_id, reference_name, part, parts)


@timed_stage(STAGE_NAME[False])
def deepvariant_from_examples_task(sample_id, reference_name):
    _call_from_examples(sample_id, reference_name)


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
    split = parser.add_mutually_exclusive_group()
    split.add_argument(
        "--make-examples-part",
        type=int,
        metavar="P",
        help="split mode, CPU image: run make_examples for part P of --parts and upload the examples",
    )
    split.add_argument(
        "--from-examples",
        action="store_true",
        help="split mode, GPU image: call_variants + postprocess_variants on every part's examples",
    )
    parser.add_argument(
        "--parts", type=int, metavar="J", help="the number of make_examples parts (with --make-examples-part)"
    )
    args = parser.parse_args(argv)
    if (args.make_examples_part is not None) != (args.parts is not None):
        parser.error("--make-examples-part and --parts go together")
    if args.pangenome and (args.make_examples_part is not None or args.from_examples):
        parser.error("split mode is for linear DeepVariant only, not --pangenome")
    if args.make_examples_part is not None:
        make_examples_part_task(args.srr_id, args.reference_name, args.make_examples_part, args.parts)
        task = f"deepvariant make_examples part {args.make_examples_part} of {args.parts}"
    elif args.from_examples:
        deepvariant_from_examples_task(args.srr_id, args.reference_name)
        task = "deepvariant (from examples)"
    elif args.pangenome:
        pangenome_deepvariant_task(args.srr_id, args.reference_name, args.pangenome)
        task = "pangenome"
    else:
        deepvariant_task(args.srr_id, args.reference_name)
        task = "deepvariant"
    print(f"Task '{task}' completed for sample '{args.srr_id}'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
