# app-deepvariant/deepvariant_task.py
"""DeepVariant variant-calling stage: an alternative to the bcftools 'variants' stage.

Runs in its own image (app-deepvariant/Dockerfile, built on google/deepvariant) with:
    /opt/geyser-venv/bin/python deepvariant_task.py <sample_id> <reference_name>

Same storage contract as the other stages (app/storage.py, via app/tasks.py):
    reads  alignments/{id}.bam and reference/{reference_name} (+ optional .fai)
    writes variants_deepvariant/{id}.vcf.gz   (beside, not instead of, variants/{id}.vcf.gz)

DeepVariant runs three steps: make_examples (CPU), call_variants (the only GPU step) and
postprocess_variants (CPU). Each is timed separately and recorded as its own metric, so the
GPU's real contribution can be reported honestly.

Environment:
    DEEPVARIANT_MODEL_TYPE   default WGS
    DEEPVARIANT_SHARDS       make_examples parallelism, default: CPUs in the container
    DEEPVARIANT_EXTRA_ARGS   extra run_deepvariant flags, e.g. --disable_small_model=true
    RUN_DEEPVARIANT          path to run_deepvariant (default: the image's copy)
"""

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

from tasks import ensure_reference_local, keys_for, metrics, run_shell, storage, timed_stage, work_dir

RUN_DEEPVARIANT = os.environ.get("RUN_DEEPVARIANT", "/opt/deepvariant/bin/run_deepvariant")

# run_deepvariant prints this banner, then the command, before each step.
BANNER = "***** Running the command:*****"
# Match the binary path: later commands also mention 'make_examples' / 'call_variants' in file names.
STEP_RE = re.compile(r"/(make_examples|call_variants|postprocess_variants)(?:\s|$)")
STEP_METRICS = {
    "make_examples": "DeepVariant.MakeExamples",
    "call_variants": "DeepVariant.CallVariants",
    "postprocess_variants": "DeepVariant.PostprocessVariants",
}


def deepvariant_keys(sample_id: str) -> dict:
    return {
        "bam": keys_for(sample_id)["bam"],
        "vcf": f"variants_deepvariant/{sample_id}.vcf.gz",
    }


def build_run_deepvariant_cmd(ref, bam, vcf, intermediate_dir, sample_name, num_shards,
                              model_type="WGS", extra_args=""):
    return [
        RUN_DEEPVARIANT,
        f"--model_type={model_type}",
        f"--ref={ref}",
        f"--reads={bam}",
        f"--output_vcf={vcf}",
        f"--intermediate_results_dir={intermediate_dir}",
        f"--sample_name={sample_name}",
        f"--num_shards={num_shards}",
    ] + shlex.split(extra_args or "")


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
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                            encoding="utf-8", errors="replace")  # a stray byte in tool logs must not fail the stage
    for line in proc.stdout:
        events.append((time.time(), line.rstrip("\n")))
        print(line, end="", flush=True)
    return proc.wait(), events, time.time()


def index_bam(bam):
    run_shell(f"samtools index {shlex.quote(bam)}")


def gpu_name():
    """The GPU visible to this container, or None. With docker --gpus, nvidia-smi is mounted in."""
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    names = out.stdout.strip().splitlines() if out.returncode == 0 else []
    return names[0].strip() if names else None


@timed_stage("DeepVariant")
def deepvariant_task(sample_id, reference_name):
    keys = deepvariant_keys(sample_id)
    wd = work_dir(sample_id)
    try:
        bam = os.path.join(wd, f"{sample_id}.bam")
        vcf = os.path.join(wd, f"{sample_id}.deepvariant.vcf.gz")
        intermediate = os.path.join(wd, "intermediate")
        os.makedirs(intermediate)

        storage().download(keys["bam"], bam)
        ref = ensure_reference_local(reference_name, wd, need_bwa_index=False)
        index_bam(bam)  # DeepVariant needs random access; the bcftools path never indexed

        shards = int(os.environ.get("DEEPVARIANT_SHARDS") or os.cpu_count() or 1)
        gpu = gpu_name()
        cmd = build_run_deepvariant_cmd(
            ref, bam, vcf, intermediate, sample_id, shards,
            model_type=os.environ.get("DEEPVARIANT_MODEL_TYPE", "WGS"),
            extra_args=os.environ.get("DEEPVARIANT_EXTRA_ARGS", ""),
        )
        code, events, end_time = run_streaming(cmd)
        if code != 0:
            raise RuntimeError(f"run_deepvariant exited with code {code}")

        timings = parse_step_timings(events, end_time)
        print(f"\nDeepVariant step timings for {sample_id} (shards={shards}, gpu={gpu or 'none'}):")
        for step, seconds in timings.items():
            print(f"  {step:<22}{seconds:9.1f}s")
            try:
                metrics().record(STEP_METRICS[step], sample_id, "Success", seconds)
            except Exception as err:  # noqa: BLE001 - metrics are best effort
                print(f"WARNING: could not record metric: {err}", flush=True)
        counts = parse_example_counts(events)
        print(f"  examples sent to call_variants: {counts['examples']}; "
              f"called by the small model in make_examples (CPU): {counts['small_model_calls']}")
        missing = [s for s in STEP_METRICS if s not in timings]
        if missing:
            print(f"WARNING: no timing found for {missing}; run_deepvariant output format may have changed")

        if not os.path.isfile(vcf) or os.path.getsize(vcf) == 0:
            raise RuntimeError(f"DeepVariant produced no VCF at {vcf}")
        storage().upload(vcf, keys["vcf"])
        print(f"Uploaded {storage().describe(keys['vcf'])}")
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Call variants with DeepVariant (one pipeline stage).")
    parser.add_argument("srr_id", help="Sample ID, e.g. SRR062634")
    parser.add_argument("reference_name", help="Reference FASTA name under reference/, e.g. chr20.fa")
    args = parser.parse_args(argv)
    deepvariant_task(args.srr_id, args.reference_name)
    print(f"Task 'deepvariant' completed for sample '{args.srr_id}'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
