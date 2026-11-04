#!/usr/bin/env python3
"""Build a reference genome's FASTA index and bwa-mem2 index in AWS Batch and store them under reference/.

The align stage builds a missing index itself, which is fine for one chromosome but not for a whole genome:
bwa-mem2 index needs about 28 GB of RAM per Gb of reference (~87 GB for GRCh38) and an hour or more. This
submits that build once, as a Batch job on the large-disk job definition (genome_scale.tf, 200 GiB disk), in
the pipeline's own image (geyser-app: samtools, bwa-mem2, app/storage.py, app/tasks.py). The job:

  1. refuses to start if reference/<name> already exists (--force to rebuild);
  2. downloads --url, checking its MD5 (--md5, of the file as downloaded) while it streams;
  3. gunzips it if the URL ends in .gz;
  4. samtools faidx, checking the contig count (--expect-contigs);
  5. checks the first contig starts with tasks.BWA_MEM2_SAFE_N_PREFIX Ns, or the align stage would ignore a
     bwa-mem2 index and use bwa (bwa-mem2#262), so the build would be wasted;
  6. bwa-mem2 index;
  7. uploads the .fai and the bwa-mem2 index files (tasks.ALIGNER_INDEX_EXTS) FIRST and the FASTA LAST, so
     reference/<name> existing means the whole set is there.

    python scripts/build_reference_index.py --dry-run          # print the submit-job request, no AWS calls
    python scripts/build_reference_index.py                    # submit it (GRCh38 no-alt analysis set)
    python scripts/build_reference_index.py --url ... --md5 ... --name my_ref.fa --expect-contigs 25

The defaults build NCBI's GRCh38 no-alt analysis set (GCA_000001405.15, UCSC contig names, 195 contigs) as
reference/GRCh38_no_alt.fa. Parameters reach the job as environment variables, so the container script
itself never changes (a test pins it). The job runs on Fargate Spot and can be interrupted: the job
definition retries an interruption up to 3 attempts in all, and each attempt starts again from the download.
See docs/operations.md, "Build a whole-genome reference index".
"""

import argparse
import json
import os
import re
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_APP = os.path.join(REPO_ROOT, "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from tasks import ALIGNER_INDEX_EXTS, REFERENCE_PREFIX  # noqa: E402

DEFAULT_REGION = "eu-west-2"
DEFAULT_URL = (
    "https://ftp.ncbi.nlm.nih.gov/genomes/all/GCA/000/001/405/GCA_000001405.15_GRCh38/"
    "seqs_for_alignment_pipelines.ucsc_ids/GCA_000001405.15_GRCh38_no_alt_analysis_set.fna.gz"
)
DEFAULT_MD5 = "a08035b6a6e31780e96a34008ff21bd6"
DEFAULT_NAME = "GRCh38_no_alt.fa"
DEFAULT_EXPECT_CONTIGS = 195
DEFAULT_QUEUE = "geyser-job-queue"
DEFAULT_JOB_DEFINITION = "geyser-app-large-job"
# Fargate's largest task: 16 vCPU / 120 GiB. bwa-mem2 index needs ~87 GB for GRCh38.
DEFAULT_VCPUS = 16
DEFAULT_MEMORY_MIB = 122880
DEFAULT_TIMEOUT_SECONDS = 14400
ALIGNER = "bwa-mem2"

# Fargate's valid memory per vCPU count (MiB): (min, max, step).
FARGATE_MEMORY_MIB = {
    1: (2048, 8192, 1024),
    2: (4096, 16384, 1024),
    4: (8192, 30720, 1024),
    8: (16384, 61440, 4096),
    16: (32768, 122880, 8192),
}

# Runs in the geyser-app image (WORKDIR /app). Everything variable comes from the REF_* environment, which the
# job's containerOverrides set; the Python steps import the image's own tasks.py and storage.py.
CONTAINER_SCRIPT = r"""set -euo pipefail
export APP="$PWD"
W=/tmp/refbuild
mkdir -p "$W"
cd "$W"
echo "== $(date -u +%FT%TZ) check reference/$REF_NAME is not already stored"
python - <<'PY'
import os, sys
sys.path.insert(0, os.environ["APP"])
import storage
key = "reference/" + os.environ["REF_NAME"]
if storage.storage_from_env().exists(key) and os.environ.get("REF_FORCE") != "1":
    sys.exit(f"{key} already exists; rerun with --force to rebuild it")
print("ok:", key, "is free (or --force)")
PY
echo "== $(date -u +%FT%TZ) download $REF_URL"
python - <<'PY'
import hashlib, os, urllib.request
h = hashlib.md5()
with urllib.request.urlopen(os.environ["REF_URL"], timeout=120) as r, open("download", "wb") as f:
    while True:
        b = r.read(1 << 22)
        if not b:
            break
        h.update(b)
        f.write(b)
d = h.hexdigest()
if d != os.environ["REF_MD5"]:
    raise SystemExit(f"md5 mismatch: got {d}, expected {os.environ['REF_MD5']}")
print("md5 ok", d, os.path.getsize("download"), "bytes")
PY
case "$REF_URL" in
  *.gz) gunzip -c download > "$REF_NAME"; rm download ;;
  *) mv download "$REF_NAME" ;;
esac
echo "== $(date -u +%FT%TZ) samtools faidx"
samtools faidx "$REF_NAME"
CONTIGS=$(wc -l < "$REF_NAME.fai")
if [ "$CONTIGS" -ne "$REF_EXPECT_CONTIGS" ]; then
  echo "contig count $CONTIGS, expected $REF_EXPECT_CONTIGS" >&2
  exit 1
fi
awk '{n += $2} END {printf "contigs %d, %.3f Gb\n", NR, n / 1e9}' "$REF_NAME.fai"
echo "== $(date -u +%FT%TZ) check bwa-mem2 will be used on this reference (bwa-mem2#262 guard)"
python - <<'PY'
import os, sys
sys.path.insert(0, os.environ["APP"])
import tasks
if tasks.effective_aligner("bwa-mem2", os.environ["REF_NAME"]) != "bwa-mem2":
    sys.exit("the align stage would use bwa on this reference, not bwa-mem2: a bwa-mem2 index is useless")
print("ok: first contig starts with", tasks.BWA_MEM2_SAFE_N_PREFIX, "Ns")
PY
echo "== $(date -u +%FT%TZ) bwa-mem2 index"
grep -E "^(MemTotal|MemAvailable):" /proc/meminfo || true
bwa-mem2 index "$REF_NAME"
echo "== $(date -u +%FT%TZ) index done"
ls -l
echo "== $(date -u +%FT%TZ) upload: indexes first, the FASTA last"
python - <<'PY'
import os, sys
sys.path.insert(0, os.environ["APP"])
import storage
s = storage.storage_from_env()
name = os.environ["REF_NAME"]
for ext in os.environ["REF_UPLOAD_EXTS"].split() + [""]:
    s.upload(name + ext, "reference/" + name + ext)
    print("uploaded", name + ext, os.path.getsize(name + ext), flush=True)
PY
echo "== $(date -u +%FT%TZ) all done"
"""


def upload_exts() -> list:
    """The files uploaded before the FASTA: its .fai, then bwa-mem2's index (the align stage's list)."""
    return [".fai", *ALIGNER_INDEX_EXTS[ALIGNER]]


def check_fargate_size(vcpus: int, memory_mib: int) -> None:
    """ValueError unless (vcpus, memory_mib) is a size Fargate can run."""
    if vcpus not in FARGATE_MEMORY_MIB:
        raise ValueError(f"--vcpus {vcpus}: Fargate offers {sorted(FARGATE_MEMORY_MIB)}")
    low, high, step = FARGATE_MEMORY_MIB[vcpus]
    if not (low <= memory_mib <= high and (memory_mib - low) % step == 0):
        raise ValueError(f"--memory-mib {memory_mib}: with {vcpus} vCPU Fargate takes {low}-{high} in steps of {step}")


def check_name(name: str) -> None:
    """ValueError unless name is a plain file name (it becomes reference/<name> and a local path)."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) or name.endswith(".gz"):
        raise ValueError(f"--name {name!r}: a plain, uncompressed FASTA file name like GRCh38_no_alt.fa")


def job_name(name: str, now: float = None) -> str:
    """IndexBuild-<name without extension, Batch-safe>-<UTC time>."""
    stem = re.sub(r"[^A-Za-z0-9_-]", "-", os.path.splitext(name)[0])
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    return f"IndexBuild-{stem}-{stamp}"[:128]


def submit_request(a, now: float = None) -> dict:
    """The batch.submit_job request for these arguments."""
    check_name(a.name)
    check_fargate_size(a.vcpus, a.memory_mib)
    if not re.fullmatch(r"[0-9a-f]{32}", a.md5):
        raise ValueError(f"--md5 {a.md5!r}: 32 lower-case hex digits")
    if a.expect_contigs < 1:
        raise ValueError("--expect-contigs must be at least 1")
    env = {
        "REF_URL": a.url,
        "REF_MD5": a.md5,
        "REF_NAME": a.name,
        "REF_EXPECT_CONTIGS": str(a.expect_contigs),
        "REF_UPLOAD_EXTS": " ".join(upload_exts()),
        "REF_FORCE": "1" if a.force else "0",
    }
    return {
        "jobName": a.job_name or job_name(a.name, now),
        "jobQueue": a.queue,
        "jobDefinition": a.job_definition,
        "containerOverrides": {
            "command": ["bash", "-c", CONTAINER_SCRIPT],
            "environment": [{"name": k, "value": v} for k, v in env.items()],
            "resourceRequirements": [
                {"type": "VCPU", "value": str(a.vcpus)},
                {"type": "MEMORY", "value": str(a.memory_mib)},
            ],
        },
        "timeout": {"attemptDurationSeconds": a.timeout_seconds},
    }


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--url", default=DEFAULT_URL, help="FASTA to download (.gz is gunzipped); default GRCh38 no-alt")
    p.add_argument("--md5", default=DEFAULT_MD5, help="MD5 of the file as downloaded")
    p.add_argument("--name", default=DEFAULT_NAME, help=f"stored as {REFERENCE_PREFIX}<name> (default {DEFAULT_NAME})")
    p.add_argument("--expect-contigs", type=int, default=DEFAULT_EXPECT_CONTIGS, help="contigs in the .fai")
    p.add_argument("--force", action="store_true", help=f"rebuild even if {REFERENCE_PREFIX}<name> exists")
    p.add_argument("--queue", default=DEFAULT_QUEUE)
    p.add_argument("--job-definition", default=DEFAULT_JOB_DEFINITION, help="needs room for the build (200 GiB)")
    p.add_argument("--job-name", help="default IndexBuild-<name>-<UTC time>")
    p.add_argument("--vcpus", type=int, default=DEFAULT_VCPUS)
    p.add_argument("--memory-mib", type=int, default=DEFAULT_MEMORY_MIB, help="~28 GB per Gb of reference")
    p.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="per attempt")
    p.add_argument("--region", default=os.environ.get("AWS_REGION", DEFAULT_REGION))
    p.add_argument("--dry-run", action="store_true", help="print the submit-job request; no AWS calls")
    return p.parse_args(argv)


def main(argv=None, batch_client=None) -> int:
    a = parse_args(argv)
    try:
        request = submit_request(a)
    except ValueError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    if a.dry_run:
        print(json.dumps(request, indent=2))
        return 0
    if batch_client is None:
        import boto3  # noqa: PLC0415 - only for real runs; tests pass a stubbed client

        batch_client = boto3.client("batch", region_name=a.region)
    response = batch_client.submit_job(**request)
    print(f"submitted {response['jobName']}: job {response['jobId']} on {a.queue}")
    print(f"watch: aws batch describe-jobs --jobs {response['jobId']} --region {a.region}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
