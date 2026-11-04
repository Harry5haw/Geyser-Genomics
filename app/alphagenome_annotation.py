# app/alphagenome_annotation.py
"""AlphaGenome variant-effect annotation (3.0 "Project Alpha Genome"): selection, scoring backends, outputs.

`tasks.py annotate <id> <reference> --source avi-file|atlas|api|fake` reads the sample's calls, picks the variants
worth scoring (select), asks a backend for their scores, and writes:

- variants/{id}.alphagenome.vcf.gz (+ .tbi): the scored variants, sites only, with the INFO tags in INFO_HEADERS
  (AG_AVI, AG_AVI_RAW, AG_TOP_TRACK, AG_GENE, AG_RNA_LFC, AG_SOURCE);
- stats/{id}/alphagenome.tsv: the same variants ranked, the "variants of interest" table (TSV_COLUMNS).

Research use only (AlphaGenome's terms), never clinical; no model may be trained on the outputs. Opt-in per run,
and an extra stage: a failed annotation is noted in the run record but never fails the run.

Sources (backend_for; the P0 spike's decision record, 4 Nov 2026):
- avi-file: the official AVI download (bgzip TSV `#CHROM POS REF ALT raw_score PHRED`, GRCh38, SNVs only, no chrM),
  a per-reference extract at reference/alphagenome/<reference>.avi.tsv.gz. A local lookup: no key, no calls, $0.
  AVI only (no gene or track in the file). AVI is a deleteriousness score (PHRED 20 = top 1% genome-wide).
- atlas: the Atlas API (alphagenome.atlas.AtlasClient): precomputed per-track and per-gene scores, so it fills
  AG_GENE, AG_TOP_TRACK and AG_RNA_LFC (the RNA_SEQ log fold change for the top gene). SNVs only. Needs the key
  and the client (requirements-alphagenome.txt, ~500 MB with its dependencies: not in the pipeline image).
- api: score_variant (dna_client) with the recommended scorers over a 1 Mb window (API_INTERVAL), one request per
  variant: indels and anything Atlas lacks. Needs the key. TODO(P2): as for atlas.
- fake: deterministic, offline, free, every field filled. Not predictions (AG_SOURCE=fake): for tests and the offline
  pipeline. Recorded responses from ALPHAGENOME_FAKE_RESPONSES where given, else values derived from a hash.

No AWS or storage here: tasks.py does the I/O. The key is read from $ALPHAGENOME_API_KEY only, and is never
logged, stored or put in an error message: client errors are redacted (redact) before they go anywhere.
Never commit AVI or Atlas data (fixtures included): tests use synthetic tables.
"""

import gzip
import hashlib
import json
import math
import os
from dataclasses import dataclass

SOURCES = ("avi-file", "atlas", "api", "fake")
API_KEY_ENV = "ALPHAGENOME_API_KEY"
MAX_VARIANTS_ENV = "ALPHAGENOME_MAX_VARIANTS"
DEFAULT_MAX_VARIANTS = 500  # the API is sized for "small to medium jobs"; chr20 has ~70k PASS SNVs per sample
FAKE_RESPONSES_ENV = "ALPHAGENOME_FAKE_RESPONSES"
# score_variant's interval must be one of these (dna_client SEQUENCE_LENGTH_16KB/_100KB/_500KB/_1MB); the
# gene-level scorers (RNA_SEQ) need the 1 Mb window, centred on the variant.
API_INTERVALS = (16_384, 131_072, 524_288, 1_048_576)
API_INTERVAL = 1_048_576
AVI_FILE_COLUMNS = ("#CHROM", "POS", "REF", "ALT", "raw_score", "PHRED")
# AlphaGenome's Output Terms ask for a conspicuous notice on published Outputs and Derivatives (the AVI score
# excepted). Every output carries it: the VCF as a header line, the TSV as its first line (a '#' comment that
# readers skip). Both are derived outputs: selected, ranked and reduced from AlphaGenome's scores.
TERMS_NOTICE = (
    "By using this information, you agree to AlphaGenome Output Terms of Use found at "
    "https://deepmind.google.com/science/alphagenome/output-terms"
)
TERMS_LINE = f"AlphaGenomeTerms={TERMS_NOTICE}"

# bcftools csq consequences that are non-coding but near a gene: where a regulatory effect is plausible and a
# protein-level one isn't the whole story. Coding-only variants (missense, synonymous, ...) are csq's territory;
# variants with no consequence (intergenic) aren't near a gene csq knows.
NEAR_GENE_CONSEQUENCES = frozenset(
    {"5_prime_utr", "3_prime_utr", "splice_region", "splice_donor", "splice_acceptor", "intron", "non_coding"}
)

INFO_HEADERS = (
    '##INFO=<ID=AG_AVI,Number=1,Type=Float,Description="AlphaGenome Variant Impact (AVI), PHRED-scaled (0 to ~80; '
    '20 = top 1% genome-wide): a deleteriousness score. Research use only">',
    '##INFO=<ID=AG_AVI_RAW,Number=1,Type=Float,Description="AlphaGenome Variant Impact (AVI), raw score">',
    '##INFO=<ID=AG_TOP_TRACK,Number=1,Type=String,Description="The AlphaGenome track (output type:tissue or cell '
    'type) with the largest predicted REF vs ALT change">',
    '##INFO=<ID=AG_GENE,Number=1,Type=String,Description="The gene of the strongest predicted expression effect, '
    'else the gene bcftools csq names">',
    '##INFO=<ID=AG_RNA_LFC,Number=1,Type=Float,Description="AlphaGenome RNA_SEQ log fold change (ALT vs REF) of '
    'AG_GENE expression, in AG_TOP_TRACK where that is an RNA_SEQ track">',
    '##INFO=<ID=AG_SOURCE,Number=1,Type=String,Description="Where the scores came from: avi-file (the AVI download), '
    'atlas (precomputed, Atlas API), api (scored on request) or fake (deterministic test data, not a prediction)">',
)
TSV_COLUMNS = (
    "rank",
    "chrom",
    "pos",
    "ref",
    "alt",
    "kind",
    "qual",
    "avi",
    "avi_raw",
    "top_track",
    "gene",
    "rna_lfc",
    "csq",
    "source",
)


@dataclass(frozen=True)
class Variant:
    """One ALT allele of a called record: what a backend scores."""

    chrom: str
    pos: int
    ref: str
    alt: str
    qual: float  # None when the VCF has "."
    consequences: tuple = ()  # bcftools csq consequence types on the record, in BCSQ order
    genes: tuple = ()  # the genes csq names, in BCSQ order
    bcsq: str = None  # the record's BCSQ value as csq wrote it (kept in the output VCF)

    @property
    def key(self) -> str:
        return f"{self.chrom}:{self.pos}:{self.ref}:{self.alt}"

    @property
    def kind(self) -> str:
        return "snv" if len(self.ref) == len(self.alt) == 1 else "indel"


@dataclass(frozen=True)
class Score:
    """A backend's answer for one variant. Fields a source doesn't have are None (avi-file: AVI only)."""

    source: str
    avi: float = None  # PHRED-scaled AVI
    avi_raw: float = None
    top_track: str = None
    gene: str = None
    rna_lfc: float = None


def redact(text, secret) -> str:
    """text with every occurrence of secret replaced: for client errors, which may echo the request."""
    text = str(text)
    return text.replace(secret, "<redacted>") if secret else text


# --- reading the calls -----------------------------------------------------------------------------------


def _open_text(path):
    with open(path, "rb") as fh:
        gzipped = fh.read(2) == b"\x1f\x8b"
    return gzip.open(path, "rt") if gzipped else open(path)


def _bcsq_value(info: str):
    return next((item[5:] for item in info.split(";") if item.startswith("BCSQ=")), None)


def parse_bcsq(info: str) -> tuple:
    """(consequences, genes) from a record's INFO BCSQ (bcftools csq: Consequence|gene|transcript|biotype|...).

    Entries starting with '@' point at another record's consequence (a compound variant) and are skipped; a
    leading '*' (a consequence downstream of a stop) is dropped. Consequences may be '&'-joined.
    """
    consequences, genes = [], []
    for entry in (_bcsq_value(info) or "").split(","):
        if not entry or entry.startswith("@"):
            continue
        fields = entry.lstrip("*").split("|")
        for consequence in fields[0].split("&"):
            if consequence and consequence not in consequences:
                consequences.append(consequence)
        if len(fields) > 1 and fields[1] and fields[1] not in genes:
            genes.append(fields[1])
    return tuple(consequences), tuple(genes)


def read_vcf(path):
    """(header lines, contig names in order, has BCSQ, records) of a VCF (plain or bgzipped).

    records: one dict per data line with chrom, pos, ref, alts, qual, filter and info.
    """
    header, contigs, records = [], [], []
    with _open_text(path) as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line.startswith("##"):
                header.append(line)
                if line.startswith("##contig=<ID="):
                    contigs.append(line[len("##contig=<ID=") :].split(",")[0].rstrip(">"))
                continue
            if line.startswith("#") or not line:
                continue
            cols = line.split("\t")
            if len(cols) < 8:
                raise ValueError(f"not a VCF data line (fewer than 8 columns): {line[:80]!r}")
            records.append(
                {
                    "chrom": cols[0],
                    "pos": int(cols[1]),
                    "ref": cols[3].upper(),
                    "alts": [a.upper() for a in cols[4].split(",")],
                    "qual": None if cols[5] == "." else float(cols[5]),
                    "filter": cols[6],
                    "info": cols[7],
                }
            )
    has_bcsq = any(line.startswith("##INFO=<ID=BCSQ,") for line in header)
    return header, contigs, has_bcsq, records


# --- selection -------------------------------------------------------------------------------------------


def max_variants_from_env(env=None) -> int:
    """ALPHAGENOME_MAX_VARIANTS (default DEFAULT_MAX_VARIANTS): a whole number >= 1, else ValueError."""
    env = os.environ if env is None else env
    raw = (env.get(MAX_VARIANTS_ENV) or "").strip()
    if not raw:
        return DEFAULT_MAX_VARIANTS
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise ValueError(f"{MAX_VARIANTS_ENV} must be a whole number >= 1, got {raw!r}")
    return value


def _allele_kind(ref: str, alt: str):
    """'snv', 'indel', or None for what isn't scored: symbolic or missing ALTs, MNPs, anything not ACGT."""
    if alt in (".", "*") or alt.startswith("<") or not set(ref + alt) <= set("ACGT"):
        return None
    if len(ref) == len(alt):
        return "snv" if len(ref) == 1 else None
    return "indel"


def _tie_break(key: str) -> bytes:
    return hashlib.sha256(key.encode()).digest()[:8]


def select(records, contigs, has_consequences: bool, max_variants: int, kinds=("snv", "indel")):
    """The alleles to score, ranked, and the counts behind the choice.

    PASS records only ('.' counts as PASS: bcftools call sets no filters). One candidate per scorable ALT
    allele (SNVs and indels; not symbolic ALTs or MNPs). With bcftools csq consequences, only alleles of records
    with a near-gene non-coding consequence (NEAR_GENE_CONSEQUENCES). Ranked by QUAL (highest first; '.' last) and
    capped at max_variants. kinds: what the source can score (the backend's KINDS): atlas and avi-file take SNVs
    only, so their cap isn't spent on indels they would leave unscored (other_kind counts those). Ties are broken by a stable hash of the variant, not by position: bcftools caps QUAL
    (~225), so thousands of calls tie at the top, and a positional tie-break would pick the first few hundred along
    the chromosome instead of a spread. The same calls always give the same selection. (contigs: the VCF's contig
    order, unused since ties stopped being positional; kept so callers needn't change.)
    """
    counts = {"records": len(records), "pass": 0, "snvs": 0, "indels": 0, "not_scorable": 0, "other_kind": 0}
    if has_consequences:
        counts["near_gene"] = 0
    candidates = []
    for rec in records:
        if rec["filter"] not in ("PASS", "."):
            continue
        counts["pass"] += 1
        consequences, genes = parse_bcsq(rec["info"]) if has_consequences else ((), ())
        near_gene = not has_consequences or bool(NEAR_GENE_CONSEQUENCES.intersection(consequences))
        for alt in rec["alts"]:
            kind = _allele_kind(rec["ref"], alt)
            if kind is None:
                counts["not_scorable"] += 1
                continue
            counts[f"{kind}s"] += 1
            if kind not in kinds:
                counts["other_kind"] += 1
                continue
            if not near_gene:
                continue
            if has_consequences:
                counts["near_gene"] += 1
            bcsq = _bcsq_value(rec["info"]) if has_consequences else None
            candidates.append(
                Variant(rec["chrom"], rec["pos"], rec["ref"], alt, rec["qual"], consequences, genes, bcsq)
            )
    candidates.sort(key=lambda v: (-(v.qual if v.qual is not None else -math.inf), _tie_break(v.key)))
    selected = candidates[:max_variants]
    counts["candidates"] = len(candidates)
    counts["selected"] = len(selected)
    counts["dropped_by_cap"] = len(candidates) - len(selected)
    return selected, counts


# --- backends --------------------------------------------------------------------------------------------
# Each has: name, model_version, api_calls (requests made), and score(variants) -> {variant key: Score}; a
# variant it can't score is left out (counted as not scored).


class FakeBackend:
    """Deterministic scores for tests and the offline pipeline. Not predictions (AG_SOURCE=fake).

    responses: {variant key: {"avi", "avi_raw", "top_track", "gene", "rna_lfc"}} recorded in a JSON file;
    anything not recorded gets values derived from a hash of its key, so the same calls always give the same
    output. It acts like the API (one call per variant), so call counting and the cap are exercised offline.
    """

    name = "fake"
    KINDS = ("snv", "indel")
    model_version = "fake-1"
    TRACKS = ("RNA_SEQ", "CAGE", "DNASE", "ATAC", "CHIP_HISTONE", "CHIP_TF", "SPLICE_SITES", "SPLICE_JUNCTIONS")
    TISSUES = ("liver", "brain_cortex", "whole_blood", "lung", "skeletal_muscle", "K562", "HepG2", "GM12878")

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.api_calls = 0

    @classmethod
    def from_file(cls, path):
        with open(path, encoding="utf-8") as fh:
            return cls(json.load(fh))

    def score(self, variants) -> dict:
        scores = {}
        for v in variants:
            self.api_calls += 1
            recorded = self.responses.get(v.key)
            if recorded is not None:
                scores[v.key] = Score(
                    self.name, **{k: recorded.get(k) for k in Score.__dataclass_fields__ if k != "source"}
                )
                continue
            d = hashlib.sha256(v.key.encode()).digest()
            unit = int.from_bytes(d[:4], "big") / 2**32
            scores[v.key] = Score(
                self.name,
                avi=round(80 * unit**3, 2),  # mostly low, a few high: AVI's shape
                avi_raw=round(-1.3 + 5.3 * unit, 4),
                top_track=f"{self.TRACKS[d[4] % len(self.TRACKS)]}:{self.TISSUES[d[5] % len(self.TISSUES)]}",
                gene=(v.genes[0] if v.genes else f"FAKEG{d[6] % 50}"),
                rna_lfc=round((int.from_bytes(d[7:9], "big") / 65536 - 0.5) * 2, 4),
            )
        return scores


class AviFileBackend:
    """The official AVI download (or a per-reference extract of it): SNVs only, AVI only, a local lookup.

    The table is the download's bgzip TSV, `#CHROM POS REF ALT raw_score PHRED` (AVI_FILE_COLUMNS). It is
    streamed once, keeping only the selected SNVs (a chr20 extract is ~2 GB). No key, no API calls.
    """

    name = "avi-file"
    KINDS = ("snv",)  # the AVI download has SNVs only

    def __init__(self, table_path, model_version=None):
        self.table_path = table_path
        self.model_version = model_version  # the file's name/date (provenance records its MD5 as an input)
        self.api_calls = 0

    def score(self, variants) -> dict:
        wanted = {v.key for v in variants if v.kind == "snv"}
        scores = {}
        if not wanted:
            return scores
        with _open_text(self.table_path) as fh:
            first = fh.readline().rstrip("\n").split("\t")
            if tuple(first[: len(AVI_FILE_COLUMNS)]) != AVI_FILE_COLUMNS:
                raise ValueError(
                    f"{os.path.basename(self.table_path)} is not an AVI table: header {first[:6]}, "
                    f"expected {list(AVI_FILE_COLUMNS)}"
                )
            for line in fh:
                cols = line.rstrip("\n").split("\t")
                if len(cols) < 6:
                    continue
                key = f"{cols[0]}:{cols[1]}:{cols[2].upper()}:{cols[3].upper()}"
                if key in wanted:
                    scores[key] = Score(self.name, avi=float(cols[5]), avi_raw=float(cols[4]))
                    if len(scores) == len(wanted):
                        break
        return scores


class _KeyedBackend:
    """A backend that calls AlphaGenome with the user's key: kept out of repr, logs and errors."""

    def __init__(self, api_key):
        import threading

        if not api_key:
            raise ValueError(f"--source {self.name} needs an AlphaGenome API key in ${API_KEY_ENV}")
        self._api_key = api_key
        self._lock = threading.Lock()
        self.api_calls = 0

    def __repr__(self):
        return f"{type(self).__name__}(api_key=<hidden>)"

    def _call(self, fn, *args, **kwargs):
        """One request to the service: counted, and any error re-raised with the key redacted (no chained
        traceback, which would carry the original text). The client raises ValueError for NOT_FOUND and
        INVALID_ARGUMENT: that variant isn't in the service (NotInService), not a failure of the annotation."""
        with self._lock:
            self.api_calls += 1
        try:
            return fn(*args, **kwargs)
        except ValueError as err:
            raise NotInService(f"{self.name}: {redact(err, self._api_key)}") from None
        except Exception as err:  # noqa: BLE001 - re-raised, redacted
            raise RuntimeError(f"{self.name}: {type(err).__name__}: {redact(err, self._api_key)}") from None


# The Atlas effect scorers (variant_scorers.RECOMMENDED_VARIANT_SCORERS without the *_ACTIVE ones, which measure
# activity, not an effect, and CONTACT_MAPS). Each returns per-track scores and a signed 'quantiles' layer in
# [-1, 1] that makes scorers comparable; gene scorers (RNA_SEQ, SPLICE_*, POLYADENYLATION) have a row per gene.
ATLAS_SCORERS = (
    "RNA_SEQ",
    "SPLICE_SITES",
    "SPLICE_SITE_USAGE",
    "SPLICE_JUNCTIONS",
    "POLYADENYLATION",
    "CAGE",
    "PROCAP",
    "DNASE",
    "ATAC",
    "CHIP_TF",
    "CHIP_HISTONE",
)
NOT_FOUND_EXAMPLES = 5  # variants the service didn't have, kept in the run record to tell coverage from a bug
ATLAS_WORKERS = 4  # concurrent lookups (the client's own default is 10; no published rate limit)
CLIENT_HINT = "pip install -r requirements-alphagenome.txt"


class NotInService(Exception):
    """The service has no score for this variant (NOT_FOUND / INVALID_ARGUMENT): not scored, not a failure."""


def _matrix(values) -> list:
    """A 2-D score array (numpy, from AnnData) as nested lists."""
    values = values.tolist() if hasattr(values, "tolist") else values
    return [list(row) for row in values]


def _records(frame) -> list:
    """A DataFrame's rows as dicts (or a list of dicts as it is); [] for None."""
    if frame is None:
        return []
    return frame.to_dict("records") if hasattr(frame, "to_dict") else list(frame)


def _largest(matrix):
    """(row, column, value) of the largest |value| in a 2-D list, or None if it is empty. NaN is skipped."""
    best = None
    for r, row in enumerate(matrix):
        for c, value in enumerate(row):
            if value is None or value != value:  # NaN
                continue
            if best is None or abs(value) > abs(best[2]):
                best = (r, c, value)
    return best


def _track_label(scorer: str, track: dict) -> str:
    """'<scorer>:<histone mark>:<biosample>' (as available), else '<scorer>:<track name>'."""
    parts = [scorer]
    if track.get("histone_mark"):
        parts.append(str(track["histone_mark"]))
    parts.append(str(track.get("biosample_name") or track.get("name") or "track"))
    return ":".join(parts)


def reduce_atlas(result) -> Score:
    """One variant's Atlas answer ({scorer: AnnData}) as a Score.

    top_track: the track with the largest |quantile| over every scorer (quantiles are comparable across scorers;
    raw scores are not). gene and rna_lfc: RNA_SEQ's gene x track cell with the largest |quantile|, and that cell's
    raw score, the signed log fold change of the gene's expression, ALT vs REF. The Atlas has no AVI (avi-file).
    None if the answer is empty.
    """
    top = None  # (|quantile|, label)
    for scorer in sorted(result):
        data = result[scorer]
        layers = getattr(data, "layers", None) or {}
        quantiles = _matrix(layers["quantiles"]) if "quantiles" in layers else _matrix(data.X)
        found = _largest(quantiles)
        if found is None:
            continue
        tracks = _records(data.var)
        label = _track_label(scorer, tracks[found[1]] if found[1] < len(tracks) else {})
        if top is None or abs(found[2]) > top[0]:
            top = (abs(found[2]), label)
    gene = rna_lfc = None
    rna = result.get("RNA_SEQ")
    if rna is not None:
        layers = getattr(rna, "layers", None) or {}
        raw = _matrix(rna.X)
        found = _largest(_matrix(layers["quantiles"]) if "quantiles" in layers else raw)
        genes = _records(rna.obs)
        if found is not None and found[0] < len(genes):
            row = genes[found[0]]
            gene = row.get("gene_name") or row.get("gene_id") or None
            rna_lfc = round(float(raw[found[0]][found[1]]), 4)
    if top is None and gene is None:
        return None
    return Score("atlas", top_track=top[1] if top else None, gene=gene, rna_lfc=rna_lfc)


class AtlasBackend(_KeyedBackend):
    """Precomputed per-track and per-gene scores from the Atlas API (alphagenome.atlas.AtlasClient): the top
    track, the top gene and its RNA_SEQ log fold change (reduce_atlas). SNVs only; no AVI (that is avi-file's).

    One query_variant per selected SNV, ATLAS_WORKERS at a time (query_variants would fail the whole batch on one
    missing variant). A variant the Atlas doesn't have is left unscored; anything else (the key refused, the
    service down) fails the annotation, which is noted and never fatal. The client (requirements-alphagenome.txt)
    is imported only here.
    """

    name = "atlas"
    KINDS = ("snv",)  # precomputed for SNVs; indels go to api

    def __init__(self, api_key, client=None, workers=ATLAS_WORKERS, make_variant=None):
        super().__init__(api_key)
        self._client = client
        self._make_variant = make_variant  # genome.Variant by default (tests pass their own: no client needed)
        self.workers = workers
        self.not_found = 0
        self.empty_answers = 0
        self.not_found_reasons = {}  # the service's message (digits as N) -> count
        self.not_found_examples = []  # (variant key, message), the first NOT_FOUND_EXAMPLES
        self.model_version = f"alphagenome-client {_client_version()} (Atlas)"

    def _connect(self):
        if self._client is None:
            try:
                from alphagenome.atlas import atlas
            except ImportError:
                raise RuntimeError(f"--source atlas needs the alphagenome client ({CLIENT_HINT})") from None
            try:
                self._client = atlas.create(self._api_key, timeout=60)
            except Exception as err:  # noqa: BLE001 - re-raised, redacted
                raise RuntimeError(
                    f"atlas: could not connect: {type(err).__name__}: {redact(err, self._api_key)}"
                ) from None
        return self._client

    def score(self, variants) -> dict:
        snvs = [v for v in variants if v.kind == "snv"]
        if not snvs:
            return {}
        client = self._connect()
        from concurrent.futures import ThreadPoolExecutor

        make_variant = self._make_variant
        if make_variant is None:
            from alphagenome.data import genome

            make_variant = genome.Variant  # 1-based position, as in the VCF

        def one(v):
            try:
                result = self._call(
                    client.query_variant,
                    make_variant(v.chrom, v.pos, v.ref, v.alt),
                    requested_scorers=list(ATLAS_SCORERS),
                )
            except NotInService as err:
                self._note_not_found(v.key, str(err))
                return v.key, None
            score = reduce_atlas(result)
            if score is None:
                with self._lock:
                    self.empty_answers += 1
            return v.key, score

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            answers = list(pool.map(one, snvs))
        return {key: score for key, score in answers if score is not None}

    def _note_not_found(self, key, message):
        import re

        with self._lock:
            self.not_found += 1
            reason = re.sub(r"\d+", "N", message)[:160]
            self.not_found_reasons[reason] = self.not_found_reasons.get(reason, 0) + 1
            if len(self.not_found_examples) < NOT_FOUND_EXAMPLES:
                self.not_found_examples.append([key, message[:240]])

    def diagnostics(self) -> dict:
        """Why selected SNVs went unscored, for the run record (messages are already redacted)."""
        reasons = sorted(self.not_found_reasons.items(), key=lambda kv: -kv[1])[:5]
        return {
            "not_in_service": self.not_found,
            "empty_answers": self.empty_answers,
            "not_in_service_reasons": dict(reasons),
            "not_in_service_examples": self.not_found_examples,
        }


def _client_version():
    try:
        from importlib.metadata import version

        return version("alphagenome")
    except Exception:  # noqa: BLE001 - only a label
        return "unknown"


class ApiBackend(_KeyedBackend):
    """score_variant (alphagenome.models.dna_client) with the recommended scorers, a 1 Mb interval centred on each
    variant (API_INTERVAL): one request per variant, for indels and anything Atlas lacks.

    TODO(P2): dna_client.create(key), then per variant genome.Interval / genome.Variant and score_variant; reduce
    the per-scorer AnnData to the top track, the top gene and its RNA_SEQ LFC. Not wired in until the user
    approves real calls.
    """

    name = "api"
    KINDS = ("snv", "indel")
    model_version = None

    def __init__(self, api_key, interval=API_INTERVAL):
        super().__init__(api_key)
        if interval not in API_INTERVALS:
            raise ValueError(f"score_variant intervals must be one of {API_INTERVALS}, got {interval}")
        self.interval = interval

    def score(self, variants) -> dict:
        if not variants:
            return {}
        raise NotImplementedError("the score_variant backend is not wired in yet (P2); use --source avi-file or fake")


def backend_for(source: str, table=None, env=None):
    """The backend for --source. table: the local AVI table (avi-file; tasks.py downloads it)."""
    env = os.environ if env is None else env
    if source == "fake":
        path = (env.get(FAKE_RESPONSES_ENV) or "").strip()
        return FakeBackend.from_file(path) if path else FakeBackend()
    if source == "avi-file":
        if not table:
            raise ValueError("--source avi-file needs the AVI table for this reference")
        return AviFileBackend(table, os.path.basename(table))
    if source == "atlas":
        return AtlasBackend((env.get(API_KEY_ENV) or "").strip())
    if source == "api":
        return ApiBackend((env.get(API_KEY_ENV) or "").strip())
    raise ValueError(f"unknown AlphaGenome source {source!r}; expected one of {SOURCES}")


# --- outputs ---------------------------------------------------------------------------------------------


def _info_value(text) -> str:
    """A string safe in a VCF INFO value (no ; = , or whitespace)."""
    return "".join("_" if c in ";=, \t" else c for c in str(text))


def _num(value) -> str:
    return "" if value is None else f"{value:g}"


def scored_rows(variants, scores) -> list:
    """(variant, score) for each variant the backend scored, in the selection's order."""
    return [(v, scores[v.key]) for v in variants if v.key in scores]


def _gene(v: Variant, s: Score):
    return s.gene or (v.genes[0] if v.genes else None)


def vcf_text(rows, header, contigs, reference_name: str, source: str, model_version) -> str:
    """The annotated VCF: sites only, sorted by position (as tabix needs), with the AG_* INFO tags a source has.

    Contig and BCSQ header lines come from the input; BCSQ is kept on each record when the input had it.
    """
    order = {name: i for i, name in enumerate(contigs)}
    bcsq_header = [line for line in header if line.startswith("##INFO=<ID=BCSQ,")]
    lines = [
        "##fileformat=VCFv4.2",
        "##source=geyser-alphagenome",
        f"##reference={reference_name}",
        f"##alphagenome_source={source}" + (" (deterministic test data, not predictions)" if source == "fake" else ""),
        f"##alphagenome_model_version={_info_value(model_version or 'unknown')}",
        f"##{TERMS_LINE}",
        "##alphagenome_use=research use only; not for clinical use",
        *[line for line in header if line.startswith("##contig=")],
        *INFO_HEADERS,
        *bcsq_header,
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO",
    ]
    for v, s in sorted(rows, key=lambda r: (order.get(r[0].chrom, len(order)), r[0].chrom, r[0].pos, r[0].alt)):
        info = []
        for tag, value in (("AG_AVI", s.avi), ("AG_AVI_RAW", s.avi_raw)):
            if value is not None:
                info.append(f"{tag}={value:g}")
        if s.top_track:
            info.append(f"AG_TOP_TRACK={_info_value(s.top_track)}")
        if _gene(v, s):
            info.append(f"AG_GENE={_info_value(_gene(v, s))}")
        if s.rna_lfc is not None:
            info.append(f"AG_RNA_LFC={s.rna_lfc:g}")
        info.append(f"AG_SOURCE={s.source}")
        if bcsq_header and v.bcsq:
            info.append(f"BCSQ={v.bcsq}")
        lines.append(f"{v.chrom}\t{v.pos}\t.\t{v.ref}\t{v.alt}\t{_num(v.qual) or '.'}\tPASS\t{';'.join(info)}")
    return "\n".join(lines) + "\n"


def _rank_key(row):
    v, s = row
    return (
        -(s.avi if s.avi is not None else -math.inf),
        -(abs(s.rna_lfc) if s.rna_lfc is not None else -math.inf),
        -(v.qual if v.qual is not None else -math.inf),
    )


def tsv_text(rows) -> str:
    """The variants-of-interest table (TSV_COLUMNS): ranked by AVI, then |RNA_SEQ LFC|, then QUAL (highest
    first; a missing value last). Empty cells for what a source doesn't have. The first line is the terms notice,
    as a '#' comment; then the column header."""
    out = [f"#{TERMS_LINE}", "\t".join(TSV_COLUMNS)]
    for rank, (v, s) in enumerate(sorted(rows, key=_rank_key), start=1):
        cells = (
            rank,
            v.chrom,
            v.pos,
            v.ref,
            v.alt,
            v.kind,
            _num(v.qual),
            _num(s.avi),
            _num(s.avi_raw),
            s.top_track or "",
            _gene(v, s) or "",
            _num(s.rna_lfc),
            ",".join(v.consequences),
            s.source,
        )
        out.append("\t".join(str(c) for c in cells))
    return "\n".join(out) + "\n"
