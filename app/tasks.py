# app/tasks.py
"""Pipeline stages: (qc and align, side by side) -> variants.

Each stage is run as its own container job (AWS Batch in the cloud, `docker run`
locally) with:   python tasks.py <stage> <sample_id> [reference_name]

Stages communicate only through storage keys:
    raw_reads/{id}.fastq.gz -> qc_reports/{id}.fastp.{html,json}     (qc: fastp, report only)
                            -> alignments/{id}.cram + .cram.crai     (align: sorted, duplicates marked)
    alignments/{id}.cram    -> variants/{id}.vcf.gz + .vcf.gz.tbi    (variants)
        or, scatter-gather: one intermediate/variant-parts/{id}/<run>/<region>.vcf.gz per region (variants --region),
        then variants --window concatenates them in reference order (see regions_from_lengths)
    stats/{id}/: flagstat.txt, mosdepth.{summary,global.dist}.txt (align); bcftools.stats.txt (variants)
    runs/{id}.provenance.json: the run record (inputs, reference MD5, tools, timings, stats; provenance.py)
    reference/{reference_name} (+ optional .fai / aligner indexes, see ALIGNER_INDEX_EXTS)

qc and align both read the gzipped upload directly (fastp, bwa-mem2 and bwa read gzip), so they don't
depend on each other: the state machine runs them as two branches of a Parallel state. There is no
decompressed copy; both stages fail on a truncated upload (tests/test_e2e_docker.py).

Paired-end samples have two raw files, raw_reads/{id}_R1.fastq.gz and {id}_R2.fastq.gz (see
keys_for); everything after them is one file per sample, the QC report included. qc and align detect the layout from
which raw keys exist (sample_layout), so the CLI and the Step Functions input are the same for
both. The trigger Lambda parses the same _R1/_R2 names (tests check they agree).

Where those keys live is chosen by STORAGE_BACKEND (see storage.py); metrics by
METRICS_BACKEND (see metrics.py). If you rename a stage or key, update the Step
Functions definition (infrastructure/state_machine.asl.json) and scripts/run_local.py too.
"""

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from functools import wraps

import provenance
from metrics import USAGE_LOG_PREFIX, metrics_from_env, usage_since, usage_snapshot
from storage import ObjectNotFound, storage_from_env

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
# fastp (QC) threads; it uses at most 16.
QC_MAX_THREADS = 16
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
# keys_for() name -> file under stats/{id}/. Names are the tools' own output names where they have one.
STATS_FILES = {
    "flagstat": "flagstat.txt",
    "mosdepth_summary": "mosdepth.summary.txt",
    "mosdepth_dist": "mosdepth.global.dist.txt",
    "bcftools_stats": "bcftools.stats.txt",
}
MOSDEPTH_MAX_THREADS = 4  # mosdepth -t only adds decompression threads; more doesn't help
MATES = ("R1", "R2")
# Keys that exist once per read file: plain for single-end, <name>_R1/<name>_R2 for paired-end.
# Only the raw reads: fastp writes one QC report for both mates of a pair.
PER_MATE_KEYS = ("raw",)


class LayoutError(ValueError):
    """The raw reads in storage don't form one valid sample (e.g. only R1, or both layouts)."""


def keys_for(sample_id: str, layout: str = SINGLE) -> dict:
    """Single source of truth for every storage key a sample touches.

    Paired-end samples get raw_R1/raw_R2 instead of raw; every other key is one per sample.
    Use keys_of() to get a stage's keys without caring about the layout.
    """
    if layout not in (SINGLE, PAIRED):
        raise ValueError(f"Unknown layout {layout!r}")
    stems = {None: sample_id} if layout == SINGLE else {m: f"{sample_id}_{m}" for m in MATES}
    keys = {}
    for mate, stem in stems.items():
        suffix = f"_{mate}" if mate else ""
        keys[f"raw{suffix}"] = f"raw_reads/{stem}.fastq.gz"
    keys["qc_html"] = f"qc_reports/{sample_id}.fastp.html"
    keys["qc_json"] = f"qc_reports/{sample_id}.fastp.json"
    # The align stage writes CRAM (reference-compressed, ~1/3 of a BAM) and its index; readers go
    # through alignment_input(), which also accepts a BAM: no stage writes one any more, but fetched
    # benchmark data (fetch_dv_testdata.py) and runs from before CRAM have alignments/{id}.bam.
    keys["cram"] = f"alignments/{sample_id}.cram"
    keys["crai"] = f"alignments/{sample_id}.cram.crai"
    keys["bam"] = f"alignments/{sample_id}.bam"
    keys["vcf"] = f"variants/{sample_id}.vcf.gz"
    keys["tbi"] = f"variants/{sample_id}.vcf.gz.tbi"
    # Consequence annotation (bcftools csq), written only when the reference has a GFF (annotate_vcf).
    keys["csq_vcf"] = f"variants/{sample_id}.csq.vcf.gz"
    keys["csq_tbi"] = f"variants/{sample_id}.csq.vcf.gz.tbi"
    # QC statistics (#101): written by align (flagstat, mosdepth) and variants (bcftools stats).
    for name, filename in STATS_FILES.items():
        keys[name] = f"stats/{sample_id}/{filename}"
    return keys


# keys_for() names a sample's alignment can be stored as, in the order readers prefer them. The one
# rule for "is this sample aligned, and from what": alignment_input, the web status and reconcile.
ALIGNMENT_FORMS = ("cram", "bam")


def alignment_input(sample_id: str, exists=None) -> str:
    """Key of the alignment a caller should read: the CRAM, else a BAM. exists(key) -> bool.

    CRAM first: it is what the align stage writes. A BAM is legacy or imported input (runs from
    before CRAM, fetch_dv_testdata.py). If both exist the CRAM wins and a note says so (normal for a
    sample re-aligned since CRAM; worth knowing if a BAM was imported on purpose). If there is no
    alignment at all, FileNotFoundError naming every key tried.
    """
    exists = exists or storage().exists
    keys = keys_for(sample_id)
    found = [keys[form] for form in ALIGNMENT_FORMS if exists(keys[form])]
    if not found:
        raise FileNotFoundError(
            f"No alignment for {sample_id}: none of " + ", ".join(storage().describe(keys[f]) for f in ALIGNMENT_FORMS)
        )
    if len(found) > 1:
        print(f"Note: {sample_id} has {' and '.join(found)}; reading {found[0]}", flush=True)
    return found[0]


def index_alignment(path: str) -> None:
    """samtools index a local CRAM or BAM (.crai / .bai next to it). Needs no reference, even for CRAM."""
    run_shell(f"samtools index -@ {os.cpu_count() or 1} {shlex.quote(path)}")


def fetch_alignment(sample_id: str, wd: str, need_index: bool = False, indexer=None) -> str:
    """Download the sample's alignment (alignment_input) into wd, keeping its name. Returns the local path.

    need_index: also provide an index, for callers that need random access (DeepVariant). A CRAM is
    always indexed here (indexer(path), default index_alignment), never paired with the stored
    .crai: if a re-run failed between its two uploads, that .crai can belong to the previous CRAM.
    A BAM is imported, never half-written by a stage, so its stored .bai is used when there is one.
    """
    key = alignment_input(sample_id)
    local = os.path.join(wd, os.path.basename(key))
    print(f"Fetching alignment {storage().describe(key)}", flush=True)
    storage().download(key, local)
    provenance.note_input(key, local)
    if need_index:
        if key.endswith(".bam"):
            try:
                storage().download(key + ".bai", local + ".bai")
                return local
            except ObjectNotFound:
                pass
        (indexer or index_alignment)(local)
    return local


def keys_of(keys: dict, name: str) -> list:
    """['raw_reads/X.fastq.gz'] or ['raw_reads/X_R1.fastq.gz', 'raw_reads/X_R2.fastq.gz'] for name='raw'."""
    if name in keys:
        return [keys[name]]
    return [keys[f"{name}_{m}"] for m in MATES]


def legacy_qc_keys(sample_id: str, layout: str = SINGLE) -> list:
    """FastQC's HTML report(s), from samples run before fastp replaced it (#100): one per read file."""
    stems = [sample_id] if layout == SINGLE else [f"{sample_id}_{m}" for m in MATES]
    return [f"qc_reports/{stem}_fastqc.html" for stem in stems]


def output_present(sample_id: str, layout: str, name: str, exists) -> bool:
    """Whether a sample has its `name` output (keys_for name). exists(key) -> bool.

    QC also counts as done when the sample has FastQC's reports from before #100, so samples that
    finished then don't look unfinished now. Their reports stay where they are; re-running
    `tasks.py qc <id>` writes the fastp ones.
    """
    if all(exists(k) for k in keys_of(keys_for(sample_id, layout), name)):
        return True
    return name == "qc_html" and all(exists(k) for k in legacy_qc_keys(sample_id, layout))


def all_raw_keys(sample_id: str) -> list:
    """Every raw key a sample ID could use, in either layout: anything here means the ID is taken."""
    return keys_of(keys_for(sample_id, SINGLE), "raw") + keys_of(keys_for(sample_id, PAIRED), "raw")


def layout_from_raw(sample_id: str, exists) -> str:
    """'single' or 'paired', from which raw reads exist. exists(key) -> bool.

    What qc, align, the trigger and the web UI all go by. Fails loudly rather than guessing: a lone
    mate (its partner not uploaded yet) or a sample present both ways is an error, not a silent
    single-end run.
    """
    single = exists(keys_for(sample_id, SINGLE)["raw"])
    mates = [m for m in MATES if exists(keys_for(sample_id, PAIRED)[f"raw_{m}"])]
    if single and mates:
        raise LayoutError(f"{sample_id} has both single-end and paired-end ({'/'.join(mates)}) raw files")
    if single:
        return SINGLE
    if len(mates) == len(MATES):
        return PAIRED
    if mates:
        missing = [m for m in MATES if m not in mates][0]
        raise LayoutError(f"{sample_id}: raw {mates[0]} exists but {missing} is missing")
    raise LayoutError(f"{sample_id}: no raw files ({keys_for(sample_id)['raw']} or _R1/_R2)")


def sample_layout(sample_id: str) -> str:
    return layout_from_raw(sample_id, storage().exists)


def run_shell(command: str) -> None:
    """Run a shell pipeline, failing if ANY command in it fails (not just the last)."""
    print(f"$ {command}", flush=True)
    subprocess.run(["bash", "-o", "pipefail", "-c", command], check=True)


def work_dir(sample_id: str) -> str:
    base = os.environ.get("WORK_DIR", tempfile.gettempdir())
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(prefix=f"geyser-{sample_id}-", dir=base)


def timed_stage(task_name, run_record=True):
    """Time a stage, record a Success/Failure metric and its run-record part (provenance.py).

    Neither the metric nor the run record can fail the stage.
    """

    def decorator(func):
        @wraps(func)
        def wrapper(sample_id, *args, **kwargs):
            print(f"--- Starting '{task_name}' for sample '{sample_id}' ---", flush=True)
            start = time.time()
            snapshot = usage_snapshot()
            status, error = "Failure", None
            if run_record:
                provenance.begin(task_name, sample_id)
            try:
                result = func(sample_id, *args, **kwargs)
                status = "Success"
                return result
            except BaseException as err:
                error = f"{type(err).__name__}: {err}"[:500]
                raise
            finally:
                duration = time.time() - start
                print(f"--- '{task_name}' for '{sample_id}': {status} in {duration:.2f}s ---", flush=True)
                try:
                    usage = usage_since(snapshot)
                except Exception as err:  # noqa: BLE001 - measuring is best effort
                    usage = None
                    print(f"WARNING: could not measure resource use: {err}", flush=True)
                if usage:
                    line = {"task": task_name, "sample_id": sample_id, "status": status, **usage}
                    print(USAGE_LOG_PREFIX + json.dumps(line), flush=True)
                try:
                    metrics().record(task_name, sample_id, status, duration, usage=usage)
                except Exception as err:  # noqa: BLE001 - metrics are best effort
                    print(f"WARNING: could not record metric: {err}", flush=True)
                try:
                    if run_record:
                        provenance.finish(storage(), status, duration, error, usage=usage)
                except Exception as err:  # noqa: BLE001 - provenance is best effort (finish never raises)
                    print(f"WARNING: could not write the run record: {err}", flush=True)

        return wrapper

    return decorator


def aligner_from_env() -> str:
    """The aligner named by ALIGNER (default bwa-mem2). Unknown names fail before any download."""
    return check_aligner(os.environ.get("ALIGNER", "").strip() or DEFAULT_ALIGNER)


def check_aligner(name: str) -> str:
    """name if it is an aligner the align stage knows (ALIGNER_INDEX_EXTS), else ValueError."""
    if name not in ALIGNER_INDEX_EXTS:
        raise ValueError(f"Unknown aligner {name!r} (ALIGNER); expected one of {sorted(ALIGNER_INDEX_EXTS)}")
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
    print(
        f"WARNING: the first contig of {os.path.basename(local_ref)} does not start with "
        f"{BWA_MEM2_SAFE_N_PREFIX} Ns; bwa-mem2 misplaces reads there (bwa-mem2#262), "
        f"so aligning with bwa instead",
        flush=True,
    )
    return "bwa"


def ensure_reference_local(reference_name: str, dest_dir: str, need_bwa_index: bool = True, aligner: str = None) -> str:
    """Fetch reference/<name> and its .fai (built if missing). Returns the local FASTA path.

    need_bwa_index: also prepare the aligner's index via prepare_aligner (so the bwa-mem2#262 guard
    applies); aligner defaults to aligner_from_env(). Variant calling only needs the .fai; skipping
    the aligner index there saves minutes on a full chromosome.
    """
    fasta_key = f"{REFERENCE_PREFIX}{reference_name}"
    local_ref = os.path.join(dest_dir, reference_name)
    if need_bwa_index:
        aligner = check_aligner(aligner) if aligner else aligner_from_env()  # before the (large) download

    try:
        print(f"Fetching reference {storage().describe(fasta_key)}")
        storage().download(fasta_key, local_ref)
    except ObjectNotFound as err:
        raise FileNotFoundError(f"Reference FASTA missing: {storage().describe(fasta_key)}") from err

    _fetch_or_build(fasta_key, local_ref, [".fai"], ["samtools", "faidx", local_ref], "FASTA index")
    provenance.note_reference(reference_name, local_ref)
    if need_bwa_index:
        prepare_aligner(reference_name, local_ref, aligner)
    return local_ref


def upload_output(local_path: str, key: str) -> None:
    """Upload a stage output and list it (with its size) in the run record."""
    storage().upload(local_path, key)
    provenance.note_output(key, local_path)


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
    _fetch_or_build(
        f"{REFERENCE_PREFIX}{reference_name}",
        local_ref,
        ALIGNER_INDEX_EXTS[aligner],
        [aligner, "index", local_ref],
        f"{aligner} index",
    )


def _fetch_or_build(fasta_key: str, local_ref: str, exts, build_cmd, what: str) -> None:
    """Download a stored index if every file of it is stored; otherwise build it.

    Checking first avoids downloading part of a set only to overwrite it (e.g. a reference/ with
    only bwa's index: bwa-mem2 index rewrites the shared .amb/.ann/.pac). The cloud reference/
    holds both indexes for chr20.fa since 2 Nov 2026.
    """
    if all(storage().exists(fasta_key + ext) for ext in exts):
        try:
            for ext in exts:
                storage().download(fasta_key + ext, local_ref + ext)
            return
        except ObjectNotFound:
            pass  # deleted in between: build it
    print(f"{what} missing; running {' '.join(build_cmd[:2])}")
    subprocess.run(build_cmd, check=True)


def download_raw_reads(keys: dict, wd: str) -> list:
    """Download the sample's gzipped read file(s) as-is into wd; returns the local paths (R1 first)."""
    paths = []
    for key in keys_of(keys, "raw"):
        paths.append(os.path.join(wd, os.path.basename(key)))
        storage().download(key, paths[-1])
        provenance.note_input(key, paths[-1])
    return paths


def qc_command(fastqs, html: str, json_path: str, threads: int, sample_id: str) -> list:
    """fastp in report-only mode: read quality, GC, duplication, adapters, overrepresented sequences.

    No -o/-O, so fastp writes no reads; and trimming and filtering are switched off (-A -Q -L -G),
    so the report describes the upload exactly as align reads it. Paired-end gives both files
    (-i/-I) and one report, with insert sizes from read overlap. The JSON report is the
    machine-readable one (#58).
    """
    fastqs = [fastqs] if isinstance(fastqs, str) else list(fastqs)
    if len(fastqs) not in (1, 2):
        raise ValueError(f"qc_command needs 1 (single-end) or 2 (paired-end) FASTQs, got {len(fastqs)}")
    cmd = ["fastp", "-i", fastqs[0]]
    if len(fastqs) == 2:
        cmd += ["-I", fastqs[1]]
    return cmd + [
        "--disable_adapter_trimming",
        "--disable_quality_filtering",
        "--disable_length_filtering",
        "--disable_trim_poly_g",
        "--thread",
        str(max(1, min(threads, QC_MAX_THREADS))),
        "--json",
        json_path,
        "--html",
        html,
        "--report_title",
        f"{sample_id} (fastp)",
    ]


@timed_stage("QualityControl")
def qc_task(sample_id):
    """fastp report (HTML + JSON) for the sample's gzipped read file(s): one report, either layout."""
    layout = sample_layout(sample_id)
    keys = keys_for(sample_id, layout)
    wd = work_dir(sample_id)
    try:
        fastqs = download_raw_reads(keys, wd)
        provenance.note(layout=layout)
        provenance.note_tools("fastp")
        html, json_path = (os.path.join(wd, os.path.basename(keys[k])) for k in ("qc_html", "qc_json"))
        # Bounded in case fastp ever hangs; keep in step with the stage's Step Functions timeout.
        # fastp fails on a truncated upload ("igzip: unexpected eof", exit 255) and writes no report.
        timeout = int(os.environ.get("QC_TIMEOUT_SECONDS", "3600"))
        subprocess.run(qc_command(fastqs, html, json_path, os.cpu_count() or 1, sample_id), check=True, timeout=timeout)
        upload_output(html, keys["qc_html"])
        upload_output(json_path, keys["qc_json"])
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def align_command(
    ref: str, fastqs, out: str, threads: int, tmp_dir: str, sample_id: str, aligner: str = DEFAULT_ALIGNER
) -> str:
    """Shell pipeline: bwa mem -> [fixmate] -> coordinate sort -> mark (not remove) duplicates -> out.

    out ending in .cram makes markdup write CRAM against ref (what the align stage stores);
    anything else is BAM. CRAM can only be decoded with the same reference, which every reader
    here passes (bcftools -f, DeepVariant --ref); its header records the reference's MD5s.

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
    check_aligner(aligner)
    if len(fastqs) not in (1, 2):
        raise ValueError(f"align_command needs 1 (single-end) or 2 (paired-end) FASTQs, got {len(fastqs)}")
    sort_threads = max(1, min(threads, SORT_MAX_THREADS))
    tmp = os.path.join(tmp_dir, "tmp")
    read_group = rf"@RG\tID:{sample_id}\tSM:{sample_id}"  # literal backslash-t: bwa turns it into tabs
    stages = [
        f"{aligner} mem -t {threads} -R {shlex.quote(read_group)} {shlex.quote(ref)} "
        + " ".join(shlex.quote(f) for f in fastqs)
    ]
    if len(fastqs) == 2:
        stages.append("samtools fixmate -m -u - -")
    stages.append(f"samtools sort -u -@ {sort_threads} -m {SORT_MEMORY_PER_THREAD} -T {shlex.quote(tmp + '.sort')} -")
    cram = f"--reference {shlex.quote(ref)} -O cram " if out.endswith(".cram") else ""
    stages.append(f"samtools markdup -s -@ {threads} -T {shlex.quote(tmp + '.markdup')} {cram}- {shlex.quote(out)}")
    return " | ".join(stages)


def parse_flagstat(text: str) -> dict:
    """samtools flagstat (default text output) -> QC-passed counts, plus <name>_pct where flagstat gives one.

    Keys: in_total, mapped, primary_mapped, duplicates, paired_in_sequencing, properly_paired. A
    percentage flagstat prints as N/A (e.g. properly paired for single-end reads) has no _pct key.
    """
    out = {}
    for line in text.splitlines():
        m = re.match(
            r"^(\d+) \+ \d+ (in total|mapped|primary mapped|duplicates|paired in sequencing|properly paired)"
            r"(?: \(([\d.]+)%| \(N/A)?",
            line,
        )
        if m:
            key = m.group(2).replace(" ", "_")
            out[key] = int(m.group(1))
            if m.group(3) is not None:
                out[key + "_pct"] = float(m.group(3))
    return out


def parse_mosdepth_summary(text: str) -> dict:
    """mosdepth's <prefix>.mosdepth.summary.txt -> length, bases and mean_depth of its 'total' row."""
    rows = [line.split("\t") for line in text.splitlines() if line.strip()]
    header, total = rows[0], next(r for r in rows[1:] if r[0] == "total")
    return {
        "length": int(total[header.index("length")]),
        "bases": int(total[header.index("bases")]),
        "mean_depth": float(total[header.index("mean")]),
    }


def parse_bcftools_stats(text: str) -> dict:
    """bcftools stats -> record/SNP/indel counts and Ti/Tv.

    Ti/Tv is computed from the TSTV row's counts (bcftools prints 0.00 when there are no
    transversions); it is None when there are no transversions, i.e. undefined.
    """
    out = {}
    for line in text.splitlines():
        cols = line.split("\t")
        if cols[0] == "SN" and len(cols) >= 4:
            label = cols[2].rstrip(":")
            for name, key in (
                ("number of records", "records"),
                ("number of SNPs", "snps"),
                ("number of indels", "indels"),
            ):
                if label == name:
                    out[key] = int(cols[3])
        elif cols[0] == "TSTV" and len(cols) >= 5:
            ts, tv = int(cols[2]), int(cols[3])
            out["tstv"] = round(ts / tv, 2) if tv else None
    return out


def _log_summary(label, parse, path):
    """Print one headline line from a stats file. Best effort: a format change never fails the stage."""
    try:
        with open(path) as fh:
            values = parse(fh.read())
        print(f"{label}: " + ", ".join(f"{k}={v}" for k, v in values.items()), flush=True)
        provenance.note_stats(label.replace(" ", "_"), values)
    except Exception as err:  # noqa: BLE001 - the stats file itself is still uploaded
        print(f"WARNING: could not summarise {os.path.basename(path)}: {err}", flush=True)


def _run_stats_tool(label: str, command: str, skipped: str = "its stats are skipped for this run") -> bool:
    """Run one best-effort command (QC statistics, annotation): its failure warns, never fails the
    stage, because the stage's real output (CRAM, VCF) is already built and worth more than a side
    report. `skipped` says what the run goes without."""
    try:
        run_shell(command)
        return True
    except subprocess.CalledProcessError as err:
        print(f"WARNING: {label} failed (exit {err.returncode}); {skipped}", flush=True)
        return False


def alignment_stats(ref: str, cram: str, wd: str, threads: int, keys: dict) -> list:
    """samtools flagstat + mosdepth (no per-base output; duplicates excluded) -> [(local path, key)] produced.

    mosdepth runs in its default mode, not --fast-mode: that corrects for overlapping mates of a
    pair (counted once, not twice) and counts deletions, so paired-end depth is not overstated.
    """
    flagstat = os.path.join(wd, STATS_FILES["flagstat"])
    prefix = os.path.join(wd, "stats")
    summary, dist = f"{prefix}.mosdepth.summary.txt", f"{prefix}.mosdepth.global.dist.txt"
    files = []
    if _run_stats_tool(
        "samtools flagstat",
        f"samtools flagstat -@ {threads} --input-fmt-option reference={shlex.quote(ref)} "
        f"{shlex.quote(cram)} > {shlex.quote(flagstat)}",
    ):
        files.append((flagstat, keys["flagstat"]))
        _log_summary("flagstat", parse_flagstat, flagstat)
    if _run_stats_tool(
        "mosdepth",
        f"mosdepth -n -t {min(threads, MOSDEPTH_MAX_THREADS)} -f {shlex.quote(ref)} "
        f"{shlex.quote(prefix)} {shlex.quote(cram)}",
    ):
        files += [(summary, keys["mosdepth_summary"]), (dist, keys["mosdepth_dist"])]
        _log_summary("mosdepth", parse_mosdepth_summary, summary)
    return files


@timed_stage("Align")
def align_task(sample_id, reference_name):
    """bwa-mem2 (or ALIGNER=bwa) on the gzipped reads -> coordinate-sorted, duplicate-marked CRAM + .crai."""
    aligner = aligner_from_env()  # a bad ALIGNER fails here, before any download
    layout = sample_layout(sample_id)
    keys = keys_for(sample_id, layout)
    wd = work_dir(sample_id)
    try:
        fastqs = download_raw_reads(keys, wd)
        cram = os.path.join(wd, os.path.basename(keys["cram"]))
        crai = os.path.join(wd, os.path.basename(keys["crai"]))
        threads = os.cpu_count() or 1
        ref = ensure_reference_local(reference_name, wd, need_bwa_index=False)
        aligner = prepare_aligner(reference_name, ref, aligner)
        print(f"Aligning with {aligner}", flush=True)
        provenance.note(aligner=aligner, layout=layout)
        provenance.note_tools(aligner, "samtools", "mosdepth")
        run_shell(align_command(ref, fastqs, cram, threads, wd, sample_id, aligner))
        run_shell(f"samtools index -@ {threads} -o {shlex.quote(crai)} {shlex.quote(cram)}")
        stats = alignment_stats(ref, cram, wd, threads, keys)
        for path, key in stats:  # before the CRAM: a sample that shows as aligned has its stats
            upload_output(path, key)
        # Index first, so a first run never shows a CRAM (the stage-done marker) without its index.
        # On a re-run that fails between the two uploads, the stored pair can disagree (new .crai,
        # old CRAM; the job role can't delete objects to avoid it). The pipeline's own readers are
        # immune, as fetch_alignment re-indexes; for people and browsers it is fixed by the next run.
        upload_output(crai, keys["crai"])
        upload_output(cram, keys["cram"])
    finally:
        shutil.rmtree(wd, ignore_errors=True)


# Scatter-gather variant calling (#108): the reference is cut into regions, each region is called as
# its own job (variants --region), and the merge (variants --window) concatenates the parts in
# reference order. mpileup -r reports each position in exactly one region, so the merged calls equal
# the unsharded ones (checked with SNPs on window boundaries; tests/test_e2e_docker.py).
SHARD_METRIC = "CallVariantsShard"  # its own metric (and dashboard group); the merge is CallVariants
DEFAULT_REGION_WINDOW = 50_000_000  # bp per region: GRCh38 chr20 (64.4 Mb) -> 2 shards
# Parts live per run (RUN_NAME: the Step Functions execution, or run_local's run), so a merge only ever
# sees its own run's shards: the job role can't delete objects, and an earlier run's parts stay behind.
# Without RUN_NAME they share "unnamed", and a re-run that skips some shards could merge stale ones.
# Everything under INTERMEDIATE_PREFIX is disposable: the data lake's lifecycle rule (#62) expires it
# after a few days, which is what clears those leftover parts. Outputs never go here.
INTERMEDIATE_PREFIX = "intermediate/"
PARTS_PREFIX = INTERMEDIATE_PREFIX + "variant-parts/{sample_id}/{run}/"


def parts_prefix(sample_id: str) -> str:
    return PARTS_PREFIX.format(sample_id=sample_id, run=_safe_name(os.environ.get("RUN_NAME") or "unnamed"))


def _safe_name(text: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in text)


def contig_lengths_from_fai(text: str) -> list:
    """[(contig, length)] in reference order, from a .fai. A malformed line is a ValueError naming it."""
    lengths = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        cols = line.split("\t")
        try:
            lengths.append((cols[0], int(cols[1])))
        except (IndexError, ValueError) as err:
            raise ValueError(f"Malformed .fai line {number}: {line[:80]!r}") from err
    return lengths


def contig_lengths_from_fasta(path: str) -> list:
    """[(contig, length)] in reference order, by reading the FASTA (when there is no .fai yet)."""
    lengths, name, total = [], None, 0
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt") as fh:
        for line in fh:
            if line.startswith(">"):
                if name is not None:
                    lengths.append((name, total))
                if not line[1:].split():
                    raise ValueError(f"FASTA header without a name in {os.path.basename(path)}")
                name, total = line[1:].split()[0], 0
            else:
                total += len(line.strip())
    if name is not None:
        lengths.append((name, total))
    return lengths


def regions_from_lengths(lengths, window: int) -> list:
    """Deterministic regions: each contig cut into windows of `window` bp, 1-based and inclusive, in order.

    The last window of a contig is the remainder. Contig names containing ':' are braced, as htslib
    reads regions ({HLA-A*01:01}:1-100).
    """
    if window < 1:
        raise ValueError(f"window must be >= 1, got {window}")
    regions = []
    for contig, length in lengths:
        name = f"{{{contig}}}" if ":" in contig else contig
        for start in range(1, length + 1, window):
            regions.append(f"{name}:{start}-{min(start + window - 1, length)}")
    return regions


def region_part_key(sample_id: str, region: str) -> str:
    """intermediate/variant-parts/{id}/<run>/<region>.vcf.gz; the name is the region made filename-safe plus a short hash."""
    digest = hashlib.md5(region.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]  # a name, not security
    return parts_prefix(sample_id) + f"{_safe_name(region)}-{digest}.vcf.gz"


def reference_regions(local_ref: str, window: int) -> list:
    """Regions of a local reference: from its .fai if there is one, else from the FASTA.

    No regions at all (an empty or broken index) is an error: a merge of nothing would otherwise
    "succeed" with an empty VCF.
    """
    fai = local_ref + ".fai"
    if os.path.exists(fai):
        with open(fai) as fh:
            lengths = contig_lengths_from_fai(fh.read())
    else:
        lengths = contig_lengths_from_fasta(local_ref)
    regions = regions_from_lengths(lengths, window)
    if not regions:
        raise ValueError(f"No regions in {os.path.basename(local_ref)}: its index lists no contig with any bases")
    return regions


# Gene models for consequence annotation sit next to the reference: reference/<name>.gff3.gz, in
# Ensembl GFF3 form (what bcftools csq reads). ANNOTATE=off skips annotation even when one exists.
ANNOTATION_SUFFIX = ".gff3.gz"
ANNOTATE_MODES = ("auto", "off")


def annotation_key(reference_name: str) -> str:
    return f"{REFERENCE_PREFIX}{reference_name}{ANNOTATION_SUFFIX}"


def annotate_mode() -> str:
    mode = os.environ.get("ANNOTATE", "auto").strip().lower() or "auto"
    if mode not in ANNOTATE_MODES:
        raise ValueError(f"ANNOTATE must be one of {ANNOTATE_MODES}, got {mode!r}")
    return mode


def annotate_vcf(reference_name: str, ref: str, vcf: str, wd: str, keys: dict) -> list:
    """bcftools csq on a finished VCF -> [(local path, key)] to upload: the annotated VCF and its index.

    Optional and best effort, like the QC statistics: nothing happens when ANNOTATE=off or the
    reference has no GFF (the cloud reference has none today), and a csq failure warns rather than
    failing the stage, whose real output (the VCF) is already built. -p a takes genotypes as phased,
    because bcftools call doesn't phase; consequences are per allele either way (BCSQ in INFO).
    """
    if annotate_mode() == "off":
        print("Annotation off (ANNOTATE=off)", flush=True)
        provenance.note(annotation="off")
        return []
    gff_key = annotation_key(reference_name)
    gff = os.path.join(wd, os.path.basename(gff_key))
    try:  # storage trouble (throttling, a GFF deleted mid-run) must not cost the VCF either
        if not storage().exists(gff_key):
            print(f"No gene models at {storage().describe(gff_key)}; skipping annotation", flush=True)
            provenance.note(annotation="skipped: no GFF")
            return []
        storage().download(gff_key, gff)
    except Exception as err:  # noqa: BLE001 - best effort, see the docstring
        print(f"WARNING: could not read {storage().describe(gff_key)} ({err}); no annotated VCF this run", flush=True)
        provenance.note(annotation="failed")
        return []
    provenance.note_input(gff_key, gff)
    csq = os.path.join(wd, os.path.basename(keys["csq_vcf"]))
    csq_tbi = os.path.join(wd, os.path.basename(keys["csq_tbi"]))
    annotate = (
        f"bcftools csq -f {shlex.quote(ref)} -g {shlex.quote(gff)} -p a -Oz -o {shlex.quote(csq)} {shlex.quote(vcf)}"
    )
    index = f"bcftools index -t -o {shlex.quote(csq_tbi)} {shlex.quote(csq)}"
    skipped = "no annotated VCF this run"
    if not (
        _run_stats_tool("bcftools csq", annotate, skipped) and _run_stats_tool("bcftools index (csq)", index, skipped)
    ):
        provenance.note(annotation="failed")
        return []
    provenance.note(annotation="done")
    return [(csq_tbi, keys["csq_tbi"]), (csq, keys["csq_vcf"])]


def _finish_vcf(reference_name: str, ref: str, vcf: str, wd: str, keys: dict) -> None:
    """Index, stats, annotation and upload of a finished VCF (the unsharded call and the merge end the same way)."""
    tbi = os.path.join(wd, os.path.basename(keys["tbi"]))
    run_shell(f"bcftools index -t -o {shlex.quote(tbi)} {shlex.quote(vcf)}")
    stats = os.path.join(wd, STATS_FILES["bcftools_stats"])
    if _run_stats_tool(
        "bcftools stats", f"bcftools stats -F {shlex.quote(ref)} -s - {shlex.quote(vcf)} > {shlex.quote(stats)}"
    ):
        _log_summary("bcftools stats", parse_bcftools_stats, stats)
        upload_output(stats, keys["bcftools_stats"])
    for local, key in annotate_vcf(reference_name, ref, vcf, wd, keys):
        upload_output(local, key)
    upload_output(tbi, keys["tbi"])  # index, stats and annotation first, as in align: the VCF marks the stage done
    upload_output(vcf, keys["vcf"])


@timed_stage(SHARD_METRIC, run_record=False)
def variants_shard_task(sample_id, reference_name, region):
    """bcftools on one region of the sample's alignment -> intermediate/variant-parts/{id}/<run>/<region>.vcf.gz."""
    wd = work_dir(sample_id)
    try:
        alignment = fetch_alignment(sample_id, wd, need_index=True)  # -r needs random access
        ref = ensure_reference_local(reference_name, wd, need_bwa_index=False)
        part = os.path.join(wd, "part.vcf.gz")
        print(f"Calling region {region}", flush=True)
        run_shell(
            f"bcftools mpileup -f {shlex.quote(ref)} -r {shlex.quote(region)} {shlex.quote(alignment)} "
            f"| bcftools call -mv -Oz -o {shlex.quote(part)}"
        )
        storage().upload(part, region_part_key(sample_id, region))
    finally:
        shutil.rmtree(wd, ignore_errors=True)


@timed_stage("CallVariants")
def variants_task(sample_id, reference_name, window=None):
    """bcftools on the sample's CRAM (or BAM) -> variants/{id}.vcf.gz + its tabix index + bcftools stats.

    window: merge instead of call: concatenate the parts that variants --region wrote for every
    region of regions_from_lengths(reference, window), in reference order. Every part must exist
    (the missing ones are named); the window must be the one the regions were planned with.
    """
    keys = keys_for(sample_id)
    annotate_mode()  # a mistyped ANNOTATE fails now, not after a chromosome's worth of calling
    wd = work_dir(sample_id)
    try:
        provenance.note_tools("bcftools")
        vcf = os.path.join(wd, os.path.basename(keys["vcf"]))
        ref = ensure_reference_local(reference_name, wd, need_bwa_index=False)
        if window is None:
            alignment = fetch_alignment(sample_id, wd)  # mpileup reads it front to back: no index needed
            run_shell(
                f"bcftools mpileup -f {shlex.quote(ref)} {shlex.quote(alignment)} "
                f"| bcftools call -mv -Oz -o {shlex.quote(vcf)}"
            )
        else:
            regions = reference_regions(ref, window)
            part_keys = [region_part_key(sample_id, r) for r in regions]
            stored = set(storage().list_keys(parts_prefix(sample_id)))  # one listing, not a HEAD per part
            missing = [r for r, k in zip(regions, part_keys, strict=True) if k not in stored]
            if missing:
                raise FileNotFoundError(
                    f"{len(missing)} of {len(regions)} region parts missing for {sample_id} "
                    f"(window {window}): {', '.join(missing[:5])}{' ...' if len(missing) > 5 else ''}"
                )
            provenance.note(regions=len(regions), region_window=window)
            try:  # the alignment the shards read, for the record (not downloaded here; never fails the merge)
                provenance.note_input(alignment_input(sample_id))
            except FileNotFoundError:
                print(f"Note: no alignment for {sample_id} to name in the run record", flush=True)
            listing = os.path.join(wd, "parts.txt")
            with open(listing, "w") as fh:
                for i, key in enumerate(part_keys):
                    local = os.path.join(wd, f"part{i:05d}.vcf.gz")
                    storage().download(key, local)
                    provenance.note_input(key, local)
                    fh.write(local + "\n")
            print(f"Merging {len(regions)} region parts", flush=True)
            run_shell(f"bcftools concat -Oz -o {shlex.quote(vcf)} -f {shlex.quote(listing)}")
        _finish_vcf(reference_name, ref, vcf, wd, keys)
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def regions_main(argv) -> int:
    """tasks.py regions <reference_name> [--window N] [--output-key KEY]: the regions as a JSON list.

    Printed as the last stdout line, and with --output-key also stored at KEY: that is how the state
    machine's planner hands the list to its Map (a Batch job can't return output; Read_Regions reads KEY).
    """
    parser = argparse.ArgumentParser(prog="tasks.py regions", description=regions_main.__doc__)
    parser.add_argument("reference_name")
    parser.add_argument("--window", type=int, default=DEFAULT_REGION_WINDOW)
    parser.add_argument("--output-key", help="also store the JSON list at this storage key")
    args = parser.parse_args(argv)
    if args.window < 1:
        parser.error("--window must be >= 1")
    wd = work_dir("regions")
    try:
        ref = ensure_reference_local(args.reference_name, wd, need_bwa_index=False)
        regions = json.dumps(reference_regions(ref, args.window))
        if args.output_key:
            storage().upload_stream(io.BytesIO(regions.encode("utf-8")), args.output_key)
            print(f"Stored {len(json.loads(regions))} regions at {storage().describe(args.output_key)}", flush=True)
        print(regions)
    finally:
        shutil.rmtree(wd, ignore_errors=True)
    return 0


def finalize_main(argv) -> int:
    """tasks.py finalize <srr_id> --status failed [--error ERROR] [--cause CAUSE]: close the run's record.

    The state machine's failure path runs this with its Catch's Error and Cause, so a run whose failing job never
    wrote its run-record part (killed, out of memory, timed out, a step outside the stages) still ends as failed,
    with the failing stage and why (provenance.close_run). Best effort: always exits 0, so the failure path goes on
    to its notification whatever happens here.
    """
    parser = argparse.ArgumentParser(prog="tasks.py finalize", description=finalize_main.__doc__)
    parser.add_argument("srr_id")
    parser.add_argument("--status", choices=["failed"], required=True)
    parser.add_argument("--error", help="the error name, e.g. States.TaskFailed")
    parser.add_argument("--cause", help="the error's cause (a failed Batch job's description, as JSON)")
    parser.add_argument(
        "--caught",
        help="the Catch output as JSON ({Error, Cause}: States.JsonToString($.error)); --error/--cause override it. "
        "The state machine passes this one: a Pass state reading $.error.Cause would itself fail if Cause were absent",
    )
    args = parser.parse_args(argv)
    caught = {}
    if args.caught:
        try:
            caught = json.loads(args.caught)
        except ValueError:
            caught = {"Cause": args.caught}  # not JSON: keep it as the cause
        if not isinstance(caught, dict):
            caught = {"Cause": args.caught}
    error = args.error if args.error is not None else caught.get("Error")
    cause = args.cause if args.cause is not None else caught.get("Cause")
    try:
        record = provenance.close_run(storage(), args.srr_id, args.status, error, cause)
    except Exception as err:  # noqa: BLE001 - close_run never raises; storage() itself might
        print(f"WARNING: could not close the run record for {args.srr_id}: {err}", flush=True)
        return 0
    failure = (record or {}).get("failure") or {}
    print(
        f"Run record for {args.srr_id} closed as {(record or {}).get('status', 'unknown')}"
        f" (stage: {failure.get('stage') or 'unknown'}; {failure.get('reason') or failure.get('error') or ''})",
        flush=True,
    )
    return 0


TASKS = {
    "qc": (qc_task, False),
    "align": (align_task, True),
    "variants": (variants_task, True),
}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["regions"]:
        return regions_main(argv[1:])
    if argv[:1] == ["finalize"]:
        return finalize_main(argv[1:])
    parser = argparse.ArgumentParser(description="Run one Geyser Genomics pipeline stage.")
    parser.add_argument("task_name", choices=sorted(TASKS))
    parser.add_argument("srr_id", help="Sample ID, e.g. SRR062634")
    parser.add_argument("reference_name", nargs="?", help="Reference FASTA name (align and variants), e.g. chr20.fa")
    parser.add_argument("--region", help="variants: call only this region (a scatter-gather shard)")
    parser.add_argument("--window", type=int, help="variants: merge the shards of regions planned with this window")
    args = parser.parse_args(argv)
    if (args.region is not None or args.window is not None) and args.task_name != "variants":
        parser.error("--region and --window only apply to 'variants'")
    if args.region is not None and args.window is not None:
        parser.error("--region (call one shard) and --window (merge the shards) are different jobs")
    if args.window is not None and args.window < 1:
        parser.error("--window must be >= 1")

    func, needs_reference = TASKS[args.task_name]
    if needs_reference:
        if not args.reference_name:
            parser.error(f"'{args.task_name}' requires reference_name")
        if args.region is not None:
            variants_shard_task(args.srr_id, args.reference_name, args.region)
        elif args.window is not None:
            func(args.srr_id, args.reference_name, window=args.window)
        else:
            func(args.srr_id, args.reference_name)
    else:
        func(args.srr_id)
    print(f"Task '{args.task_name}' completed for sample '{args.srr_id}'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
