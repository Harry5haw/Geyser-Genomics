# app/tasks.py
"""Pipeline stages: (qc and align, side by side) -> variants.

Each stage is run as its own container job (AWS Batch in the cloud, `docker run`
locally) with:   python tasks.py <stage> <sample_id> [reference_name]

Stages communicate only through storage keys:
    raw_reads/{id}.fastq.gz -> qc_reports/{id}.fastp.{html,json}     (qc: fastp, report only)
                            -> alignments/{id}.cram + .cram.crai     (align: sorted, duplicates marked)
    alignments/{id}.cram    -> variants/{id}.vcf.gz + .vcf.gz.tbi    (variants)
    stats/{id}/: flagstat.txt, mosdepth.{summary,global.dist}.txt (align); bcftools.stats.txt (variants)
    reference/{reference_name} (+ optional .fai / aligner indexes, see ALIGNER_INDEX_EXTS)

qc and align both read the gzipped upload directly (fastp, bwa-mem2 and bwa read gzip), so they don't
depend on each other and can run in parallel (the three-stage state machine, #84). There is no
decompressed copy; both stages fail on a truncated upload (tests/test_e2e_docker.py).

Paired-end samples have two raw files, raw_reads/{id}_R1.fastq.gz and {id}_R2.fastq.gz (see
keys_for); everything after them is one file per sample, the QC report included. qc and align detect the layout from
which raw keys exist (sample_layout), so the CLI and the Step Functions input are the same for
both. The trigger Lambda parses the same _R1/_R2 names (tests check they agree).

Where those keys live is chosen by STORAGE_BACKEND (see storage.py); metrics by
METRICS_BACKEND (see metrics.py). If you rename a stage or key, update the Step
Functions definition (infrastructure/step_functions.tf) and scripts/run_local.py too.
"""

import argparse
import gzip
import os
import re
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
        raise FileNotFoundError(f"No alignment for {sample_id}: none of "
                                + ", ".join(storage().describe(keys[f]) for f in ALIGNMENT_FORMS))
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
        aligner = check_aligner(aligner) if aligner else aligner_from_env()  # before the (large) download

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
    """Download a stored index if every file of it is stored; otherwise build it.

    Checking first avoids downloading part of a set only to overwrite it (the cloud reference/
    holds bwa's index, whose .amb/.ann/.pac bwa-mem2 index rewrites).
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


def decompress_task(sample_id):
    """No-op, kept so a new image still works under the old four-stage state machine.

    qc and align now read raw_reads/ directly. Remove this (and its TASKS entry) once the
    three-stage state machine is applied; it records no metric, so dashboards never show it.
    """
    print(f"'decompress' is no longer a stage: qc and align read raw_reads/ directly. "
          f"Nothing to do for '{sample_id}'.", flush=True)


def download_raw_reads(keys: dict, wd: str) -> list:
    """Download the sample's gzipped read file(s) as-is into wd; returns the local paths (R1 first)."""
    paths = []
    for key in keys_of(keys, "raw"):
        paths.append(os.path.join(wd, os.path.basename(key)))
        storage().download(key, paths[-1])
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
    return cmd + ["--disable_adapter_trimming", "--disable_quality_filtering", "--disable_length_filtering",
                  "--disable_trim_poly_g",
                  "--thread", str(max(1, min(threads, QC_MAX_THREADS))),
                  "--json", json_path, "--html", html, "--report_title", f"{sample_id} (fastp)"]


@timed_stage("QualityControl")
def qc_task(sample_id):
    """fastp report (HTML + JSON) for the sample's gzipped read file(s): one report, either layout."""
    keys = keys_for(sample_id, sample_layout(sample_id))
    wd = work_dir(sample_id)
    try:
        fastqs = download_raw_reads(keys, wd)
        html, json_path = (os.path.join(wd, os.path.basename(keys[k])) for k in ("qc_html", "qc_json"))
        # Bounded in case fastp ever hangs; keep in step with the stage's Step Functions timeout.
        # fastp fails on a truncated upload ("igzip: unexpected eof", exit 255) and writes no report.
        timeout = int(os.environ.get("QC_TIMEOUT_SECONDS", "3600"))
        subprocess.run(qc_command(fastqs, html, json_path, os.cpu_count() or 1, sample_id),
                       check=True, timeout=timeout)
        storage().upload(html, keys["qc_html"])
        storage().upload(json_path, keys["qc_json"])
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def align_command(ref: str, fastqs, out: str, threads: int, tmp_dir: str, sample_id: str,
                  aligner: str = DEFAULT_ALIGNER) -> str:
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
    stages = [f"{aligner} mem -t {threads} -R {shlex.quote(read_group)} {shlex.quote(ref)} "
              + " ".join(shlex.quote(f) for f in fastqs)]
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
        m = re.match(r"^(\d+) \+ \d+ (in total|mapped|primary mapped|duplicates|paired in sequencing|properly paired)"
                     r"(?: \(([\d.]+)%| \(N/A)?", line)
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
    return {"length": int(total[header.index("length")]), "bases": int(total[header.index("bases")]),
            "mean_depth": float(total[header.index("mean")])}


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
            for name, key in (("number of records", "records"), ("number of SNPs", "snps"),
                              ("number of indels", "indels")):
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
    except Exception as err:  # noqa: BLE001 - the stats file itself is still uploaded
        print(f"WARNING: could not summarise {os.path.basename(path)}: {err}", flush=True)


def _run_stats_tool(label: str, command: str) -> bool:
    """Run one QC statistics command. Best effort: its failure warns, never fails the stage, because
    the stage's real output (CRAM, VCF) is already built and worth more than a side report."""
    try:
        run_shell(command)
        return True
    except subprocess.CalledProcessError as err:
        print(f"WARNING: {label} failed (exit {err.returncode}); its stats are skipped for this run", flush=True)
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
    if _run_stats_tool("samtools flagstat",
                       f"samtools flagstat -@ {threads} --input-fmt-option reference={shlex.quote(ref)} "
                       f"{shlex.quote(cram)} > {shlex.quote(flagstat)}"):
        files.append((flagstat, keys["flagstat"]))
        _log_summary("flagstat", parse_flagstat, flagstat)
    if _run_stats_tool("mosdepth",
                       f"mosdepth -n -t {min(threads, MOSDEPTH_MAX_THREADS)} -f {shlex.quote(ref)} "
                       f"{shlex.quote(prefix)} {shlex.quote(cram)}"):
        files += [(summary, keys["mosdepth_summary"]), (dist, keys["mosdepth_dist"])]
        _log_summary("mosdepth", parse_mosdepth_summary, summary)
    return files


@timed_stage("Align")
def align_task(sample_id, reference_name):
    """bwa-mem2 (or ALIGNER=bwa) on the gzipped reads -> coordinate-sorted, duplicate-marked CRAM + .crai."""
    aligner = aligner_from_env()  # a bad ALIGNER fails here, before any download
    keys = keys_for(sample_id, sample_layout(sample_id))
    wd = work_dir(sample_id)
    try:
        fastqs = download_raw_reads(keys, wd)
        cram = os.path.join(wd, os.path.basename(keys["cram"]))
        crai = os.path.join(wd, os.path.basename(keys["crai"]))
        threads = os.cpu_count() or 1
        ref = ensure_reference_local(reference_name, wd, need_bwa_index=False)
        aligner = prepare_aligner(reference_name, ref, aligner)
        print(f"Aligning with {aligner}", flush=True)
        run_shell(align_command(ref, fastqs, cram, threads, wd, sample_id, aligner))
        run_shell(f"samtools index -@ {threads} -o {shlex.quote(crai)} {shlex.quote(cram)}")
        stats = alignment_stats(ref, cram, wd, threads, keys)
        for path, key in stats:  # before the CRAM: a sample that shows as aligned has its stats
            storage().upload(path, key)
        # Index first, so a first run never shows a CRAM (the stage-done marker) without its index.
        # On a re-run that fails between the two uploads, the stored pair can disagree (new .crai,
        # old CRAM; the job role can't delete objects to avoid it). The pipeline's own readers are
        # immune, as fetch_alignment re-indexes; for people and browsers it is fixed by the next run.
        storage().upload(crai, keys["crai"])
        storage().upload(cram, keys["cram"])
    finally:
        shutil.rmtree(wd, ignore_errors=True)


@timed_stage("CallVariants")
def variants_task(sample_id, reference_name):
    """bcftools on the sample's CRAM (or BAM) -> variants/{id}.vcf.gz + its tabix index + bcftools stats."""
    keys = keys_for(sample_id)
    wd = work_dir(sample_id)
    try:
        alignment = fetch_alignment(sample_id, wd)  # mpileup reads it front to back: no index needed
        vcf = os.path.join(wd, os.path.basename(keys["vcf"]))
        tbi = os.path.join(wd, os.path.basename(keys["tbi"]))
        ref = ensure_reference_local(reference_name, wd, need_bwa_index=False)
        run_shell(
            f"bcftools mpileup -f {shlex.quote(ref)} {shlex.quote(alignment)} "
            f"| bcftools call -mv -Oz -o {shlex.quote(vcf)}"
        )
        run_shell(f"bcftools index -t -o {shlex.quote(tbi)} {shlex.quote(vcf)}")
        stats = os.path.join(wd, STATS_FILES["bcftools_stats"])
        if _run_stats_tool("bcftools stats",
                           f"bcftools stats -F {shlex.quote(ref)} -s - {shlex.quote(vcf)} > {shlex.quote(stats)}"):
            _log_summary("bcftools stats", parse_bcftools_stats, stats)
            storage().upload(stats, keys["bcftools_stats"])
        storage().upload(tbi, keys["tbi"])  # index and stats first, as in align: the VCF marks the stage done
        storage().upload(vcf, keys["vcf"])
    finally:
        shutil.rmtree(wd, ignore_errors=True)


TASKS = {
    "decompress": (decompress_task, False),  # no-op until the three-stage state machine is applied
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
