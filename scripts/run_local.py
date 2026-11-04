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

After variant calling, the quality gate (tasks.py gate, #144) checks the run's QC statistics and
writes qc_reports/<id>.gate.json, as the state machine's last step does in the cloud. GATE_MODE=warn
(default) only warns on a failed verdict, GATE_MODE=fail stops the run. For make_test_data.py samples (truth/<id>.json)
it uses the synthetic thresholds unless GATE_CONFIG is set; --no-gate skips it.

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

Aligner (--aligner): bwa-mem2 (default), bwa, or giraffe: vg giraffe on a pangenome graph (#146), in its own
image (app-giraffe/, built on geyser-app). The graph is pangenome/<--pangenome> (default: <reference stem>.gbz,
what make_test_data.py --pangenome writes); GIRAFFE_SAMPLING=off maps to the whole graph instead of a sampled one.

    docker build -f app-giraffe/Dockerfile -t geyser-giraffe .
    python scripts/make_test_data.py --pangenome
    python scripts/run_local.py --sample SYNTH001 --reference synthetic_ref.fa --aligner giraffe --check

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
DEEPVARIANT_ENV_VARS = (
    "DEEPVARIANT_EXTRA_ARGS",
    "DEEPVARIANT_SHARDS",
    "DEEPVARIANT_MODEL_TYPE",
    "DEEPVARIANT_TIMEOUT_SECONDS",
    "DEEPVARIANT_IDLE_TIMEOUT_SECONDS",
    "DEEPVARIANT_FATAL_GRACE_SECONDS",
    "DEEPVARIANT_CHECKPOINT",
    "DEEPVARIANT_GPU_SAMPLE_SECONDS",
)
# Passed to every pipeline-stage container: ALIGNER=bwa runs the align stage with bwa instead of bwa-mem2
# (--aligner sets it too); GIRAFFE_GRAPH, GIRAFFE_SAMPLING and KMC_MEMORY_GB tune ALIGNER=giraffe (tasks.py);
# ANNOTATE=off skips consequence annotation even when the reference has gene models (.gff3.gz);
# GATE_MODE (warn | fail) and GATE_CONFIG (a thresholds file) set up the quality gate.
PIPELINE_ENV_VARS = (
    "ALIGNER",
    "GIRAFFE_GRAPH",
    "GIRAFFE_SAMPLING",
    "KMC_MEMORY_GB",
    "ANNOTATE",
    "GATE_MODE",
    "GATE_CONFIG",
    "GEYSER_STREAM_REGIONS",  # 0: calling shards download the CRAM instead of reading their region in place
)
ALIGNERS = ("bwa-mem2", "bwa", "giraffe")  # tasks.ALIGNERS (a test keeps them equal)
# The gate's thresholds for make_test_data.py data: the image's copy, or the repo's for --runner host.
# The gate's threshold profiles (gate/thresholds/<profile>.toml), as the state machine's gate_profile
# input names them (Check_Gate_Profile; tests/test_consistency.py keeps the three in step).
GATE_PROFILES = ("default", "synthetic", "smoke")
# Where the image has gate/thresholds/ (Dockerfile: $GATE_THRESHOLDS_DIR).
IMAGE_THRESHOLDS_DIR = "/opt/geyser-gate/thresholds"


def gate_profile_config(profile, runner):
    """GATE_CONFIG for a threshold profile: its file in the image, or in the repo for --runner host."""
    if profile not in GATE_PROFILES:
        raise ValueError(f"gate profile must be one of {GATE_PROFILES}, got {profile!r}")
    if runner == "docker":
        return f"{IMAGE_THRESHOLDS_DIR}/{profile}.toml"
    return os.path.join(REPO_ROOT, "gate", "thresholds", f"{profile}.toml")


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


def pipeline_jobs(
    sample_id,
    reference_name,
    caller="bcftools",
    app_image="geyser-app",
    dv_image="geyser-deepvariant",
    gpus=True,
    skip_to_variants=False,
    pg_image="geyser-deepvariant-pangenome",
    pangenome=DEFAULT_PANGENOME,
    pg_shm=DEFAULT_PANGENOME_SHM,
    regions=None,
    shard_window=None,
    gate=False,
    gate_config=None,
    align_image=None,
    align_env=None,
    chunks=None,
):
    """The containers to run, in order, as dicts with name, cmd, image, gpus (and shm for pangenome, env).

    align_image / align_env: the align job's image and extra environment (--aligner giraffe: geyser-giraffe,
    ALIGNER=giraffe and the graph); default app_image and nothing.

    regions + shard_window: scatter-gather variant calling, as the Map state will run it (#108): one
    'variants --region' job per region, then 'variants --window' merging them into the usual VCF.
    gate: the quality gate, last, when the run made the sample's stats (bcftools, or a DeepVariant-only
    run's PASS-call stats, #147; not a caller added with skip_to_variants); gate_config: its
    GATE_CONFIG for this sample (None: the host's GATE_CONFIG, else the built-in thresholds).
    """
    if caller not in CALLERS:
        raise ValueError(f"unknown caller {caller!r}")
    sets = CALLER_SETS[caller]
    jobs = []
    for stage, needs_ref in STAGES:
        if skip_to_variants and stage != "variants":
            continue
        if stage == "variants" and "bcftools" not in sets:
            continue
        if stage == "variants" and regions:
            cmd = stage_command(stage, sample_id, reference_name, needs_ref)
            jobs += [
                {"name": "variants-shard", "cmd": cmd + ["--region", r], "image": app_image, "gpus": False}
                for r in regions
            ]
            jobs.append(
                {"name": stage, "cmd": cmd + ["--window", str(shard_window)], "image": app_image, "gpus": False}
            )
            continue
        job = {
            "name": stage,
            "cmd": stage_command(stage, sample_id, reference_name, needs_ref),
            "image": app_image,
            "gpus": False,
        }
        if stage == "align":
            job["image"] = align_image or app_image
            if align_env:
                job["env"] = dict(align_env)
            if chunks:  # v1.4's large route, as the state machine runs it: check and split, align each chunk, merge
                jobs += large_route_jobs(sample_id, reference_name, chunks, job["image"], job.get("env"))
                continue
        jobs.append(job)
    # Like the cloud's caller choice: without bcftools in the run, DeepVariant's stats are the sample's.
    # Not with --skip-to-variants: that adds a caller to an earlier run, whose stats stay the sample's.
    caller_owns_stats = "bcftools" not in sets and not skip_to_variants
    caller_env = {"PIPELINE_CALLER": sets[0]} if caller_owns_stats else {}
    if "deepvariant" in sets:
        jobs.append(
            {
                "name": "deepvariant",
                "cmd": deepvariant_command(sample_id, reference_name),
                "image": dv_image,
                "gpus": gpus,
                "env": caller_env if sets == ["deepvariant"] else {},
            }
        )
    if "pangenome" in sets:
        # Google publishes pangenome-aware images with and without GPU support (pangenome_aware_deepvariant-
        # <version>[-gpu]); like linear DeepVariant, only call_variants uses the GPU. --no-gpu for the CPU image.
        jobs.append(
            {
                "name": "pangenome",
                "cmd": deepvariant_command(sample_id, reference_name, pangenome),
                "image": pg_image,
                "gpus": gpus,
                "shm": pg_shm,
                "env": caller_env if sets == ["pangenome"] else {},
            }
        )
    # The gate judges the sample's stats: bcftools', or a DeepVariant-only run's (#147), as in the cloud. Not
    # an add-on caller (--skip-to-variants without bcftools): the stats it would judge are an earlier run's.
    if gate and ("bcftools" in sets or caller_owns_stats):
        # Last, so a failed gate (GATE_MODE=fail) can't keep DeepVariant or --check from running.
        job = {
            "name": "gate",
            "cmd": stage_command("gate", sample_id, None, False),
            "image": app_image,
            "gpus": False,
        }
        if gate_config:
            job["env"] = {"GATE_CONFIG": gate_config}
        jobs.append(job)
    return jobs


def large_route_jobs(sample_id, reference_name, chunks, image, env=None):
    """preflight --chunks N, align_chunk --chunk 0..N-1, align_merge: the large route's align (one RUN_NAME for all)."""

    def job(name, cmd):
        return {"name": name, "cmd": cmd, "image": image, "gpus": False, **({"env": dict(env)} if env else {})}

    base = ["python", "tasks.py"]
    jobs = [job("preflight", base + ["preflight", sample_id, "--chunks", str(chunks)])]
    jobs += [
        job("align_chunk", base + ["align_chunk", sample_id, reference_name, "--chunk", str(i)]) for i in range(chunks)
    ]
    return jobs + [job("align_merge", base + ["align_merge", sample_id, reference_name])]


def reference_problem(root, reference_name):
    """Why this reference can't be used, or None: the state machine's two checks before any job.

    Check_Reference_Name: a non-empty string without a slash (a file name under reference/).
    Check_Reference: reference/<name> exists (locally, under --root). tests/test_consistency.py keeps
    the rules in step with the state machine.
    """
    if not isinstance(reference_name, str) or not reference_name or "/" in reference_name:
        return f"reference {reference_name!r} is not a file name under reference/ (a non-empty string without a slash)"
    path = os.path.join(root, "reference", reference_name)
    if not os.path.isfile(path):
        return f"reference/{reference_name} is not under {root} (in the cloud: ReferenceNotFound before any job)"
    return None


def align_settings(aligner, pangenome, giraffe_image):
    """(align image, align env) for --aligner: giraffe runs in its own image with ALIGNER=giraffe and, when
    --pangenome was given, GIRAFFE_GRAPH; any other aligner in the pipeline image (None) with ALIGNER set."""
    if not aligner:
        return None, {}
    env = {"ALIGNER": aligner}
    if aligner != "giraffe":
        return None, env
    return giraffe_image, {**env, **({"GIRAFFE_GRAPH": pangenome} if pangenome else {})}


def local_regions(root, reference_name, window):
    """The regions tasks.py will merge for this reference (tasks.regions_from_lengths), from the local FASTA."""
    app_dir = os.path.join(REPO_ROOT, "app")
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)
    import tasks  # noqa: PLC0415 - only for --shard-window; tasks.py imports nothing AWS-specific at import time

    return tasks.reference_regions(os.path.join(root, "reference", reference_name), window)


def gate_config_for(root, sample_id, runner, profile=None):
    """GATE_CONFIG for the gate job: an explicit --gate-profile's file, else the host's GATE_CONFIG if set
    (for docker, a host file in the repo's gate/thresholds/ or under the storage root becomes its path
    in the container), else the synthetic thresholds for a make_test_data.py sample (it has
    truth/<id>.json), else None (built-in thresholds)."""
    if profile:
        return gate_profile_config(profile, runner)
    host = os.environ.get("GATE_CONFIG")
    if host:
        return container_path(host, root) if runner == "docker" else None
    if os.path.exists(os.path.join(root, "truth", f"{sample_id}.json")):
        return gate_profile_config("synthetic", runner)
    return None


def container_path(host_path, root):
    """Where a host file is inside the stage container: the repo's gate/thresholds/ files are in the
    image, the storage root is mounted at /data. Anything else (a container path already) as it is."""
    if not os.path.isfile(host_path):
        return host_path
    path = os.path.abspath(host_path)
    bases = (
        (os.path.join(REPO_ROOT, "gate", "thresholds"), IMAGE_THRESHOLDS_DIR),
        (root, "/data"),
    )
    for base, target in bases:
        base = os.path.abspath(base)
        if os.path.commonpath([path, base]) == base:
            return target + "/" + os.path.relpath(path, base).replace(os.sep, "/")
    return host_path


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
    env = {"STORAGE_BACKEND": "local", "METRICS_BACKEND": "local", "LOCAL_STORAGE_DIR": "/data", **(extra_env or {})}
    for k, v in env.items():
        full += ["-e", f"{k}={v}"]
    return full + [image] + cmd


def run_env(sample_id, image):
    """RUN_NAME and APP_VERSION for one job: the stages put them in the run record (app/provenance.py).

    One RUN_NAME per pipeline run, shaped like the cloud's <sample>-<UTC time>-<hash>, so the
    record keeps the parts of one run together. APP_VERSION names the local image (the cloud: its tag).
    """
    return {
        "RUN_NAME": f"{sample_id}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-local",
        "APP_VERSION": f"local:{image}",
    }


def run_job(job, runner, root, dv_memory=None, env=None):
    """env: extra variables for the stage (run_env), on top of the pass-through knobs."""
    if runner == "docker":
        # Pipeline and DeepVariant knobs are passed through to the container when set on the host
        # (the host runner inherits the whole environment anyway).
        extra = {k: os.environ[k] for k in PIPELINE_ENV_VARS if os.environ.get(k)}
        extra.update(job.get("env") or {})
        extra.update(env or {})
        memory = None
        if job["name"] in ("deepvariant", "pangenome"):
            extra.update({k: os.environ[k] for k in DEEPVARIANT_ENV_VARS if os.environ.get(k)})
            memory = dv_memory
        return subprocess.run(
            docker_command(job["cmd"], root, job["image"], job["gpus"], extra, memory, job.get("shm"))
        ).returncode
    if job["name"] in ("deepvariant", "pangenome"):
        print("DeepVariant only runs in its container (use --runner docker).")
        return 1
    # host: tools (bwa-mem2 and bwa, samtools, bcftools, fastp) must be on PATH. bwa-mem2 has no
    # apt package (the image installs the upstream release); without it, set ALIGNER=bwa.
    host_env = {
        **os.environ,
        **(env or {}),
        **(job.get("env") or {}),
        "STORAGE_BACKEND": "local",
        "METRICS_BACKEND": "local",
        "LOCAL_STORAGE_DIR": os.path.abspath(root),
    }
    return subprocess.run(
        [sys.executable, "tasks.py"] + job["cmd"][2:], cwd=os.path.join(REPO_ROOT, "app"), env=host_env
    ).returncode


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
    return {
        "kind": kind,
        "truth": len(truth),
        "tp": len(truth & called),
        "missed": sorted(truth - called),
        "extra": sorted(called - truth),
    }


def check_against_truth(root, sample_id, caller="bcftools"):
    """Print the truth comparison. Strict pass/fail for planted SNPs; report-only for GIAB truth."""
    counts = truth_counts(root, sample_id, caller)
    if counts is None:
        print(f"\nTruth check ({caller}): no truth/{sample_id}.json or truth/{sample_id}.vcf.gz under {root}")
        return False
    if counts["kind"] == "planted":
        print(
            f"\nTruth check ({caller}): {counts['tp']}/{counts['truth']} planted SNPs called, "
            f"{len(counts['extra'])} unexpected calls"
        )
        for item in counts["missed"]:
            print(f"  MISSED   pos={item[0]} {item[1]}>{item[2]}")
        for item in counts["extra"]:
            print(f"  EXTRA    pos={item[0]} {item[1]}>{item[2]}")
        return not counts["missed"] and not counts["extra"]
    # Real data: exact (pos, ref, alt) matching with no normalisation, so this is indicative only.
    tp, fn, fp = counts["tp"], len(counts["missed"]), len(counts["extra"])
    recall = tp / counts["truth"] if counts["truth"] else 0.0
    precision = tp / (tp + fp) if tp + fp else 0.0
    print(
        f"\nTruth check ({caller}, GIAB, report only): TP={tp} FN={fn} FP={fp} "
        f"recall={recall:.3f} precision={precision:.3f}"
    )
    return True


def compare_callers(root, sample_id, first="bcftools", second="deepvariant"):
    a = read_vcf_calls(vcf_path(root, first, sample_id))
    b = read_vcf_calls(vcf_path(root, second, sample_id))
    print(
        f"\nCaller comparison for {sample_id}: shared={len(a & b)} {first}-only={len(a - b)} {second}-only={len(b - a)}"
    )


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default=os.path.join(REPO_ROOT, "local_bucket"), help="local storage root")
    p.add_argument("--sample", help="sample ID; default: every raw_reads/*.fastq.gz under --root")
    p.add_argument(
        "--reference",
        help="reference name under reference/, as the cloud's S3 upload metadata reference names it "
        "(default: same as the Lambda); checked to exist before any job, like the state machine",
    )
    p.add_argument("--runner", choices=["docker", "host"], default="docker")
    p.add_argument("--image", default="geyser-app")
    p.add_argument("--caller", choices=CALLERS, default="bcftools")
    p.add_argument("--dv-image", default="geyser-deepvariant", help="DeepVariant image (app-deepvariant/)")
    p.add_argument(
        "--no-gpu",
        action="store_true",
        help="run DeepVariant and pangenome-aware DeepVariant without --gpus all (CPU images)",
    )
    p.add_argument(
        "--pg-image",
        default="geyser-deepvariant-pangenome",
        help="pangenome-aware DeepVariant image (app-deepvariant/ built with "
        "DV_TAG=pangenome_aware_deepvariant-1.10.0-gpu, or without -gpu for CPU)",
    )
    p.add_argument(
        "--pangenome",
        metavar="GBZ",
        help=f"pangenome graph under pangenome/, for --caller pangenome (default: {DEFAULT_PANGENOME}) and "
        "--aligner giraffe (default: <reference stem>.gbz)",
    )
    p.add_argument(
        "--aligner",
        choices=ALIGNERS,
        help="align stage's aligner (sets ALIGNER for it; default: $ALIGNER, else bwa-mem2). giraffe runs in "
        "--giraffe-image",
    )
    p.add_argument("--giraffe-image", default="geyser-giraffe", help="image for --aligner giraffe (app-giraffe/)")
    p.add_argument(
        "--pg-shm-size",
        default=DEFAULT_PANGENOME_SHM,
        metavar="SIZE",
        help="--shm-size for the pangenome container (default: %(default)s, as in Google's case studies)",
    )
    p.add_argument(
        "--dv-memory",
        metavar="SIZE",
        help="memory cap for the DeepVariant and pangenome containers, e.g. 12g. A whole chromosome can "
        "otherwise take most of the Docker VM; 16 make_examples shards thrashed under 8g",
    )
    p.add_argument(
        "--skip-to-variants",
        action="store_true",
        help="start at variant calling, using an existing alignments/<sample>.cram (or .bam)",
    )
    p.add_argument(
        "--shard-window",
        type=int,
        metavar="BP",
        help="scatter-gather variant calling: one bcftools job per region of BP bases, then a merge into the "
        "usual VCF (what the Map state will run; #108). Default: one variants job",
    )
    p.add_argument(
        "--chunks",
        type=int,
        metavar="N",
        help="align through v1.4's large route: preflight into N chunks, align each, merge (default: one align job)",
    )
    p.add_argument("--check", action="store_true", help="compare each VCF with truth/<sample>.*")
    p.add_argument("--no-gate", action="store_true", help="skip the quality gate after variant calling")
    p.add_argument(
        "--gate-profile",
        choices=GATE_PROFILES,
        help="the gate's thresholds, as the cloud's gate_profile input (S3 metadata gate-profile) names them; "
        "default: synthetic for make_test_data.py samples, else the built-in human WGS thresholds",
    )
    a = p.parse_args(argv)
    if a.chunks is not None and not 1 <= a.chunks <= 999:
        p.error("--chunks must be 1-999")
    if a.chunks and a.skip_to_variants:
        p.error("--chunks aligns; it can't be combined with --skip-to-variants")
    if a.shard_window is not None and a.shard_window < 1:
        p.error("--shard-window must be >= 1")

    trigger = load_trigger()
    keys = sample_keys(trigger, a.root, a.sample)
    if not keys:
        print(f"No inputs found under {a.root}/raw_reads/. Run scripts/make_test_data.py first.")
        return 1

    callers = CALLER_SETS[a.caller]
    # $ALIGNER=giraffe without --aligner must still pick the giraffe image: geyser-app has no vg.
    # One graph per run: --pangenome, else a GIRAFFE_GRAPH set on the host, names it for both the aligner and
    # the pangenome caller; without either, giraffe aligns to the caller's default graph when that caller runs
    # (and to <reference stem>.gbz, tasks.py's default, when it doesn't).
    graph = a.pangenome or os.environ.get("GIRAFFE_GRAPH", "").strip() or None
    giraffe_graph = graph or (DEFAULT_PANGENOME if "pangenome" in callers else None)
    align_image, align_env = align_settings(
        a.aligner or os.environ.get("ALIGNER", "").strip() or None, giraffe_graph, a.giraffe_image
    )
    ok = True
    for key in keys:
        payload = trigger.build_pipeline_input(key, a.reference)
        sample_id, reference = payload["srr_id"], payload["reference_name"]
        print(f"\n=== Pipeline input: {json.dumps(payload)} ===")
        problem = reference_problem(a.root, reference)
        if problem:  # as the state machine does: before any job
            print(f"\nFAILED before any job for {sample_id}: {problem}")
            ok = False
            continue
        started = time.time()
        jobs = pipeline_jobs(
            sample_id,
            reference,
            a.caller,
            a.image,
            a.dv_image,
            gpus=not a.no_gpu,
            skip_to_variants=a.skip_to_variants,
            pg_image=a.pg_image,
            pangenome=graph or DEFAULT_PANGENOME,
            pg_shm=a.pg_shm_size,
            # Only when there is a bcftools stage to shard (not --caller deepvariant/pangenome).
            regions=local_regions(a.root, reference, a.shard_window)
            if a.shard_window and "bcftools" in CALLER_SETS[a.caller]
            else None,
            shard_window=a.shard_window,
            gate=not a.no_gate,
            gate_config=gate_config_for(a.root, sample_id, a.runner, a.gate_profile),
            align_image=align_image,
            align_env=align_env,
            chunks=a.chunks,
        )
        run_name = run_env(sample_id, a.image)["RUN_NAME"]
        for job in jobs:
            print(f"\n>>> [{job['image']}] {' '.join(job['cmd'])}", flush=True)
            env = {**run_env(sample_id, job["image"]), "RUN_NAME": run_name}
            if run_job(job, a.runner, a.root, a.dv_memory, env) != 0:
                print(f"\nFAILED at stage '{job['name']}' for {sample_id} (in the cloud this goes to Notify_Failure)")
                ok = False
                break
        else:
            outputs = ", ".join(vcf_path(a.root, c, sample_id) for c in callers)
            print(f"\n=== {sample_id} finished in {time.time() - started:.1f}s; VCF at {outputs} ===")
            if a.check:
                for caller in callers:
                    ok = check_against_truth(a.root, sample_id, caller) and ok
            for first, second in zip(callers, callers[1:], strict=False):
                compare_callers(a.root, sample_id, first, second)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
