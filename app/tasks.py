# app/tasks.py
"""Pipeline stages: decompress -> qc -> align -> variants.

Each stage is run as its own container job (AWS Batch in the cloud, `docker run`
locally) with:   python tasks.py <stage> <sample_id> [reference_name]

Stages communicate only through storage keys:
    raw_reads/{id}.fastq.gz -> decompressed/{id}.fastq
        -> qc_reports/{id}_fastqc.{html,zip}
        -> alignments/{id}.bam (sorted, duplicates marked) -> variants/{id}.vcf.gz
    reference/{reference_name} (+ optional .fai / bwa indexes)

Where those keys live is chosen by STORAGE_BACKEND (see storage.py); metrics by
METRICS_BACKEND (see metrics.py). If you rename a stage or key, update the Step
Functions definition (infrastructure/step_functions.tf) and scripts/run_local.py too.
"""

import argparse
import gzip
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from functools import wraps

from metrics import metrics_from_env
from storage import ObjectNotFound, storage_from_env

REFERENCE_PREFIX = "reference/"
BWA_INDEX_EXTS = [".amb", ".ann", ".bwt", ".pac", ".sa"]

# Backends are created on first use so importing this module never needs AWS credentials.
_storage = None
_metrics = None


def storage():
    global _storage
    if _storage is None:
        _storage = storage_from_env()
    return _storage


def metrics():
    global _metrics
    if _metrics is None:
        _metrics = metrics_from_env()
    return _metrics


def keys_for(sample_id: str) -> dict:
    """Single source of truth for every storage key a sample touches."""
    return {
        "raw": f"raw_reads/{sample_id}.fastq.gz",
        "fastq": f"decompressed/{sample_id}.fastq",
        "qc_html": f"qc_reports/{sample_id}_fastqc.html",
        "qc_zip": f"qc_reports/{sample_id}_fastqc.zip",
        "bam": f"alignments/{sample_id}.bam",
        "vcf": f"variants/{sample_id}.vcf.gz",
    }


def run_shell(command: str) -> None:
    """Run a shell pipeline, failing if ANY command in it fails (not just the last)."""
    print(f"$ {command}", flush=True)
    subprocess.run(["bash", "-o", "pipefail", "-c", command], check=True)


def work_dir(sample_id: str) -> str:
    base = os.environ.get("WORK_DIR", tempfile.gettempdir())
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(prefix=f"geyser-{sample_id}-", dir=base)


def timed_stage(task_name):
    """Time a stage and record a Success/Failure metric. Metric errors never fail the stage."""
    def decorator(func):
        @wraps(func)
        def wrapper(sample_id, *args, **kwargs):
            print(f"--- Starting '{task_name}' for sample '{sample_id}' ---", flush=True)
            start = time.time()
            status = "Failure"
            try:
                result = func(sample_id, *args, **kwargs)
                status = "Success"
                return result
            finally:
                duration = time.time() - start
                print(f"--- '{task_name}' for '{sample_id}': {status} in {duration:.2f}s ---", flush=True)
                try:
                    metrics().record(task_name, sample_id, status, duration)
                except Exception as err:  # noqa: BLE001 - metrics are best effort
                    print(f"WARNING: could not record metric: {err}", flush=True)
        return wrapper
    return decorator


def ensure_reference_local(reference_name: str, dest_dir: str, need_bwa_index: bool = True) -> str:
    """Fetch reference/<name> and any stored indexes; build missing indexes. Returns the local FASTA path.

    Variant calling only needs the .fai; skipping the BWA index there saves minutes on a full chromosome.
    """
    fasta_key = f"{REFERENCE_PREFIX}{reference_name}"
    local_ref = os.path.join(dest_dir, reference_name)

    try:
        print(f"Fetching reference {storage().describe(fasta_key)}")
        storage().download(fasta_key, local_ref)
    except ObjectNotFound as err:
        raise FileNotFoundError(f"Reference FASTA missing: {storage().describe(fasta_key)}") from err

    for ext in [".fai"] + (BWA_INDEX_EXTS if need_bwa_index else []):
        try:
            storage().download(fasta_key + ext, local_ref + ext)
        except ObjectNotFound:
            pass  # built below

    if need_bwa_index and any(not os.path.exists(local_ref + ext) for ext in BWA_INDEX_EXTS):
        print("BWA index missing; running bwa index")
        subprocess.run(["bwa", "index", local_ref], check=True)
    if not os.path.exists(local_ref + ".fai"):
        print("FASTA index missing; running samtools faidx")
        subprocess.run(["samtools", "faidx", local_ref], check=True)
    return local_ref


@timed_stage("Decompress")
def decompress_task(sample_id):
    """Stream-decompress raw_reads/<id>.fastq.gz to decompressed/<id>.fastq without staging on disk."""
    keys = keys_for(sample_id)
    print(f"Decompressing {storage().describe(keys['raw'])} -> {storage().describe(keys['fastq'])}")
    source = storage().open_read(keys["raw"])
    try:
        with gzip.GzipFile(fileobj=source, mode="rb") as unzipped:
            storage().upload_stream(unzipped, keys["fastq"])
    finally:
        source.close()


@timed_stage("QualityControl")
def qc_task(sample_id):
    keys = keys_for(sample_id)
    wd = work_dir(sample_id)
    try:
        fastq = os.path.join(wd, f"{sample_id}.fastq")
        out_dir = os.path.join(wd, "qc")
        os.makedirs(out_dir)
        storage().download(keys["fastq"], fastq)
        # FastQC can hang (not exit) if its report thread crashes, so bound it. Matches the
        # Step Functions timeout for this stage.
        timeout = int(os.environ.get("QC_TIMEOUT_SECONDS", "1800"))
        subprocess.run(["fastqc", fastq, "-o", out_dir], check=True, timeout=timeout)
        storage().upload(os.path.join(out_dir, f"{sample_id}_fastqc.html"), keys["qc_html"])
        storage().upload(os.path.join(out_dir, f"{sample_id}_fastqc.zip"), keys["qc_zip"])
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def align_command(ref: str, fastq: str, bam: str, threads: int, tmp_dir: str) -> str:
    """Shell pipeline: bwa mem -> coordinate sort -> mark (not remove) PCR/optical duplicates.

    Every sample arrives as ONE FASTQ (raw_reads/<id>.fastq.gz) and bwa mem runs without -p,
    so every read is aligned single-end. `samtools fixmate -m` is therefore left out: it only
    adds mate tags (MC, ms) to name-grouped read PAIRS, and markdup only needs those tags for
    paired reads. Single-end duplicates are found from coordinate, strand and orientation
    alone. If paired-end input is ever added (two FASTQs, or interleaved with bwa mem -p),
    insert `samtools fixmate -m - -` between bwa and sort.

    Duplicates are flagged (0x400), not removed: bcftools mpileup skips flagged reads by
    default, and DeepVariant (incl. the pangenome-aware model, #51) expects a marked BAM.
    sort writes uncompressed BAM (-u) into the pipe so it is not compressed twice; both sort
    and markdup keep their temporary files in tmp_dir (the stage's work dir), and markdup
    prints its duplicate statistics (-s) to the job log. With single-end reads markdup also
    warns that it cannot estimate library size (that needs pairs); the warning is harmless.
    """
    t = str(threads)
    tmp = os.path.join(tmp_dir, "tmp")
    return (
        f"bwa mem -t {t} {shlex.quote(ref)} {shlex.quote(fastq)} "
        f"| samtools sort -u -@ {t} -T {shlex.quote(tmp + '.sort')} - "
        f"| samtools markdup -s -@ {t} -T {shlex.quote(tmp + '.markdup')} - {shlex.quote(bam)}"
    )


@timed_stage("Align")
def align_task(sample_id, reference_name):
    """bwa mem -> coordinate-sorted, duplicate-marked BAM (see align_command)."""
    keys = keys_for(sample_id)
    wd = work_dir(sample_id)
    try:
        fastq = os.path.join(wd, f"{sample_id}.fastq")
        bam = os.path.join(wd, f"{sample_id}.bam")
        storage().download(keys["fastq"], fastq)
        ref = ensure_reference_local(reference_name, wd)
        run_shell(align_command(ref, fastq, bam, os.cpu_count() or 1, wd))
        storage().upload(bam, keys["bam"])
    finally:
        shutil.rmtree(wd, ignore_errors=True)


@timed_stage("CallVariants")
def variants_task(sample_id, reference_name):
    keys = keys_for(sample_id)
    wd = work_dir(sample_id)
    try:
        bam = os.path.join(wd, f"{sample_id}.bam")
        vcf = os.path.join(wd, f"{sample_id}.vcf.gz")
        storage().download(keys["bam"], bam)
        ref = ensure_reference_local(reference_name, wd, need_bwa_index=False)
        run_shell(
            f"bcftools mpileup -f {shlex.quote(ref)} {shlex.quote(bam)} "
            f"| bcftools call -mv -Oz -o {shlex.quote(vcf)}"
        )
        storage().upload(vcf, keys["vcf"])
    finally:
        shutil.rmtree(wd, ignore_errors=True)


TASKS = {
    "decompress": (decompress_task, False),
    "qc": (qc_task, False),
    "align": (align_task, True),
    "variants": (variants_task, True),
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run one Geyser Genomics pipeline stage.")
    parser.add_argument("task_name", choices=sorted(TASKS))
    parser.add_argument("srr_id", help="Sample ID, e.g. SRR062634")
    parser.add_argument("reference_name", nargs="?", help="Reference FASTA name (align and variants), e.g. chr20.fa")
    args = parser.parse_args(argv)

    func, needs_reference = TASKS[args.task_name]
    if needs_reference:
        if not args.reference_name:
            parser.error(f"'{args.task_name}' requires reference_name")
        func(args.srr_id, args.reference_name)
    else:
        func(args.srr_id)
    print(f"Task '{args.task_name}' completed for sample '{args.srr_id}'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
