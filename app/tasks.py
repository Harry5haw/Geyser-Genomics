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
    fastp JSON + stats/{id}/ + run record -> qc_reports/{id}.gate.json   (gate: the quality gate's verdict)
    reference/{reference_name} (+ optional .fai / aligner indexes, see ALIGNER_INDEX_EXTS)

align runs bwa-mem2 by default; ALIGNER=bwa or ALIGNER=giraffe (vg giraffe on a pangenome graph,
pangenome/<graph>.gbz, in the geyser-giraffe image; see GIRAFFE) write the same keys.

qc and align both read the gzipped upload directly (fastp, the aligners and kmc read gzip), so they don't
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
import zlib
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
# vg giraffe (#146): aligns to a pangenome graph instead of the FASTA, then surjects onto the graph's
# GRCh38 paths, so its output is an ordinary linear alignment and everything after it (fixmate | sort |
# markdup, CRAM, stats, variants) is unchanged. It needs vg and kmc, which live in the geyser-giraffe image
# (app-giraffe/), not in geyser-app. Its files belong to the graph, not to the FASTA, so they are not in
# ALIGNER_INDEX_EXTS: the graph is pangenome/<graph>.gbz (GIRAFFE_GRAPH, default <reference stem>.gbz:
# the key the pangenome-aware caller and make_test_data.py --pangenome use) with its indexes beside it.
GIRAFFE = "giraffe"
ALIGNERS = (*ALIGNER_INDEX_EXTS, GIRAFFE)
PANGENOME_PREFIX = "pangenome/"
# Graph index files next to pangenome/<stem>.gbz (each fetched if stored), per mode (GIRAFFE_SAMPLING):
#   on (default; Google's VG case study and the #129 benchmark): haplotype sampling. kmc counts the sample's
#      k-mers and giraffe maps to a personal graph of the haplotypes closest to them, building that graph's
#      indexes itself. Needs the haplotype information (.hapl); a .hapl that isn't stored is built in the job
#      (from the stored .dist and .ri, or building those too): seconds on the synthetic graph, ~6 min on chr20, hours for a whole
#      genome, so upload that one (HPRC publishes .hapl files).
#   off: map to the whole graph with its distance and minimizer indexes; giraffe builds any that isn't stored.
GRAPH_INDEX_EXTS = (".hapl", ".dist", ".min", ".ri")  # .ri: the r-index, only used to build a .hapl
# The graph sample whose paths are the linear reference (HPRC graphs: GRCh38#0#chr20; the synthetic graph too).
GRAPH_REFERENCE_SAMPLE = "GRCh38"
KMER_LENGTH = 29  # vg haplotypes' minimizer k: the KFF counts must use the same k
DEFAULT_KMC_MEMORY_GB = 4  # kmc -m: a cap, not a reservation (7.4 GiB peak at -m8 on HG003 chr20, 37 s)
# Google's VG case-study flags: -P prunes short low-complexity anchors when realigning onto the linear
# paths; -L caps the fragment length giraffe assumes while learning the insert sizes (paired-end only).
GIRAFFE_MAX_FRAGMENT_LENGTH = 3000
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
    # The quality gate's verdict on all of the above (gate_task, gate/README.md).
    keys["gate_verdict"] = f"qc_reports/{sample_id}.gate.json"
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
    """name if it is an aligner the align stage knows (ALIGNERS), else ValueError."""
    if name not in ALIGNERS:
        raise ValueError(f"Unknown aligner {name!r} (ALIGNER); expected one of {sorted(ALIGNERS)}")
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
        if aligner == GIRAFFE:
            raise ValueError("giraffe has no FASTA index: it aligns to a pangenome graph (prepare_graph)")

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

    Returns the aligner to run. The only way to a FASTA aligner index, so the guard can't be skipped.
    giraffe has no FASTA index (it aligns to its graph: prepare_graph), so asking for one is an error,
    not a reference that silently comes back without an index.
    """
    if requested == GIRAFFE:
        raise ValueError("giraffe has no FASTA index: it aligns to a pangenome graph (prepare_graph)")
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
    if _fetch_stored(fasta_key, local_ref, exts):
        return
    print(f"{what} missing; running {' '.join(build_cmd[:2])}")
    subprocess.run(build_cmd, check=True)


def _fetch_stored(base_key: str, local_base: str, exts) -> bool:
    """Download base_key+ext to local_base+ext for every ext, if all of them are stored. True if done."""
    if not all(storage().exists(base_key + ext) for ext in exts):
        return False
    try:
        for ext in exts:
            storage().download(base_key + ext, local_base + ext)
        return True
    except ObjectNotFound:
        return False  # deleted in between: the caller builds it


# --- vg giraffe (#146) -------------------------------------------------------------------------------


def _reference_stem(reference_name: str) -> str:
    """chr20.fa -> chr20: the default graph's stem, the reference name without its last extension, which is
    what make_test_data.py --pangenome writes for a top-level reference (the pipeline only fetches those)."""
    return os.path.splitext(os.path.basename(reference_name))[0]


def giraffe_settings(reference_name: str) -> dict:
    """{graph, sampling} from GIRAFFE_GRAPH and GIRAFFE_SAMPLING (on | off, default on), checked before any
    download. graph is a .gbz name under pangenome/, default <reference stem>.gbz."""
    graph = os.environ.get("GIRAFFE_GRAPH", "").strip() or f"{_reference_stem(reference_name)}.gbz"
    if not graph.endswith(".gbz") or "/" in graph or graph == ".gbz":
        raise ValueError(f"GIRAFFE_GRAPH must name a .gbz file under {PANGENOME_PREFIX}, got {graph!r}")
    mode = os.environ.get("GIRAFFE_SAMPLING", "").strip().lower() or "on"
    if mode not in ("on", "off"):
        raise ValueError(f"GIRAFFE_SAMPLING must be on or off, got {mode!r}")
    return {"graph": graph, "sampling": mode == "on"}


def kmc_memory_gb() -> int:
    value = os.environ.get("KMC_MEMORY_GB", "").strip() or str(DEFAULT_KMC_MEMORY_GB)
    if not value.isdigit() or int(value) < 1:
        raise ValueError(f"KMC_MEMORY_GB must be a whole number of GB >= 1, got {value!r}")
    return int(value)


def giraffe_files(dest_dir: str, graph: str) -> dict:
    """Local paths of a graph's files in dest_dir: gbz, and each GRAPH_INDEX_EXTS index named after it."""
    base = os.path.join(dest_dir, graph[: -len(".gbz")])
    return {"gbz": base + ".gbz", **{ext[1:]: base + ext for ext in GRAPH_INDEX_EXTS}}


def missing_tools(*names: str) -> list:
    return [name for name in names if shutil.which(name) is None]


def prepare_graph(dest_dir: str, graph: str, sampling: bool) -> dict:
    """Fetch pangenome/<graph> and whichever of its stored indexes its mode uses into dest_dir; returns
    giraffe_files(dest_dir, graph).

    Each index file is fetched on its own (they share nothing, unlike bwa's): sampling needs the .hapl,
    and a missing .hapl is built here from the stored .dist/.ri, building only those not stored. Sampling
    off uses .dist/.min; giraffe builds any that isn't stored.
    """
    files = giraffe_files(dest_dir, graph)
    key = PANGENOME_PREFIX + graph
    try:
        print(f"Fetching pangenome graph {storage().describe(key)}", flush=True)
        storage().download(key, files["gbz"])
    except ObjectNotFound as err:
        raise FileNotFoundError(f"Pangenome graph missing: {storage().describe(key)} (GIRAFFE_GRAPH)") from err
    provenance.note_input(key, files["gbz"])
    base_key = key[: -len(".gbz")]

    def fetch(ext):
        if not _fetch_stored(base_key, files["gbz"][: -len(".gbz")], (ext,)):
            return False
        provenance.note_input(base_key + ext, files[ext[1:]])
        return True

    if sampling:
        if not fetch(".hapl"):
            build = [ext for ext in (".dist", ".ri") if not fetch(ext)] + [".hapl"]
            print(
                f"Haplotype information {graph[:-4]}.hapl missing; building {', '.join(build)} in the job", flush=True
            )
            run_shell(haplotype_index_command(files, os.cpu_count() or 1, build))
    else:
        missing = [ext for ext in (".dist", ".min") if not fetch(ext)]
        if missing:
            print(f"{', '.join(missing)} not stored; giraffe builds it from the graph", flush=True)
    return files


def haplotype_index_command(files: dict, threads: int, build=(".dist", ".ri", ".hapl")) -> str:
    """distance index -> r-index -> haplotype information (.hapl), as scripts/benchmark_giraffe.py builds them.

    build: the files to make (the others are already in place); .hapl needs both of the others.
    """
    q = {k: shlex.quote(v) for k, v in files.items()}
    steps = {
        ".dist": f"vg index -t {threads} -j {q['dist']} {q['gbz']}",
        ".ri": f"vg gbwt -p --num-threads {threads} -r {q['ri']} -Z {q['gbz']}",
        ".hapl": f"vg haplotypes -v 1 -t {threads} -d {q['dist']} -r {q['ri']} -H {q['hapl']} {q['gbz']}",
    }
    return " && ".join(command for ext, command in steps.items() if ext in build)


def kmc_command(list_file: str, prefix: str, tmp_dir: str, threads: int, memory_gb: int) -> str:
    """k-mer counts of the reads listed in list_file -> <prefix>.kff (KFF, k=KMER_LENGTH), for haplotype sampling.

    -hp hides the percentage progress that would otherwise fill the job log.
    """
    return (
        f"kmc -k{KMER_LENGTH} -m{memory_gb} -okff -t{threads} -hp "
        f"@{shlex.quote(list_file)} {shlex.quote(prefix)} {shlex.quote(tmp_dir)}"
    )


def giraffe_command(
    files: dict, fastqs, out: str, threads: int, sample_id: str, ref_paths: str, kff: str = None, sampled: str = None
) -> str:
    """vg giraffe -> unsorted BAM surjected onto the GRCh38 paths, with Google's VG case-study flags.

    kff + sampled (a name prefix in the work dir): haplotype sampling, giraffe builds the sampled graph's
    indexes there. Without them: the whole graph, with its .dist/.min next to the GBZ (giraffe guesses
    them from the GBZ's name and builds any that is missing). Paired-end gives both files (-f twice); the
    mates of a pair come out next to each other, as fixmate needs. -N/-R write @RG ID:<id> SM:<id>.
    ref_paths is an ordered list: the BAM's @SQ lines come in its order (reference_path_order).
    """
    fastqs = [fastqs] if isinstance(fastqs, str) else list(fastqs)
    if len(fastqs) not in (1, 2):
        raise ValueError(f"giraffe_command needs 1 (single-end) or 2 (paired-end) FASTQs, got {len(fastqs)}")
    if (kff is None) != (sampled is None):
        raise ValueError("haplotype sampling needs both kff and sampled")
    cmd = f"vg giraffe -t {threads} -Z {shlex.quote(files['gbz'])} "
    if kff:
        cmd += (
            f"--haplotype-name {shlex.quote(files['hapl'])} --kff-name {shlex.quote(kff)} "
            f"--index-basename {shlex.quote(sampled)} "
        )
    sid = shlex.quote(sample_id)
    cmd += f"-N {sid} -R {sid} -o BAM --ref-paths {shlex.quote(ref_paths)} -P "
    if len(fastqs) == 2:
        cmd += f"-L {GIRAFFE_MAX_FRAGMENT_LENGTH} "
    cmd += " ".join(f"-f {shlex.quote(f)}" for f in fastqs)
    return f"{cmd} > {shlex.quote(out)}"


def _graph_contig(name: str, sample: str):
    """GRCh38#0#chr20 (or GRCh38#chr20) -> chr20; None for another sample's path."""
    match = re.fullmatch(rf"{re.escape(sample)}#(?:\d+#)?(.+)", name)
    return match.group(1) if match else None


def reference_path_order(paths: str, fai: str, sample: str = GRAPH_REFERENCE_SAMPLE) -> list:
    """The graph's reference paths (vg paths -L -Q <sample> output) to surject onto, in the FASTA's order.

    giraffe writes @SQ lines in --ref-paths order, so this keeps the CRAM's contigs in .fai order, as bwa's
    are. -Q matches by prefix, so paths of another sample (GRCh38x#...) are dropped. So are reference paths
    that aren't FASTA contigs (a whole-genome graph with chr20.fa): reads that map there have no path to
    surject onto and come out unmapped, as they would if the reference were only chr20. No path on the FASTA
    at all, or two paths of one contig (GRCh38#0#chr20 and GRCh38#1#chr20), is a ValueError: the graph
    doesn't match this reference.
    """
    order = {name: i for i, (name, _) in enumerate(contig_lengths_from_fai(fai))}
    by_contig, dropped = {}, []
    for name in (line.strip() for line in paths.splitlines()):
        contig = _graph_contig(name, sample) if name else None
        if contig is None:
            continue
        if contig not in order:
            dropped.append(name)
            continue
        if contig in by_contig:
            raise ValueError(f"graph paths {by_contig[contig]!r} and {name!r} are both the reference's {contig}")
        by_contig[contig] = name
    if not by_contig:
        found = f" (it has {', '.join(dropped[:3])}{', ...' if len(dropped) > 3 else ''})" if dropped else ""
        raise ValueError(f"none of the graph's {sample} paths is a contig of the reference FASTA{found}")
    if dropped:
        print(
            f"Surjecting onto {len(by_contig)} of the graph's {len(by_contig) + len(dropped)} {sample} paths: "
            f"{len(dropped)} aren't contigs of the reference (e.g. {dropped[0]}); reads there stay unmapped",
            flush=True,
        )
    return [by_contig[c] for c in sorted(by_contig, key=order.get)]


def rename_graph_contigs(header: str, fai: str, sample: str = GRAPH_REFERENCE_SAMPLE) -> str:
    """The giraffe BAM header with @SQ names as in the FASTA: GRCh38#0#chr20 -> chr20.

    Only @SQ names change, never their order: the records refer to contigs by index, so samtools reheader
    is enough (the order is set before mapping, by reference_path_order). Each renamed contig must be in
    the FASTA (fai: its .fai) with the same length, and appear once; otherwise the graph was not built on
    this reference, and a CRAM written against it would be wrong, so that's a ValueError.

    FASTA contigs the graph has no path for (alt, decoy, HLA contigs, or the rest of the genome) are added
    after the graph's, in .fai order, so the header lists every contig, as bwa's does: region shards cut
    the genome from the .fai, and bcftools mpileup fails on a region whose contig the CRAM lacks. No record
    points at them, so appending them is safe; fasta_ordered_header says whether the result is in .fai order.
    """
    contigs = contig_lengths_from_fai(fai)
    lengths = dict(contigs)
    lines, seen, last_sq = [], set(), None
    for line in header.splitlines():
        if line.startswith("@SQ\t"):
            fields = line.split("\t")
            sn = next((i for i, f in enumerate(fields) if f.startswith("SN:")), None)
            ln = next((f[3:] for f in fields if f.startswith("LN:")), None)
            if sn is None:
                raise ValueError(f"@SQ line without SN: {line!r}")
            name = fields[sn][3:]
            contig = _graph_contig(name, sample) or name
            if contig not in lengths:
                raise ValueError(
                    f"graph path {name!r} is not a contig of the reference FASTA (the graph's {sample} paths must match it)"
                )
            if ln is not None and int(ln) != lengths[contig]:
                raise ValueError(f"graph path {name!r} is {ln} bp but the reference's {contig} is {lengths[contig]} bp")
            if contig in seen:
                raise ValueError(f"the giraffe BAM header has {contig} twice")
            seen.add(contig)
            fields[sn] = f"SN:{contig}"
            line = "\t".join(fields)
            last_sq = len(lines)
        lines.append(line)
    if not seen:
        raise ValueError("the giraffe BAM has no @SQ lines: nothing was surjected onto the reference paths")
    extra = [f"@SQ\tSN:{name}\tLN:{length}" for name, length in contigs if name not in seen]
    lines[last_sq + 1 : last_sq + 1] = extra
    return "\n".join(lines) + "\n"


def fasta_ordered_header(header: str, fai: str) -> str:
    """header with its @SQ lines (all FASTA contigs, see rename_graph_contigs) in .fai order, in place."""
    rank = {name: i for i, (name, _) in enumerate(contig_lengths_from_fai(fai))}
    lines = header.splitlines()
    slots = [i for i, line in enumerate(lines) if line.startswith("@SQ\t")]
    sq = sorted((lines[i] for i in slots), key=lambda line: rank[line.split("\tSN:", 1)[1].split("\t", 1)[0]])
    for i, line in zip(slots, sq, strict=True):
        lines[i] = line
    return "\n".join(lines) + "\n"


def giraffe_align(
    ref: str, fastqs, out: str, threads: int, wd: str, sample_id: str, files: dict, sampling: bool, kmc_memory: int
):
    """vg giraffe on the gzipped reads -> the same sorted, duplicate-marked CRAM as align_command.

    [kmc ->] giraffe -> unsorted BAM (work dir) -> header renamed to the FASTA's contigs (samtools reheader,
    header only) -> postprocess_stages (fixmate | sort | markdup). The unsorted BAM is on disk, not piped,
    because reheader needs a file: ~2.7x the CRAM's size in work space (HG003 chr20: 1.0 GB against 383 MB),
    so ~40-50 GB for a 30x whole genome. Streaming it (a header rewrite in the pipe) is the whole-genome
    follow-up if the job's disk is short (docs/design/giraffe-align.md, disk per job).
    """
    fastqs = [fastqs] if isinstance(fastqs, str) else list(fastqs)
    graph_paths, ref_paths = os.path.join(wd, "graph.paths.txt"), os.path.join(wd, "graph.ref-paths.txt")
    run_shell(f"vg paths -x {shlex.quote(files['gbz'])} -L -Q {GRAPH_REFERENCE_SAMPLE} > {shlex.quote(graph_paths)}")
    with open(graph_paths) as fh, open(ref + ".fai") as fai:
        ordered = reference_path_order(fh.read(), fai.read())
    with open(ref_paths, "w") as fh:
        fh.write("\n".join(ordered) + "\n")
    kff = sampled = None
    if sampling:
        list_file, prefix = os.path.join(wd, "kmc.reads.txt"), os.path.join(wd, "kmers")
        with open(list_file, "w") as fh:
            fh.write("\n".join(fastqs) + "\n")
        os.makedirs(os.path.join(wd, "kmc.tmp"), exist_ok=True)
        _timed("kmc", kmc_command(list_file, prefix, os.path.join(wd, "kmc.tmp"), threads, kmc_memory))
        kff, sampled = prefix + ".kff", os.path.join(wd, "sampled")
    unsorted = os.path.join(wd, "giraffe.unsorted.bam")
    _timed("giraffe", giraffe_command(files, fastqs, unsorted, threads, sample_id, ref_paths, kff, sampled))
    header = subprocess.run(["samtools", "view", "-H", unsorted], check=True, capture_output=True, text=True).stdout
    with open(ref + ".fai") as fh:
        fai_text = fh.read()
    renamed = rename_graph_contigs(header, fai_text)
    header_file = os.path.join(wd, "giraffe.header.sam")
    with open(header_file, "w") as fh:
        fh.write(renamed)
    source = f"samtools reheader {shlex.quote(header_file)} {shlex.quote(unsorted)}"
    ordered = fasta_ordered_header(renamed, fai_text)
    if ordered != renamed:
        # FASTA contigs without a graph path sit between the graph's: the full .fai-ordered header can't be
        # put on the BAM (records point at @SQ lines by index), so its records go through SAM text, which
        # names contigs, under that header. Correct for any order; slower (one text conversion).
        print(
            "The reference's contigs without a graph path lie between the graph's: re-indexing via SAM text", flush=True
        )
        ordered_file = os.path.join(wd, "giraffe.header.fasta-order.sam")
        with open(ordered_file, "w") as fh:
            fh.write(ordered)
        source = f"{{ cat {shlex.quote(ordered_file)}; {source} | samtools view -@ {threads} -; }}"
    stages = [source]
    stages += postprocess_stages(ref, out, threads, wd, paired=len(fastqs) == 2)
    _timed("post-processing", " | ".join(stages))
    os.remove(unsorted)


def _timed(label: str, command: str) -> None:
    """run_shell, then print how long it took (the job log's per-step timing; the stage metric is the total)."""
    started = time.monotonic()
    run_shell(command)
    print(f"{label}: {time.monotonic() - started:.1f} s", flush=True)


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
    if aligner == GIRAFFE:
        raise ValueError("giraffe is not a bwa-style 'mem' aligner: the align stage runs it via giraffe_align")
    read_group = rf"@RG\tID:{sample_id}\tSM:{sample_id}"  # literal backslash-t: bwa turns it into tabs
    stages = [
        f"{aligner} mem -t {threads} -R {shlex.quote(read_group)} {shlex.quote(ref)} "
        + " ".join(shlex.quote(f) for f in fastqs)
    ]
    return " | ".join(stages + postprocess_stages(ref, out, threads, tmp_dir, paired=len(fastqs) == 2))


def postprocess_stages(ref: str, out: str, threads: int, tmp_dir: str, paired: bool) -> list:
    """[fixmate (paired only)], sort, markdup: the align stage's tail after any aligner (see align_command).

    Reads a name-grouped BAM/SAM stream on stdin and writes out (CRAM against ref if out ends in .cram).
    """
    sort_threads = max(1, min(threads, SORT_MAX_THREADS))
    tmp = os.path.join(tmp_dir, "tmp")
    stages = ["samtools fixmate -m -u - -"] if paired else []
    stages.append(f"samtools sort -u -@ {sort_threads} -m {SORT_MEMORY_PER_THREAD} -T {shlex.quote(tmp + '.sort')} -")
    cram = f"--reference {shlex.quote(ref)} -O cram " if out.endswith(".cram") else ""
    stages.append(f"samtools markdup -s -@ {threads} -T {shlex.quote(tmp + '.markdup')} {cram}- {shlex.quote(out)}")
    return stages


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
    """bwa-mem2 (or ALIGNER=bwa / giraffe) on the gzipped reads -> coordinate-sorted, duplicate-marked CRAM + .crai."""
    aligner = aligner_from_env()  # a bad ALIGNER fails here, before any download
    giraffe = giraffe_settings(reference_name) if aligner == GIRAFFE else None  # so do bad GIRAFFE_* settings
    if giraffe:
        kmc_memory = kmc_memory_gb() if giraffe["sampling"] else None
        missing = missing_tools("vg", *(["kmc"] if giraffe["sampling"] else []))
        if missing:  # e.g. ALIGNER=giraffe in geyser-app: fail now, not after downloading a multi-GB graph
            raise FileNotFoundError(
                f"ALIGNER=giraffe needs {' and '.join(missing)}: run it in the geyser-giraffe image"
            )
    layout = sample_layout(sample_id)
    keys = keys_for(sample_id, layout)
    wd = work_dir(sample_id)
    try:
        fastqs = download_raw_reads(keys, wd)
        cram = os.path.join(wd, os.path.basename(keys["cram"]))
        crai = os.path.join(wd, os.path.basename(keys["crai"]))
        threads = os.cpu_count() or 1
        ref = ensure_reference_local(reference_name, wd, need_bwa_index=False)
        if giraffe:  # giraffe aligns to its graph: no FASTA index, and the bwa-mem2#262 guard doesn't apply
            files = prepare_graph(os.path.dirname(ref), giraffe["graph"], giraffe["sampling"])
        else:
            aligner = prepare_aligner(reference_name, ref, aligner)
        print(f"Aligning with {aligner}", flush=True)
        provenance.note(aligner=aligner, layout=layout)
        if giraffe:
            provenance.note(graph={"name": giraffe["graph"], "haplotype_sampling": giraffe["sampling"]})
            provenance.note_tools("vg", *(["kmc"] if giraffe["sampling"] else []), "samtools", "mosdepth")
            giraffe_align(ref, fastqs, cram, threads, wd, sample_id, files, giraffe["sampling"], kmc_memory)
        else:
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
        # upload_alignment then records the CRAM's ETag on the .crai, so calling shards can stream regions.
        upload_alignment(cram, crai, keys)
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


STREAM_REGIONS_ENV = "GEYSER_STREAM_REGIONS"  # 0: calling shards always download the CRAM (today's behaviour)


def streamable_alignment(sample_id: str, wd: str):
    """(where to read the CRAM in place, the local .crai) when the stored .crai is provably the CRAM's, else None.

    A calling shard used to download the whole CRAM and re-index it, never trusting the stored .crai (a re-run
    that failed between its uploads can leave a .crai for a different CRAM). For a genome CRAM that is ~17 GB per
    shard, so now the stored .crai is trusted when its cram-etag metadata (upload_alignment) equals the CRAM's
    current ETag, checked again after the .crai is downloaded. Anything else (a BAM, an old CRAM without the
    metadata, a mismatch, GEYSER_STREAM_REGIONS=0) returns None: the caller downloads, as before.
    """
    if os.environ.get(STREAM_REGIONS_ENV, "1").strip() == "0":
        return None
    key = alignment_input(sample_id)
    if not key.endswith(".cram"):
        return None
    crai_key = key + ".crai"
    try:
        etag = storage().head(key)["etag"]
        if storage().head(crai_key)["metadata"].get(CRAI_ETAG_METADATA) != etag:
            print("The stored index isn't recorded as this CRAM's: downloading the CRAM", flush=True)
            return None
        crai = os.path.join(wd, os.path.basename(crai_key))
        storage().download(crai_key, crai)
        if storage().head(crai_key)["metadata"].get(CRAI_ETAG_METADATA) != etag:  # replaced while we read it
            return None
        return storage().presigned_url(key), crai
    except (ObjectNotFound, AttributeError) as err:  # AttributeError: a storage backend without head()
        print(f"Can't read the region in place ({type(err).__name__}): downloading the CRAM", flush=True)
        return None


@timed_stage(SHARD_METRIC, run_record=False)
def variants_shard_task(sample_id, reference_name, region):
    """bcftools on one region of the sample's alignment -> intermediate/variant-parts/{id}/<run>/<region>.vcf.gz."""
    wd = work_dir(sample_id)
    try:
        ref = ensure_reference_local(reference_name, wd, need_bwa_index=False)
        part = os.path.join(wd, "part.vcf.gz")
        print(f"Calling region {region}", flush=True)

        def call(source):
            run_shell(
                f"bcftools mpileup -f {shlex.quote(ref)} -r {shlex.quote(region)} {shlex.quote(source)} "
                f"| bcftools call -mv -Oz -o {shlex.quote(part)}"
            )

        streamed = streamable_alignment(sample_id, wd)
        done = False
        if streamed:
            location, crai = streamed
            try:
                call(f"{location}##idx##{crai}")  # htslib reads only the region's slices, through the verified index
                print("Read the region in place (ETag-verified index)", flush=True)
                done = True
            except subprocess.CalledProcessError as err:
                print(f"WARNING: reading the region in place failed (exit {err.returncode}); downloading", flush=True)
        if not done:
            call(fetch_alignment(sample_id, wd, need_index=True))  # -r needs random access
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
    """tasks.py finalize <srr_id> --status failed [--error E] [--cause C | --caught JSON]: close the run's record.

    Closes a run whose failing job never wrote its run-record part (killed, out of memory, timed out, a step outside
    the stages) as failed, with the failing stage and why (provenance.close_run): by hand, or for a local run. The
    state machine's failure path writes the same closing part itself, straight to S3, without a Batch job (a
    retried job's Cause can be too big for a command line). Once its arguments parse, it exits 0 whatever happens
    (best effort).
    """
    parser = argparse.ArgumentParser(prog="tasks.py finalize", description=finalize_main.__doc__)
    parser.add_argument("srr_id")
    parser.add_argument("--status", choices=["failed"], required=True)
    parser.add_argument("--error", help="the error name, e.g. States.TaskFailed")
    parser.add_argument("--cause", help="the error's cause (a failed Batch job's description, as JSON)")
    parser.add_argument(
        "--caught",
        help="a Step Functions Catch output as JSON ({Error, Cause}, e.g. copied from the execution's history); "
        "--error / --cause override it",
    )
    parser.add_argument(
        "--run-name",
        help="the run to close: its execution name (default $RUN_NAME). Required one way or the other: the failed "
        "job often wrote no part, so the latest part can be an earlier, successful run's",
    )
    args = parser.parse_args(argv)
    run_name = args.run_name or os.environ.get("RUN_NAME")
    if not run_name:
        parser.error("which run? give --run-name (its execution name) or set RUN_NAME")
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
        record = provenance.close_run(storage(), args.srr_id, args.status, error, cause, run_name=run_name)
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


# --------------------------------------------------------------------------------------- quality gate
# The gate (gate/, a Rust binary in the image) checks a finished run's QC statistics against thresholds.
# GATE_MODE=warn (default until the user decides, #144): the verdict is stored and recorded, and a
# failed verdict, or a gate that can't run (an image without the binary), only warns. GATE_MODE=fail:
# a failed verdict fails the stage with the gate's reasons. GATE_CONFIG: a thresholds file (default:
# the binary's built-in human WGS thresholds; the image has the shipped ones in GATE_THRESHOLDS_DIR).
GATE_MODES = ("warn", "fail")
GATE_BINARY = "geyser-gate"
# Its stage name (metrics, run record part): an extra stage after the core three, like the shards
# (scripts/monitoring.py STAGE_GROUPS["gate"]).
GATE_METRIC = "QualityGate"
GATE_TIMEOUT_SECONDS = 300  # it parses a few small files; minutes means something is wrong
# keys_for names the gate reads (the run record comes from provenance.read_record, assembled fresh).
GATE_INPUTS = ("qc_json", "flagstat", "mosdepth_summary", "mosdepth_dist", "bcftools_stats")


class GateFailed(RuntimeError):
    """The sample failed the quality gate and GATE_MODE=fail."""


def gate_mode() -> str:
    mode = os.environ.get("GATE_MODE", "warn").strip().lower() or "warn"
    if mode not in GATE_MODES:
        raise ValueError(f"GATE_MODE must be one of {GATE_MODES}, got {mode!r}")
    return mode


def fetch_gate_inputs(sample_id: str, root: str) -> list:
    """The gate's inputs under a local root laid out like the bucket. Returns the keys fetched.

    A missing file is left missing: the gate reports it (missing stats count as a failed check by
    default). The run record is assembled from its stage parts, like every other reader of it, so a
    stored copy that a failed re-assembly left behind can't mislead the gate.
    """
    keys = keys_for(sample_id)
    fetched = []
    for name in GATE_INPUTS:
        key = keys[name]
        local = os.path.join(root, *key.split("/"))
        os.makedirs(os.path.dirname(local), exist_ok=True)
        try:
            storage().download(key, local)  # one request; a missing file is the gate's to report
        except ObjectNotFound:
            continue
        provenance.note_input(key, local)
        fetched.append(key)
    record = provenance.read_record(storage(), sample_id)
    if record is not None:
        local = os.path.join(root, *provenance.record_key(sample_id).split("/"))
        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local, "w") as fh:
            json.dump(record, fh)
        fetched.append(provenance.record_key(sample_id))
    return fetched


def _store_no_verdict(sample_id: str, key: str, wd: str, mode: str, message: str) -> None:
    """The gate gave no verdict: store that under the verdict key too, so an earlier run's verdict
    there can't pass for this run's (nothing is ever deleted from the bucket), and record it."""
    path = os.path.join(wd, "no-verdict.json")
    with open(path, "w") as fh:
        json.dump(
            {
                "sample_id": sample_id,
                "run_name": os.environ.get("RUN_NAME") or None,
                "verdict": None,
                "mode": mode,
                "error": message,
            },
            fh,
        )
    upload_output(path, key)
    provenance.note(gate={"verdict": None, "mode": mode, "error": message, "key": key})


def _gate_problem(sample_id: str, key: str, wd: str, mode: str, message: str) -> None:
    """The gate couldn't give a verdict (not installed, timed out, crashed, unreadable verdict).
    warn: say so, store and record it; fail: the same, then fail the stage."""
    _store_no_verdict(sample_id, key, wd, mode, message)
    if mode == "fail":
        raise RuntimeError(f"quality gate: {message}")
    print(f"WARNING: quality gate: {message} (GATE_MODE=warn: the run continues)", flush=True)


def _read_verdict(path: str) -> dict:
    """The verdict JSON, or ValueError if it isn't one (missing file, bad JSON, missing fields)."""
    try:
        with open(path) as fh:
            verdict = json.load(fh)
    except (OSError, json.JSONDecodeError) as err:
        raise ValueError(f"no readable verdict: {err}") from err
    missing = [k for k in ("verdict", "reasons", "config") if k not in verdict]
    if missing or verdict["verdict"] not in ("pass", "fail"):
        raise ValueError(f"not a gate verdict (missing {missing or 'a pass/fail verdict'})")
    return verdict


@timed_stage(GATE_METRIC)
def gate_task(sample_id):
    """geyser-gate on the sample's fastp report, stats/{id}/ and run record -> qc_reports/{id}.gate.json."""
    mode = gate_mode()  # a mistyped GATE_MODE fails now, as ANNOTATE does
    # Expanded here: Batch and `docker run -e` pass values as they are (e.g. $GATE_THRESHOLDS_DIR/...).
    config = os.path.expandvars(os.environ.get("GATE_CONFIG", "").strip()) or None
    key = keys_for(sample_id)["gate_verdict"]
    wd = work_dir(sample_id)
    try:
        root = os.path.join(wd, "root")
        os.makedirs(root)
        fetched = fetch_gate_inputs(sample_id, root)
        print(f"Quality gate ({mode}) on {len(fetched)} inputs: {', '.join(fetched) or 'none'}", flush=True)
        verdict_path = os.path.join(wd, os.path.basename(key))
        cmd = [GATE_BINARY, "--root", root, "--sample", sample_id, "--output", verdict_path]
        if config:
            cmd += ["--config", config]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=GATE_TIMEOUT_SECONDS)
        except FileNotFoundError:
            return _gate_problem(
                sample_id, key, wd, mode, f"{GATE_BINARY} is not installed (an image from before #144?)"
            )
        except subprocess.TimeoutExpired:
            return _gate_problem(sample_id, key, wd, mode, f"{GATE_BINARY} timed out after {GATE_TIMEOUT_SECONDS}s")
        if result.stderr:
            print(result.stderr.rstrip(), flush=True)  # one line per check, then PASS/FAIL
        if result.returncode == 2:
            # A usage or config error (e.g. a GATE_CONFIG with a typo): an operator setting, so it fails
            # in either mode, like a mistyped GATE_MODE.
            message = f"could not run (exit 2): {result.stderr.strip()}"
            _store_no_verdict(sample_id, key, wd, mode, message)
            raise RuntimeError(f"quality gate {message}")
        if result.returncode not in (0, 1):
            # A crash (e.g. a panic, which aborts: a negative code here) is the gate's problem, not the run's.
            return _gate_problem(sample_id, key, wd, mode, f"{GATE_BINARY} exited {result.returncode}")
        try:
            verdict = _read_verdict(verdict_path)
        except ValueError as err:
            return _gate_problem(sample_id, key, wd, mode, str(err))
        upload_output(verdict_path, key)
        summary = {
            "verdict": verdict["verdict"],
            "reasons": verdict["reasons"],
            "mode": mode,
            "config": verdict["config"],
            "gate_version": verdict.get("gate_version"),
            "key": key,
        }
        provenance.note(gate=summary)
        provenance.note_tools("geyser-gate")
        if verdict["verdict"] != "pass":
            reasons = "; ".join(verdict["reasons"])
            if mode == "fail":
                raise GateFailed(f"{sample_id} failed the quality gate: {reasons}")
            print(f"WARNING: {sample_id} failed the quality gate: {reasons} (GATE_MODE=warn: the run continues)")
    finally:
        shutil.rmtree(wd, ignore_errors=True)


# --- v1.4 "Big inputs": pre-flight, chunked alignment, the streaming merge (docs/design/genome-scale-app.md) -----
#
# A genome too big for one align job takes the large route: preflight checks and splits the raw reads in one
# streaming pass, align_chunk aligns each chunk (no duplicate marking: duplicates can sit in different chunks),
# and align_merge merges the chunk BAMs in place, marks duplicates once over all of them and writes exactly what
# align_task writes. Everything lives under the run's folder (RUN_NAME), so one run never reads another's chunks.

PREFLIGHT_METRIC = "Preflight"
CHUNK_METRIC = "AlignChunk"
MERGE_METRIC = "Align"
DEFAULT_CHUNKS = 24
MAX_CHUNKS = 999
MANIFEST_SCHEMA = 1
CHUNKS_PREFIX = INTERMEDIATE_PREFIX + "chunks/{sample_id}/{run}/"
ALIGNED_PREFIX = INTERMEDIATE_PREFIX + "aligned/{sample_id}/{run}/"
CHUNK_GZIP_LEVEL = 1  # chunks live for days and are read once: speed over size (pigz -1 when the image has it)
CRAI_ETAG_METADATA = "cram-etag"  # the .crai's object metadata: the ETag of the CRAM it indexes


class PreflightFailed(RuntimeError):
    """The raw reads failed the pre-flight check (corrupt or truncated gzip, malformed FASTQ, mates out of step)."""


def _run_folder() -> str:
    return _safe_name(os.environ.get("RUN_NAME") or "unnamed")


def chunks_prefix(sample_id: str) -> str:
    return CHUNKS_PREFIX.format(sample_id=sample_id, run=_run_folder())


def aligned_prefix(sample_id: str) -> str:
    return ALIGNED_PREFIX.format(sample_id=sample_id, run=_run_folder())


def manifest_key(sample_id: str) -> str:
    return chunks_prefix(sample_id) + "manifest.json"


def preflight_report_key(sample_id: str) -> str:
    return f"qc_reports/{sample_id}.preflight.json"


def chunk_name(index: int) -> str:
    return f"chunk-{index:03d}"


def chunk_keys(sample_id: str, index: int, layout: str) -> dict:
    """keys_for-style names of one chunk's reads: raw (single-end) or raw_R1/raw_R2 (paired-end)."""
    base = chunks_prefix(sample_id) + chunk_name(index)
    if layout == SINGLE:
        return {"raw": base + ".fastq.gz"}
    return {f"raw_{m}": f"{base}_{m}.fastq.gz" for m in MATES}


def _read_name(header: bytes) -> bytes:
    """A FASTQ header's read name, for checking mates: up to the first space, without a /1 or /2 suffix."""
    name = header[1:].split(None, 1)[0] if header[1:].strip() else b""
    return name[:-2] if name[-2:] in (b"/1", b"/2") else name


def _fastq_records(fh, label: str):
    """Yield one 4-line FASTQ record (bytes) at a time, checking its shape. Raises PreflightFailed."""
    number = 0
    while True:
        header = fh.readline()
        if not header:
            return
        seq, plus, qual = fh.readline(), fh.readline(), fh.readline()
        number += 1
        if not header.startswith(b"@") or not plus.startswith(b"+") or not qual:
            raise PreflightFailed(f"{label}: record {number} is not a FASTQ record")
        if len(seq.rstrip(b"\r\n")) != len(qual.rstrip(b"\r\n")):
            raise PreflightFailed(f"{label}: record {number} has a sequence and quality of different lengths")
        yield header, header + seq + plus + qual


class _ChunkWriter:
    """A gzip file written at CHUNK_GZIP_LEVEL: through pigz when it is installed (much faster), else gzip."""

    def __init__(self, path: str):
        self.path = path
        if shutil.which("pigz"):
            self._file = open(path, "wb")
            self._proc = subprocess.Popen(
                ["pigz", f"-{CHUNK_GZIP_LEVEL}", "-c"], stdin=subprocess.PIPE, stdout=self._file
            )
            self._out = self._proc.stdin
        else:
            self._file = self._proc = None
            self._out = gzip.open(path, "wb", compresslevel=CHUNK_GZIP_LEVEL)

    def write(self, data: bytes) -> None:
        self._out.write(data)

    def close(self) -> None:
        self._out.close()
        if self._proc:
            if self._proc.wait() != 0:
                raise RuntimeError(f"pigz failed writing {os.path.basename(self.path)}")
            self._file.close()


def split_reads(streams, labels, n_chunks: int, wd: str, layout: str, sample_id: str) -> dict:
    """One pass over the raw reads (one stream per mate, gzipped): check them and deal record i to chunk i mod N.

    Round-robin keeps chunk sizes within one read of each other without knowing the total in advance, and keeps
    mates together (record i of R1 and R2 go to the same chunk, in the same order). Decompressing is the gzip
    integrity check: a truncated or corrupt file fails here, in minutes, before any alignment. Paired-end mates
    must have the same read names in the same order and the same number of reads. Memory is bounded: one record
    per mate is in flight. Returns {"reads", "chunks": [{"index", "reads", "paths": {name: local path}}]}.
    """
    names = ["raw"] if layout == SINGLE else [f"raw_{m}" for m in MATES]
    chunks = []
    for i in range(n_chunks):
        keys = chunk_keys(sample_id, i, layout)
        chunks.append(
            {
                "index": i,
                "reads": 0,
                "paths": {n: os.path.join(wd, os.path.basename(keys[n])) for n in names},
                "writers": None,
            }
        )
    readers = []
    try:
        for chunk in chunks:
            chunk["writers"] = {n: _ChunkWriter(p) for n, p in chunk["paths"].items()}
        for stream, label in zip(streams, labels, strict=True):
            readers.append(_fastq_records(gzip.GzipFile(fileobj=stream, mode="rb"), label))
        total = 0
        try:
            while True:
                records = [next(r, None) for r in readers]
                if all(r is None for r in records):
                    break
                if any(r is None for r in records):
                    shorter = labels[records.index(None)]
                    raise PreflightFailed(f"the mates have different read counts ({shorter} ends first, after {total})")
                if len(records) == 2 and _read_name(records[0][0]) != _read_name(records[1][0]):
                    raise PreflightFailed(
                        f"mates out of step at read {total + 1}: "
                        f"{_read_name(records[0][0]).decode(errors='replace')} vs "
                        f"{_read_name(records[1][0]).decode(errors='replace')}"
                    )
                chunk = chunks[total % n_chunks]
                for name, (_, record) in zip(names, records, strict=True):
                    chunk["writers"][name].write(record)
                chunk["reads"] += 1
                total += 1
        except (EOFError, OSError, gzip.BadGzipFile, zlib.error) as err:
            raise PreflightFailed(f"the gzip stream is truncated or corrupt: {type(err).__name__}: {err}") from err
        if total == 0:
            raise PreflightFailed("the input has no reads")
        return {"reads": total, "chunks": chunks}
    finally:
        for chunk in chunks:
            for writer in (chunk.get("writers") or {}).values():
                try:
                    writer.close()
                except Exception:  # noqa: BLE001 - closing after a failure; the first error is the one raised
                    pass


def _preflight_report(sample_id, layout, input_keys, input_bytes, ok, error, reads, n_chunks, manifest, seconds):
    return {
        "schema": MANIFEST_SCHEMA,
        "sample_id": sample_id,
        "run_name": os.environ.get("RUN_NAME") or None,
        "layout": layout,
        "ok": ok,
        "error": error,
        "input": {"keys": input_keys, "bytes": input_bytes},
        "reads": reads,
        "chunk_count": n_chunks if ok else None,
        "manifest": manifest if ok else None,
        "seconds": round(seconds, 1),
    }


@timed_stage(PREFLIGHT_METRIC)
def preflight_task(sample_id, n_chunks=DEFAULT_CHUNKS, manifest_out=None):
    """Check the raw reads and split them into n_chunks chunks: the chunks, the manifest, the pre-flight report."""
    if not 1 <= n_chunks <= MAX_CHUNKS:
        raise ValueError(f"--chunks must be 1-{MAX_CHUNKS}, got {n_chunks}")
    started = time.time()
    layout = sample_layout(sample_id)  # a lone mate or both layouts is a LayoutError, before any read
    input_keys = keys_of(keys_for(sample_id, layout), "raw")
    input_bytes = sum(storage().head(k)["size"] or 0 for k in input_keys)
    manifest_out = manifest_out or manifest_key(sample_id)
    provenance.note(layout=layout)
    for key in input_keys:
        provenance.note_input(key)
    wd = work_dir(sample_id)
    error, result = None, None
    try:
        streams = [storage().open_read(k) for k in input_keys]
        try:
            result = split_reads(streams, input_keys, n_chunks, wd, layout, sample_id)
        except PreflightFailed as err:
            error = str(err)
            raise
        finally:
            for s in streams:
                close = getattr(s, "close", None)
                if close:
                    close()
        chunks = []
        for chunk in result["chunks"]:
            keys = chunk_keys(sample_id, chunk["index"], layout)
            size = 0
            for name, path in chunk["paths"].items():
                size += os.path.getsize(path)
                storage().upload(path, keys[name])
                os.remove(path)  # free the disk as we go
            chunks.append(
                {
                    "index": chunk["index"],
                    "name": chunk_name(chunk["index"]),
                    "reads": chunk["reads"],
                    "bytes": size,
                    "keys": keys,
                    "aligned": aligned_prefix(sample_id) + chunk_name(chunk["index"]) + ".bam",
                }
            )
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "sample_id": sample_id,
            "run_name": os.environ.get("RUN_NAME") or None,
            "layout": layout,
            "reads": result["reads"],
            "chunk_count": n_chunks,
            "input": {"keys": input_keys, "bytes": input_bytes},
            "aligned_prefix": aligned_prefix(sample_id),
            "chunks": chunks,
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        path = os.path.join(wd, "manifest.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=1)
        storage().upload(path, manifest_out)
        provenance.note(preflight={"reads": result["reads"], "chunks": n_chunks, "input_bytes": input_bytes})
        print(f"Pre-flight OK: {result['reads']} reads ({layout}) in {n_chunks} chunks -> {manifest_out}", flush=True)
    finally:
        report = _preflight_report(
            sample_id,
            layout,
            input_keys,
            input_bytes,
            error is None and result is not None,
            error,
            result["reads"] if result else None,
            n_chunks,
            manifest_out,
            time.time() - started,
        )
        report_path = os.path.join(wd, "preflight.json")
        try:
            with open(report_path, "w", encoding="utf-8") as fh:
                json.dump(report, fh, indent=1)
            upload_output(report_path, preflight_report_key(sample_id))
        except Exception as err:  # noqa: BLE001 - the report must not hide the check's own error
            print(f"WARNING: could not write the pre-flight report: {err}", flush=True)
        shutil.rmtree(wd, ignore_errors=True)


def read_manifest(sample_id: str) -> dict:
    """This run's manifest (RUN_NAME). A missing one names the key: preflight didn't run for this run."""
    key = manifest_key(sample_id)
    try:
        fh = storage().open_read(key)
        try:
            manifest = json.loads(fh.read())
        finally:
            fh.close()
    except ObjectNotFound:
        raise FileNotFoundError(f"no chunk manifest at {storage().describe(key)}: run preflight first") from None
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ValueError(f"unknown manifest schema {manifest.get('schema')!r} at {key}")
    return manifest


def chunk_align_command(
    ref: str, fastqs: list, out: str, threads: int, tmp_dir: str, sample_id: str, aligner: str
) -> str:
    """align_command without markdup: <aligner> mem (the sample's one read group) | [fixmate -m] | sort -> BAM.

    Duplicates are marked once, over all chunks, in align_merge (a duplicate's twin can sit in another chunk).
    The read group is the sample's, as in align_command, so the merged CRAM has one read group, as a single job's.
    """
    check_aligner(aligner)
    if aligner == GIRAFFE:
        raise ValueError("the chunked route aligns with bwa-mem2 or bwa; giraffe isn't supported there yet")
    read_group = rf"@RG\tID:{sample_id}\tSM:{sample_id}"
    sort_threads = max(1, min(threads, SORT_MAX_THREADS))
    tmp = os.path.join(tmp_dir, "tmp")
    stages = [
        f"{aligner} mem -t {threads} -R {shlex.quote(read_group)} {shlex.quote(ref)} "
        + " ".join(shlex.quote(f) for f in fastqs)
    ]
    if len(fastqs) == 2:
        stages.append("samtools fixmate -m -u - -")
    stages.append(
        f"samtools sort -l 1 -@ {sort_threads} -m {SORT_MEMORY_PER_THREAD} -T {shlex.quote(tmp + '.sort')} "
        f"-o {shlex.quote(out)} -"
    )
    return " | ".join(stages)


@timed_stage(CHUNK_METRIC, run_record=False)
def align_chunk_task(sample_id, reference_name, chunk):
    """Chunk `chunk` of this run's manifest -> its coordinate-sorted BAM (no markdup) under intermediate/aligned/."""
    aligner = aligner_from_env()
    manifest = read_manifest(sample_id)
    entry = next((c for c in manifest["chunks"] if c["index"] == chunk), None)
    if entry is None:
        raise ValueError(f"chunk {chunk} is not in the manifest (it has {manifest['chunk_count']} chunks)")
    names = ["raw"] if manifest["layout"] == SINGLE else [f"raw_{m}" for m in MATES]
    wd = work_dir(sample_id)
    try:
        fastqs = []
        for name in names:
            fastqs.append(os.path.join(wd, os.path.basename(entry["keys"][name])))
            storage().download(entry["keys"][name], fastqs[-1])
        threads = os.cpu_count() or 1
        ref = ensure_reference_local(reference_name, wd, need_bwa_index=False)
        aligner = prepare_aligner(reference_name, ref, aligner)
        out = os.path.join(wd, os.path.basename(entry["aligned"]))
        print(f"Aligning {entry['name']} ({entry['reads']} reads) with {aligner}", flush=True)
        run_shell(chunk_align_command(ref, fastqs, out, threads, wd, sample_id, aligner))
        storage().upload(out, entry["aligned"])
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def merge_command(ref: str, inputs_file: str, out: str, threads: int, tmp_dir: str) -> str:
    """samtools merge (the chunk BAMs, read in place: the paths or presigned URLs in inputs_file) | markdup -> CRAM.

    The chunks are each coordinate-sorted, so merge keeps the order; markdup then sees every read of the sample
    at once, as in a single job (it needs the fixmate tags, which align_chunk added for paired-end reads).
    """
    tmp = os.path.join(tmp_dir, "tmp")
    return (
        f"samtools merge -@ {threads} -u -b {shlex.quote(inputs_file)} - "  # the first positional: the output, stdout
        f"| samtools markdup -s -@ {threads} -T {shlex.quote(tmp + '.markdup')} "
        f"--reference {shlex.quote(ref)} -O cram - {shlex.quote(out)}"
    )


def upload_alignment(cram: str, crai: str, keys: dict) -> None:
    """The .crai, then the CRAM (the stage-done marker), then the .crai again with the CRAM's ETag as metadata.

    Readers that stream a region (variants --region) trust a stored .crai only if its cram-etag equals the CRAM's
    current ETag. Between the two .crai uploads it has none, so they fall back to downloading: no window in which a
    stale index is trusted, and the index-before-CRAM order (#99) is kept."""
    upload_output(crai, keys["crai"])
    upload_output(cram, keys["cram"])
    try:
        etag = storage().head(keys["cram"])["etag"]
        storage().upload(crai, keys["crai"], metadata={CRAI_ETAG_METADATA: etag})
    except Exception as err:  # noqa: BLE001 - only streaming needs it; readers fall back without it
        print(f"WARNING: could not record the CRAM's ETag on its index ({err}); calling will download", flush=True)


@timed_stage(MERGE_METRIC)  # the Align stage's own metric and run-record part: the merge *is* the sample's align
def align_merge_task(sample_id, reference_name, threads=None):
    """Every chunk BAM of this run -> the sample's duplicate-marked CRAM + .crai and stats: align_task's outputs.

    threads: samtools merge/markdup/index and the stats tools (default: the job's CPUs; a run's merge_threads)."""
    manifest = read_manifest(sample_id)
    missing = [c["aligned"] for c in manifest["chunks"] if not storage().exists(c["aligned"])]
    if missing:
        raise FileNotFoundError(f"{len(missing)} chunk BAM(s) missing, e.g. {', '.join(missing[:3])}")
    layout = manifest["layout"]
    keys = keys_for(sample_id, layout)
    provenance.note(aligner=aligner_from_env(), layout=layout, chunks=manifest["chunk_count"])
    provenance.note_tools(aligner_from_env(), "samtools", "mosdepth")
    for c in manifest["chunks"]:
        provenance.note_input(c["aligned"])
    wd = work_dir(sample_id)
    try:
        threads = threads or os.cpu_count() or 1
        ref = ensure_reference_local(reference_name, wd, need_bwa_index=False)
        inputs = os.path.join(wd, "chunks.txt")
        with open(inputs, "w", encoding="utf-8") as fh:  # in place: local paths, or presigned HTTPS URLs on S3
            fh.write("\n".join(storage().presigned_url(c["aligned"]) for c in manifest["chunks"]) + "\n")
        cram = os.path.join(wd, os.path.basename(keys["cram"]))
        crai = os.path.join(wd, os.path.basename(keys["crai"]))
        print(f"Merging {manifest['chunk_count']} chunk BAMs ({manifest['reads']} reads)", flush=True)
        run_shell(merge_command(ref, inputs, cram, threads, wd))
        run_shell(f"samtools index -@ {threads} -o {shlex.quote(crai)} {shlex.quote(cram)}")
        for path, key in alignment_stats(ref, cram, wd, threads, keys):
            upload_output(path, key)
        upload_alignment(cram, crai, keys)
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def preflight_main(argv) -> int:
    """tasks.py preflight <srr_id> [--chunks N] [--manifest-key KEY]: check and split the raw reads (large route)."""
    parser = argparse.ArgumentParser(prog="tasks.py preflight", description=preflight_main.__doc__)
    parser.add_argument("srr_id")
    parser.add_argument("--chunks", type=int, default=DEFAULT_CHUNKS)
    parser.add_argument("--manifest-key", help="where to write the manifest (default: the run's chunk folder)")
    args = parser.parse_args(argv)
    if not 1 <= args.chunks <= MAX_CHUNKS:
        parser.error(f"--chunks must be 1-{MAX_CHUNKS}")
    try:
        preflight_task(args.srr_id, args.chunks, args.manifest_key)
    except PreflightFailed as err:
        print(f"PreflightFailed: {err}", flush=True)
        return 1
    return 0


def align_chunk_main(argv) -> int:
    """tasks.py align_chunk <srr_id> <reference_name> --chunk K: align one chunk of this run's manifest."""
    parser = argparse.ArgumentParser(prog="tasks.py align_chunk", description=align_chunk_main.__doc__)
    parser.add_argument("srr_id")
    parser.add_argument("reference_name")
    parser.add_argument("--chunk", type=int, required=True, help="the manifest's chunk index (0-based)")
    args = parser.parse_args(argv)
    if args.chunk < 0:
        parser.error("--chunk must be >= 0")
    align_chunk_task(args.srr_id, args.reference_name, args.chunk)
    return 0


def align_merge_main(argv) -> int:
    """tasks.py align_merge <srr_id> <reference_name>: merge this run's chunk BAMs into the sample's CRAM."""
    parser = argparse.ArgumentParser(prog="tasks.py align_merge", description=align_merge_main.__doc__)
    parser.add_argument("srr_id")
    parser.add_argument("reference_name")
    parser.add_argument("--threads", type=int, help="samtools threads (default: the job's CPUs)")
    args = parser.parse_args(argv)
    if args.threads is not None and args.threads < 1:
        parser.error("--threads must be >= 1")
    align_merge_task(args.srr_id, args.reference_name, args.threads)
    return 0


TASKS = {
    "qc": (qc_task, False),
    "align": (align_task, True),
    "variants": (variants_task, True),
    "gate": (gate_task, False),
    # v1.4's large route (docs/design/genome-scale-app.md). Their CLIs are their own *_main (other options and exit
    # codes); listed here so the state machine and tasks.py agree on the stages (tests/test_consistency.py).
    "preflight": (preflight_task, False),
    "align_chunk": (align_chunk_task, True),
    "align_merge": (align_merge_task, True),
}
SUBCOMMANDS = {"preflight": preflight_main, "align_chunk": align_chunk_main, "align_merge": align_merge_main}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["regions"]:
        return regions_main(argv[1:])
    if argv[:1] and argv[0] in SUBCOMMANDS:
        return SUBCOMMANDS[argv[0]](argv[1:])
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
