#!/usr/bin/env python3
"""Run the whole pipeline on this machine, with no AWS involved.

This plays the part of the cloud orchestration:
  * Lambda trigger  -> the sample ID is parsed from raw_reads/<id>.fastq.gz using the
                       same function the Lambda uses (lambda/trigger/index.py).
  * Step Functions  -> the three stages run with the same commands (infrastructure/state_machine.asl.json);
                       qc and align are independent (both read raw_reads/): the cloud runs them in
                       parallel, here they run one after the other. The first failure stops the run.
  * AWS Batch       -> each stage runs in a fresh container from the same image.
  * S3              -> a local folder (default ./local_bucket) mounted at /data.

Typical use:
    docker build -f Dockerfile -t geyser-app .
    python scripts/make_test_data.py
    python scripts/run_local.py --sample SYNTH001 --reference synthetic_ref.fa --check

Variant caller (--caller):
    bcftools     the 'variants' stage in geyser-app (default; what the cloud runs today)
    deepvariant  DeepVariant in its own image instead, with --gpus all (see app-deepvariant/)
    both         bcftools, then DeepVariant, then a comparison of the two call sets
    pangenome    pangenome-aware DeepVariant, with --gpus all, using a pangenome graph under pangenome/
    all          bcftools, DeepVariant and pangenome-aware DeepVariant, then comparisons

    docker build -f app-deepvariant/Dockerfile -t geyser-deepvariant .
    python scripts/run_local.py --sample SYNTH001 --reference synthetic_ref.fa --caller both --check

    docker build -f app-deepvariant/Dockerfile --build-arg DV_TAG=pangenome_aware_deepvariant-1.10.0-gpu \
        -t geyser-deepvariant-pangenome .
    python scripts/fetch_dv_testdata.py --dataset hg003-chr20 --pangenome        # chr20 graph (72 MB)
    python scripts/run_local.py --sample HG003_chr20 --reference GRCh38_chr20.fa --caller pangenome \
        --skip-to-variants --check --dv-memory 14g

--check compares each VCF against truth/<sample>.json (from make_test_data.py), or against
truth/<sample>.vcf.gz + truth/<sample>.bed (from fetch_dv_testdata.py) as a counts-only report.
"""

import argparse
import bisect
import glob
import gzip
import importlib.util
import json
import os
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Same stages and arguments as the Step Functions definition (qc and align are its parallel branches).
STAGES = [
    ("qc", False),
    ("align", True),
    ("variants", True),
]

CALLERS = ("bcftools", "deepvariant", "both", "pangenome", "all")
# The call sets each --caller produces, in run order.
CALLER_SETS = {
    "bcftools": ["bcftools"],
    "deepvariant": ["deepvariant"],
    "both": ["bcftools", "deepvariant"],
    "pangenome": ["pangenome"],
    "all": ["bcftools", "deepvariant", "pangenome"],
}
VCF_KEYS = {
    "bcftools": "variants/{id}.vcf.gz",
    "deepvariant": "variants_deepvariant/{id}.vcf.gz",
    "pangenome": "variants_pangenome/{id}.vcf.gz",
}
# HPRC v1.1 Minigraph-Cactus graph on GRCh38 (the graph Google's case studies use), cut down to chr20 by
# fetch_dv_testdata.py --pangenome. The whole-genome graph (hprc-v1.1-mc-grch38.gbz) needs ~10.7 GB of shared
# memory once loaded, plus ~2 GB per make_examples worker: too much for a 32 GB desktop.
DEFAULT_PANGENOME = "hprc-v1.1-mc-grch38.chr20.gbz"
# Google's case studies run the pangenome container with --shm-size 12gb.
DEFAULT_PANGENOME_SHM = "12g"
DEEPVARIANT_PYTHON = "/opt/geyser-venv/bin/python"
DEEPVARIANT_ENV_VARS = ("DEEPVARIANT_EXTRA_ARGS", "DEEPVARIANT_SHARDS", "DEEPVARIANT_MODEL_TYPE")
# Passed to every pipeline-stage container: ALIGNER=bwa runs the align stage with bwa instead of bwa-mem2.
PIPELINE_ENV_VARS = ("ALIGNER",)
# ALTs that are not real variants (gVCF-style placeholders, DeepVariant RefCall rows).
NON_VARIANT_ALTS = {".", "<*>", "<NON_REF>"}


def load_trigger():
    """Import lambda/trigger/index.py by path ('lambda' is a Python keyword, so no normal import)."""
    path = os.path.join(REPO_ROOT, "lambda", "trigger", "index.py")
    spec = importlib.util.spec_from_file_location("geyser_trigger", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sample_keys(trigger, root, sample=None):
    """One raw_reads/<id>.fastq.gz key per sample to run.

    A paired sample has two files (<id>_R1.fastq.gz and <id>_R2.fastq.gz) but is one run, so IDs are
    deduplicated with the trigger's own parse_sample_id. The stages detect paired vs single-end themselves,
    so the plain key is enough for build_pipeline_input.
    """
    if sample:
        return [f"raw_reads/{sample}.fastq.gz"]
    found = sorted(glob.glob(os.path.join(root, "raw_reads", "*.gz")))
    ids = sorted({trigger.parse_sample_id(os.path.relpath(f, root).replace(os.sep, "/")) for f in found})
    return [f"raw_reads/{sample_id}.fastq.gz" for sample_id in ids]


def stage_command(stage, sample_id, reference_name, needs_reference):
    cmd = ["python", "tasks.py", stage, sample_id]
    if needs_reference:
        cmd.append(reference_name)
    return cmd


def deepvariant_command(sample_id, reference_name, pangenome=None):
    cmd = [DEEPVARIANT_PYTHON, "deepvariant_task.py", sample_id, reference_name]
    return cmd + ["--pangenome", pangenome] if pangenome else cmd


def pipeline_jobs(sample_id, reference_name, caller="bcftools", app_image="geyser-app",
                  dv_image="geyser-deepvariant", gpus=True, skip_to_variants=False,
                  pg_image="geyser-deepvariant-pangenome", pangenome=DEFAULT_PANGENOME,
                  pg_shm=DEFAULT_PANGENOME_SHM):
    """The containers to run, in order, as dicts with name, cmd, image, gpus (and shm for pangenome)."""
    if caller not in CALLERS:
        raise ValueError(f"unknown caller {caller!r}")
    sets = CALLER_SETS[caller]
    jobs = []
    for stage, needs_ref in STAGES:
        if skip_to_variants and stage != "variants":
            continue
        if stage == "variants" and "bcftools" not in sets:
            continue
        jobs.append({"name": stage, "cmd": stage_command(stage, sample_id, reference_name, needs_ref),
                     "image": app_image, "gpus": False})
    if "deepvariant" in sets:
        jobs.append({"name": "deepvariant", "cmd": deepvariant_command(sample_id, reference_name),
                     "image": dv_image, "gpus": gpus})
    if "pangenome" in sets:
        # Google publishes pangenome-aware images with and without GPU support (pangenome_aware_deepvariant-
        # <version>[-gpu]); like linear DeepVariant, only call_variants uses the GPU. --no-gpu for the CPU image.
        jobs.append({"name": "pangenome", "cmd": deepvariant_command(sample_id, reference_name, pangenome),
                     "image": pg_image, "gpus": gpus, "shm": pg_shm})
    return jobs


def docker_command(cmd, root, image, gpus=False, extra_env=None, memory=None, shm=None):
    full = ["docker", "run", "--rm"]
    if gpus:
        full += ["--gpus", "all"]
    if shm:
        full += ["--shm-size", shm]
    if memory:
        # Hard cap (also counts the container's page cache): keeps a big run from starving the host.
        full += ["--memory", memory, "--memory-swap", memory]
    full += ["--mount", f"type=bind,source={os.path.abspath(root)},target=/data"]
    env = {"STORAGE_BACKEND": "local", "METRICS_BACKEND": "local", "LOCAL_STORAGE_DIR": "/data",
           **(extra_env or {})}
    for k, v in env.items():
        full += ["-e", f"{k}={v}"]
    return full + [image] + cmd


def run_job(job, runner, root, dv_memory=None):
    if runner == "docker":
        # Pipeline and DeepVariant knobs are passed through to the container when set on the host
        # (the host runner inherits the whole environment anyway).
        extra = {k: os.environ[k] for k in PIPELINE_ENV_VARS if os.environ.get(k)}
        memory = None
        if job["name"] in ("deepvariant", "pangenome"):
            extra.update({k: os.environ[k] for k in DEEPVARIANT_ENV_VARS if os.environ.get(k)})
            memory = dv_memory
        return subprocess.run(docker_command(job["cmd"], root, job["image"], job["gpus"], extra, memory,
                                             job.get("shm"))).returncode
    if job["name"] in ("deepvariant", "pangenome"):
        print("DeepVariant only runs in its container (use --runner docker).")
        return 1
    # host: tools (bwa-mem2 and bwa, samtools, bcftools, fastp) must be on PATH. bwa-mem2 has no
    # apt package (the image installs the upstream release); without it, set ALIGNER=bwa.
    host_env = {**os.environ, "STORAGE_BACKEND": "local", "METRICS_BACKEND": "local",
                "LOCAL_STORAGE_DIR": os.path.abspath(root)}
    return subprocess.run([sys.executable, "tasks.py"] + job["cmd"][2:], cwd=os.path.join(REPO_ROOT, "app"),
                          env=host_env).returncode


def read_vcf_calls(path, regions=None):
    """Return {(pos, ref, alt)} for the variant calls in a (b)gzipped VCF.

    Skips filtered rows (e.g. DeepVariant's RefCall and NoCall) and placeholder ALTs. The contig is
    ignored: every test dataset here has a single contig. regions: optional BED [(start0, end)],
    non-overlapping (GIAB confident-region BEDs are merged).
    """
    if regions is not None:
        regions = sorted(regions)
        starts = [s for s, _ in regions]
    calls = set()
    with gzip.open(path, "rt") as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            cols = line.rstrip("\n").split("\t")
            if cols[6] not in ("PASS", "."):
                continue
            pos = int(cols[1])
            if regions is not None:
                # BED is 0-based half-open: 1-based pos is inside (start, end]. Binary search, since a
                # whole-chromosome BED has tens of thousands of intervals.
                i = bisect.bisect_left(starts, pos) - 1
                if i < 0 or pos > regions[i][1]:
                    continue
            for alt in cols[4].split(","):
                if alt not in NON_VARIANT_ALTS:
                    calls.add((pos, cols[3], alt))
    return calls


def read_bed(path):
    regions = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip() and not line.startswith(("#", "track", "browser")):
                cols = line.split("\t")
                regions.append((int(cols[1]), int(cols[2])))
    return regions


def vcf_path(root, caller, sample_id):
    return os.path.join(root, *VCF_KEYS[caller].format(id=sample_id).split("/"))


def truth_counts(root, sample_id, caller="bcftools"):
    """Compare a caller's VCF with the truth set. Returns a dict of counts, or None if there is no truth."""
    truth_dir = os.path.join(root, "truth")
    json_truth = os.path.join(truth_dir, f"{sample_id}.json")
    vcf_truth = os.path.join(truth_dir, f"{sample_id}.vcf.gz")
    if os.path.exists(json_truth):
        with open(json_truth, encoding="utf-8") as fh:
            truth = {(s["pos"], s["ref"], s["alt"]) for s in json.load(fh)["snps"]}
        called, kind = read_vcf_calls(vcf_path(root, caller, sample_id)), "planted"
    elif os.path.exists(vcf_truth):
        bed = os.path.join(truth_dir, f"{sample_id}.bed")
        regions = read_bed(bed) if os.path.exists(bed) else None
        truth = read_vcf_calls(vcf_truth, regions)
        called, kind = read_vcf_calls(vcf_path(root, caller, sample_id), regions), "giab"
    else:
        return None
    return {"kind": kind, "truth": len(truth), "tp": len(truth & called),
            "missed": sorted(truth - called), "extra": sorted(called - truth)}


def check_against_truth(root, sample_id, caller="bcftools"):
    """Print the truth comparison. Strict pass/fail for planted SNPs; report-only for GIAB truth."""
    counts = truth_counts(root, sample_id, caller)
    if counts is None:
        print(f"\nTruth check ({caller}): no truth/{sample_id}.json or truth/{sample_id}.vcf.gz under {root}")
        return False
    if counts["kind"] == "planted":
        print(f"\nTruth check ({caller}): {counts['tp']}/{counts['truth']} planted SNPs called, "
              f"{len(counts['extra'])} unexpected calls")
        for item in counts["missed"]:
            print(f"  MISSED   pos={item[0]} {item[1]}>{item[2]}")
        for item in counts["extra"]:
            print(f"  EXTRA    pos={item[0]} {item[1]}>{item[2]}")
        return not counts["missed"] and not counts["extra"]
    # Real data: exact (pos, ref, alt) matching with no normalisation, so this is indicative only.
    tp, fn, fp = counts["tp"], len(counts["missed"]), len(counts["extra"])
    recall = tp / counts["truth"] if counts["truth"] else 0.0
    precision = tp / (tp + fp) if tp + fp else 0.0
    print(f"\nTruth check ({caller}, GIAB, report only): TP={tp} FN={fn} FP={fp} "
          f"recall={recall:.3f} precision={precision:.3f}")
    return True


def compare_callers(root, sample_id, first="bcftools", second="deepvariant"):
    a = read_vcf_calls(vcf_path(root, first, sample_id))
    b = read_vcf_calls(vcf_path(root, second, sample_id))
    print(f"\nCaller comparison for {sample_id}: shared={len(a & b)} "
          f"{first}-only={len(a - b)} {second}-only={len(b - a)}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default=os.path.join(REPO_ROOT, "local_bucket"), help="local storage root")
    p.add_argument("--sample", help="sample ID; default: every raw_reads/*.fastq.gz under --root")
    p.add_argument("--reference", help="reference name under reference/ (default: same as the Lambda)")
    p.add_argument("--runner", choices=["docker", "host"], default="docker")
    p.add_argument("--image", default="geyser-app")
    p.add_argument("--caller", choices=CALLERS, default="bcftools")
    p.add_argument("--dv-image", default="geyser-deepvariant", help="DeepVariant image (app-deepvariant/)")
    p.add_argument("--no-gpu", action="store_true",
                   help="run DeepVariant and pangenome-aware DeepVariant without --gpus all (CPU images)")
    p.add_argument("--pg-image", default="geyser-deepvariant-pangenome",
                   help="pangenome-aware DeepVariant image (app-deepvariant/ built with "
                        "DV_TAG=pangenome_aware_deepvariant-1.10.0-gpu, or without -gpu for CPU)")
    p.add_argument("--pangenome", default=DEFAULT_PANGENOME, metavar="GBZ",
                   help="pangenome graph under pangenome/ (default: %(default)s)")
    p.add_argument("--pg-shm-size", default=DEFAULT_PANGENOME_SHM, metavar="SIZE",
                   help="--shm-size for the pangenome container (default: %(default)s, as in Google's case studies)")
    p.add_argument("--dv-memory", metavar="SIZE",
                   help="memory cap for the DeepVariant and pangenome containers, e.g. 12g. A whole chromosome can "
                        "otherwise take most of the Docker VM; 16 make_examples shards thrashed under 8g")
    p.add_argument("--skip-to-variants", action="store_true",
                   help="start at variant calling, using an existing alignments/<sample>.cram (or .bam)")
    p.add_argument("--check", action="store_true", help="compare each VCF with truth/<sample>.*")
    a = p.parse_args(argv)

    trigger = load_trigger()
    keys = sample_keys(trigger, a.root, a.sample)
    if not keys:
        print(f"No inputs found under {a.root}/raw_reads/. Run scripts/make_test_data.py first.")
        return 1

    callers = CALLER_SETS[a.caller]
    ok = True
    for key in keys:
        payload = trigger.build_pipeline_input(key, a.reference)
        sample_id, reference = payload["srr_id"], payload["reference_name"]
        print(f"\n=== Pipeline input: {json.dumps(payload)} ===")
        started = time.time()
        jobs = pipeline_jobs(sample_id, reference, a.caller, a.image, a.dv_image,
                             gpus=not a.no_gpu, skip_to_variants=a.skip_to_variants,
                             pg_image=a.pg_image, pangenome=a.pangenome, pg_shm=a.pg_shm_size)
        for job in jobs:
            print(f"\n>>> [{job['image']}] {' '.join(job['cmd'])}", flush=True)
            if run_job(job, a.runner, a.root, a.dv_memory) != 0:
                print(f"\nFAILED at stage '{job['name']}' for {sample_id} (in the cloud this goes to Notify_Failure)")
                ok = False
                break
        else:
            outputs = ", ".join(vcf_path(a.root, c, sample_id) for c in callers)
            print(f"\n=== {sample_id} finished in {time.time() - started:.1f}s; VCF at {outputs} ===")
            if a.check:
                for caller in callers:
                    ok = check_against_truth(a.root, sample_id, caller) and ok
            for first, second in zip(callers, callers[1:]):
                compare_callers(a.root, sample_id, first, second)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
