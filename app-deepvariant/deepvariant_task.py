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
    RUN_DEEPVARIANT          path to run_deepvariant (default: the image's copy)
    RUN_PANGENOME_DEEPVARIANT  path to run_pangenome_aware_deepvariant (default: the image's copy)
"""

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
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


def run_streaming(cmd):
    """Run cmd, echo its output live, and return (returncode, [(timestamp, line)], end_time)."""
    print("$ " + " ".join(shlex.quote(c) for c in cmd), flush=True)
    events = []
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, encoding="utf-8", errors="replace"
    )  # a stray byte in tool logs must not fail the stage
    for line in proc.stdout:
        events.append((time.time(), line.rstrip("\n")))
        print(line, end="", flush=True)
    return proc.wait(), events, time.time()


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


def _call_variants(sample_id, reference_name, pangenome=None):
    """Shared body of the linear and pangenome-aware stages. pangenome: graph name under pangenome/."""
    label = STAGE_NAME[bool(pangenome)]
    step_metrics = PANGENOME_STEP_METRICS if pangenome else STEP_METRICS
    keys = deepvariant_keys(sample_id, pangenome)
    wd = work_dir(sample_id)
    try:
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
        cmd = build_run_deepvariant_cmd(
            ref,
            bam,
            vcf,
            intermediate,
            sample_id,
            shards,
            model_type=os.environ.get("DEEPVARIANT_MODEL_TYPE", "WGS"),
            extra_args=os.environ.get("DEEPVARIANT_EXTRA_ARGS", ""),
            pangenome=graph,
        )
        code, events, end_time = run_streaming(cmd)
        if code != 0:
            raise RuntimeError(f"{os.path.basename(cmd[0])} exited with code {code}")

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
        missing = [s for s in step_metrics if s not in timings]
        if missing:
            print(f"WARNING: no timing found for {missing}; {os.path.basename(cmd[0])} output format may have changed")

        if not os.path.isfile(vcf) or os.path.getsize(vcf) == 0:
            raise RuntimeError(f"{label} produced no VCF at {vcf}")
        for local, key in caller_stats(ref, vcf, wd, keys, pangenome):  # before the VCF, which marks the stage done
            upload_output(local, key)
        upload_output(vcf, keys["vcf"])
        print(f"Uploaded {storage().describe(keys['vcf'])}")
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
