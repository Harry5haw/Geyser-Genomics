# app/tasks.py
"""Pipeline stages: decompress -> qc -> align -> variants.

Each stage is run as its own container job (AWS Batch in the cloud, `docker run`
locally) with:   python tasks.py <stage> <sample_id> [reference_name]

Stages communicate only through storage keys:
    raw_reads/{id}.fastq.gz -> decompressed/{id}.fastq
        -> qc_reports/{id}_fastqc.{html,zip}
        -> alignments/{id}.bam (sorted, duplicates marked) -> variants/{id}.vcf.gz
    reference/{reference_name} (+ optional .fai / aligner indexes, see ALIGNER_INDEX_EXTS)

Paired-end samples have two raw files, raw_reads/{id}_R1.fastq.gz and {id}_R2.fastq.gz, and
every per-read key before alignment comes in an _R1/_R2 pair (see keys_for). Each stage detects
the layout from which of its input keys exist (sample_layout), so the CLI and the Step Functions
input are the same for both. The trigger Lambda parses the same _R1/_R2 names (tests check they agree).

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
from storage import ForwardOnlyStream, ObjectNotFound, storage_from_env

REFERENCE_PREFIX = "reference/"
# Aligners the align stage can run, chosen by the ALIGNER env var. bwa-mem2 is the default: the
# same algorithm as bwa mem and, apart from the case below, the same alignments, faster on
# AVX2/AVX-512 CPUs. bwa stays for comparisons (the pangenome benchmark, #51). Each needs its own
# index next to the FASTA, built by `<aligner> index` and stored under reference/ with these
# extensions (.amb/.ann/.pac are shared; bwa-mem2's .0123 and .bwt.2bit.64 replace .bwt and .sa).
ALIGNER_INDEX_EXTS = {
    "bwa-mem2": [".0123", ".amb", ".ann", ".bwt.2bit.64", ".pac"],
    "bwa": [".amb", ".ann", ".bwt", ".pac", ".sa"],
}
DEFAULT_ALIGNER = "bwa-mem2"
# bwa-mem2 (2.2.1 and 2.3) places reads that start in the first few bases of the FIRST contig of
# the index at position 1, shifted, so they show up as false SNPs (upstream bwa-mem2#262, open;
# on the synthetic data 3 of 10,000 reads, 5 false calls at positions 2-9; every other read is
# identical to bwa). Human chromosomes start with long N gaps (chr1 10 kb, chr20 60 kb), where no
# read aligns, so they are unaffected. When the first contig does not start with at least this
# many Ns (bacteria, viruses, plasmids, the synthetic genome), the align stage uses bwa instead.
BWA_MEM2_SAFE_N_PREFIX = 1000
# samtools sort RAM budget in the align stage (see align_command).
SORT_MAX_THREADS = 4
SORT_MEMORY_PER_THREAD = "512M"

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


SINGLE, PAIRED = "single", "paired"
MATES = ("R1", "R2")
# Keys that exist once per read file: plain for single-end, <name>_R1/<name>_R2 for paired-end.
PER_MATE_KEYS = ("raw", "fastq", "qc_html", "qc_zip")


class LayoutError(ValueError):
    """The raw reads in storage don't form one valid sample (e.g. only R1, or both layouts)."""


def keys_for(sample_id: str, layout: str = SINGLE) -> dict:
    """Single source of truth for every storage key a sample touches.

    Paired-end samples get raw_R1/raw_R2, fastq_R1/fastq_R2, ... instead of raw, fastq, ...
    (FastQC names its reports after the input file, so {id}_R1.fastq -> {id}_R1_fastqc.html).
    Use keys_of() to get a stage's keys without caring about the layout.
    """
    if layout not in (SINGLE, PAIRED):
        raise ValueError(f"Unknown layout {layout!r}")
    stems = {None: sample_id} if layout == SINGLE else {m: f"{sample_id}_{m}" for m in MATES}
    keys = {}
    for mate, stem in stems.items():
        suffix = f"_{mate}" if mate else ""
        keys[f"raw{suffix}"] = f"raw_reads/{stem}.fastq.gz"
        keys[f"fastq{suffix}"] = f"decompressed/{stem}.fastq"
        keys[f"qc_html{suffix}"] = f"qc_reports/{stem}_fastqc.html"
        keys[f"qc_zip{suffix}"] = f"qc_reports/{stem}_fastqc.zip"
    keys["bam"] = f"alignments/{sample_id}.bam"
    keys["vcf"] = f"variants/{sample_id}.vcf.gz"
    return keys


def keys_of(keys: dict, name: str) -> list:
    """['raw_reads/X.fastq.gz'] or ['raw_reads/X_R1.fastq.gz', 'raw_reads/X_R2.fastq.gz'] for name='raw'."""
    if name in keys:
        return [keys[name]]
    return [keys[f"{name}_{m}"] for m in MATES]


def all_raw_keys(sample_id: str) -> list:
    """Every raw key a sample ID could use, in either layout: anything here means the ID is taken."""
    return keys_of(keys_for(sample_id, SINGLE), "raw") + keys_of(keys_for(sample_id, PAIRED), "raw")


def layout_from_keys(sample_id: str, exists, name: str = "raw") -> str:
    """'single' or 'paired', from which of a sample's `name` keys exist ('raw' or 'fastq'). exists(key) -> bool.

    Each stage asks about its own inputs (decompress: raw, qc/align: fastq), so it never depends
    on an earlier stage's files still being there. Fails loudly rather than guessing: a lone mate
    (its partner not uploaded yet) or a sample present both ways is an error, not a silent
    single-end run.
    """
    single = exists(keys_for(sample_id, SINGLE)[name])
    mates = [m for m in MATES if exists(keys_for(sample_id, PAIRED)[f"{name}_{m}"])]
    if single and mates:
        raise LayoutError(f"{sample_id} has both single-end and paired-end ({'/'.join(mates)}) {name} files")
    if single:
        return SINGLE
    if len(mates) == len(MATES):
        return PAIRED
    if mates:
        missing = [m for m in MATES if m not in mates][0]
        raise LayoutError(f"{sample_id}: {name} {mates[0]} exists but {missing} is missing")
    raise LayoutError(f"{sample_id}: no {name} files ({keys_for(sample_id)[name]} or _R1/_R2)")


def layout_from_raw(sample_id: str, exists) -> str:
    """The layout a sample was uploaded with (what the trigger and the web UI go by)."""
    return layout_from_keys(sample_id, exists, "raw")


def sample_layout(sample_id: str, name: str = "raw") -> str:
    return layout_from_keys(sample_id, storage().exists, name)


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


def aligner_from_env() -> str:
    """The aligner named by ALIGNER (default bwa-mem2). Unknown names fail before any download."""
    name = os.environ.get("ALIGNER", "").strip() or DEFAULT_ALIGNER
    if name not in ALIGNER_INDEX_EXTS:
        raise ValueError(f"ALIGNER={name!r} is not one of {sorted(ALIGNER_INDEX_EXTS)}")
    return name


def first_contig_prefix(fasta_path: str, length: int) -> str:
    """The first `length` bases of the first sequence in a FASTA (plain or gzipped), upper-case."""
    opener = gzip.open if fasta_path.endswith(".gz") else open
    seen_header, parts, total = False, [], 0
    with opener(fasta_path, "rt") as fh:
        for line in fh:
            if line.startswith(">"):
                if seen_header:
                    break  # the first contig ended before `length` bases
                seen_header = True
                continue
            parts.append(line.strip())
            total += len(parts[-1])
            if total >= length:
                break
    return "".join(parts)[:length].upper()


def effective_aligner(requested: str, local_ref: str) -> str:
    """The aligner to run on this reference: bwa instead of bwa-mem2 where bwa-mem2#262 would bite."""
    if requested != "bwa-mem2":
        return requested
    prefix = first_contig_prefix(local_ref, BWA_MEM2_SAFE_N_PREFIX)
    if len(prefix) == BWA_MEM2_SAFE_N_PREFIX and set(prefix) == {"N"}:
        return requested
    print(f"WARNING: the first contig of {os.path.basename(local_ref)} does not start with "
          f"{BWA_MEM2_SAFE_N_PREFIX} Ns; bwa-mem2 misplaces reads there (bwa-mem2#262), "
          f"so aligning with bwa instead", flush=True)
    return "bwa"


def ensure_reference_local(reference_name: str, dest_dir: str, need_bwa_index: bool = True,
                           aligner: str = None) -> str:
    """Fetch reference/<name> and its .fai (built if missing). Returns the local FASTA path.

    need_bwa_index: also prepare the aligner's index via prepare_aligner (so the bwa-mem2#262 guard
    applies); aligner defaults to aligner_from_env(). Variant calling only needs the .fai; skipping
    the aligner index there saves minutes on a full chromosome.
    """
    fasta_key = f"{REFERENCE_PREFIX}{reference_name}"
    local_ref = os.path.join(dest_dir, reference_name)
    if need_bwa_index:
        aligner = aligner or aligner_from_env()
        if aligner not in ALIGNER_INDEX_EXTS:  # fail before the (large) download
            raise ValueError(f"Unknown aligner {aligner!r}; expected one of {sorted(ALIGNER_INDEX_EXTS)}")

    try:
        print(f"Fetching reference {storage().describe(fasta_key)}")
        storage().download(fasta_key, local_ref)
    except ObjectNotFound as err:
        raise FileNotFoundError(f"Reference FASTA missing: {storage().describe(fasta_key)}") from err

    _fetch_or_build(fasta_key, local_ref, [".fai"], ["samtools", "faidx", local_ref], "FASTA index")
    if need_bwa_index:
        prepare_aligner(reference_name, local_ref, aligner)
    return local_ref


def prepare_aligner(reference_name: str, local_ref: str, requested: str) -> str:
    """Choose the aligner for this reference (effective_aligner) and fetch or build its index.

    Returns the aligner to run. The only way to an aligner index, so the guard can't be skipped.
    """
    aligner = effective_aligner(requested, local_ref)
    _ensure_aligner_index(reference_name, local_ref, aligner)
    return aligner


def _ensure_aligner_index(reference_name: str, local_ref: str, aligner: str) -> None:
    """Fetch the aligner's stored index next to local_ref, or build it if any file is missing.

    Building in the job is fine for one chromosome. bwa-mem2 index needs ~28 bytes of RAM per
    reference base (~90 GB for a whole genome), so build that once elsewhere and upload it under
    reference/.
    """
    _fetch_or_build(f"{REFERENCE_PREFIX}{reference_name}", local_ref, ALIGNER_INDEX_EXTS[aligner],
                    [aligner, "index", local_ref], f"{aligner} index")


def _fetch_or_build(fasta_key: str, local_ref: str, exts, build_cmd, what: str) -> None:
    for ext in exts:
        try:
            storage().download(fasta_key + ext, local_ref + ext)
        except ObjectNotFound:
            pass  # built below
    if any(not os.path.exists(local_ref + ext) for ext in exts):
        print(f"{what} missing; running {' '.join(build_cmd[:2])}")
        subprocess.run(build_cmd, check=True)


@timed_stage("Decompress")
def decompress_task(sample_id):
    """Stream-decompress each raw_reads/ file (one, or R1 and R2) to decompressed/ without staging on disk."""
    keys = keys_for(sample_id, sample_layout(sample_id))
    for raw, fastq in zip(keys_of(keys, "raw"), keys_of(keys, "fastq")):
        print(f"Decompressing {storage().describe(raw)} -> {storage().describe(fastq)}")
        source = storage().open_read(raw)
        try:
            with gzip.GzipFile(fileobj=source, mode="rb") as unzipped:
                # GzipFile claims it can seek even over a forward-only S3 body; uploaders that
                # believe it rewind the body and fail (#71). ForwardOnlyStream stops that.
                storage().upload_stream(ForwardOnlyStream(unzipped), fastq)
        finally:
            source.close()


@timed_stage("QualityControl")
def qc_task(sample_id):
    """FastQC on each read file. Reports are named after the input: {id}_fastqc.* or {id}_R1_fastqc.*."""
    keys = keys_for(sample_id, sample_layout(sample_id, "fastq"))
    wd = work_dir(sample_id)
    try:
        out_dir = os.path.join(wd, "qc")
        os.makedirs(out_dir)
        fastqs = []
        for key in keys_of(keys, "fastq"):
            fastqs.append(os.path.join(wd, os.path.basename(key)))
            storage().download(key, fastqs[-1])
        # FastQC can hang (not exit) if its report thread crashes, so bound it. Matches the
        # Step Functions timeout for this stage. One thread per file (-t) processes R1 and R2 at
        # the same time, so a paired sample fits the same timeout as a single-end one
        # (FastQC reserves ~250 MB per thread; 2 fit the 4 GB job easily).
        timeout = int(os.environ.get("QC_TIMEOUT_SECONDS", "1800"))
        subprocess.run(["fastqc", "-t", str(len(fastqs)), *fastqs, "-o", out_dir], check=True, timeout=timeout)
        for name in ("qc_html", "qc_zip"):
            for key in keys_of(keys, name):
                storage().upload(os.path.join(out_dir, os.path.basename(key)), key)
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def align_command(ref: str, fastqs, bam: str, threads: int, tmp_dir: str, sample_id: str,
                  aligner: str = DEFAULT_ALIGNER) -> str:
    """Shell pipeline: bwa mem -> [fixmate] -> coordinate sort -> mark (not remove) duplicates.

    fastqs is [reads] for single-end or [R1, R2] for paired-end. aligner is "bwa-mem2" (default)
    or "bwa": both take the same `mem` arguments and write the same records, so everything below
    applies to either.

    Paired-end: bwa mem gets both files and writes each pair's records together (name-grouped),
    which is what `samtools fixmate -m` needs. fixmate adds the mate tags (MC, ms) that markdup
    uses to treat a pair as one fragment: a pair is only a duplicate if BOTH ends match.

    Single-end: there are no mates, so fixmate is left out; markdup finds duplicates from
    position, strand and orientation alone. That also flags independent reads that merely start
    at the same place on the same strand: about 5% of reads on the synthetic 30x/150 bp data
    (which has no real duplicates), rising with depth. Paired-end input avoids this. markdup also
    warns that it cannot estimate library size without pairs; that warning is harmless.

    Duplicates are flagged (0x400), not removed: bcftools mpileup and DeepVariant (incl. the
    pangenome-aware model, #51) both skip flagged reads by default.

    The read group (-R) names the sample in the BAM, so callers that read SM report sample_id.
    fixmate and sort pass uncompressed BAM (-u) down the pipe so nothing is compressed twice; sort
    and markdup keep their temporary files in tmp_dir (the stage's work dir), and markdup prints
    its duplicate statistics (-s) to the job log.

    sort buffers about -m per thread in RAM before spilling to tmp_dir, and its default
    (768 MB/thread) on a 16-thread host is ~12 GB, alongside bwa and markdup in the same pipe.
    So sort gets at most SORT_MAX_THREADS threads of SORT_MEMORY_PER_THREAD each (~2 GB),
    which fits the 4 GB Fargate job and a shared local Docker VM.
    """
    fastqs = [fastqs] if isinstance(fastqs, str) else list(fastqs)
    if aligner not in ALIGNER_INDEX_EXTS:
        raise ValueError(f"Unknown aligner {aligner!r}; expected one of {sorted(ALIGNER_INDEX_EXTS)}")
    if len(fastqs) not in (1, 2):
        raise ValueError(f"align_command needs 1 (single-end) or 2 (paired-end) FASTQs, got {len(fastqs)}")
    sort_threads = max(1, min(threads, SORT_MAX_THREADS))
    tmp = os.path.join(tmp_dir, "tmp")
    read_group = rf"@RG\tID:{sample_id}\tSM:{sample_id}"  # literal backslash-t: bwa turns it into tabs
    stages = [f"{aligner} mem -t {threads} -R {shlex.quote(read_group)} {shlex.quote(ref)} "
              + " ".join(shlex.quote(f) for f in fastqs)]
    if len(fastqs) == 2:
        stages.append("samtools fixmate -m -u - -")
    stages.append(f"samtools sort -u -@ {sort_threads} -m {SORT_MEMORY_PER_THREAD} -T {shlex.quote(tmp + '.sort')} -")
    stages.append(f"samtools markdup -s -@ {threads} -T {shlex.quote(tmp + '.markdup')} - {shlex.quote(bam)}")
    return " | ".join(stages)


@timed_stage("Align")
def align_task(sample_id, reference_name):
    """bwa-mem2 (or ALIGNER=bwa) -> coordinate-sorted, duplicate-marked BAM, single- or paired-end."""
    aligner = aligner_from_env()  # a bad ALIGNER fails here, before any download
    keys = keys_for(sample_id, sample_layout(sample_id, "fastq"))
    wd = work_dir(sample_id)
    try:
        fastqs = []
        for key in keys_of(keys, "fastq"):
            fastqs.append(os.path.join(wd, os.path.basename(key)))
            storage().download(key, fastqs[-1])
        bam = os.path.join(wd, f"{sample_id}.bam")
        ref = ensure_reference_local(reference_name, wd, need_bwa_index=False)
        aligner = prepare_aligner(reference_name, ref, aligner)
        print(f"Aligning with {aligner}", flush=True)
        run_shell(align_command(ref, fastqs, bam, os.cpu_count() or 1, wd, sample_id, aligner))
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
