"""AlphaGenome's "variants of interest" for one sample, as the detail page shows them (3.0, P4).

The annotate stage (app/alphagenome.py, P1) writes stats/<id>/alphagenome.tsv: one row per scored variant,
ranked, with its AlphaGenome Variant Impact score (AVI), the track it moves most, the gene, bcftools csq's
consequence and where the score came from (atlas, api, avi-file, or fake in tests). Rows from the AVI file
have no gene or track (it holds the score only); the page shows a dash. Its run-record part ("Annotate")
says how the stage went. Annotation is opt-in per run and never fails a run, so every outcome here is a
note on the page, never an error: not annotated, annotated with nothing of interest, failed, or rows.

Research use only: AlphaGenome's terms forbid clinical use, and the page says so wherever the table is.
"""

import csv
import io
import math
from dataclasses import dataclass, field

from geyser_web.pipeline import ObjectNotFound, keys_for

STAGE = "Annotate"  # the run-record part's task name
KEY_NAME = "alphagenome_tsv"  # keys_for() name from #207; until that merges, the path below
TERMS_URL = "https://deepmind.google.com/science/alphagenome/output-terms"  # shown with every published table
MAX_TSV_BYTES = 20 * 1024 * 1024  # capped at ALPHAGENOME_MAX_VARIANTS rows (500 by default): ~100 KB

# TSV header -> field: the columns the page shows, read by name from the output contract (#207, TSV_COLUMNS:
# rank chrom pos ref alt kind qual avi avi_raw top_track gene rna_lfc csq source). The rest are ignored.
COLUMNS = {
    "rank": "rank",  # not shown: the file's order (AVI, then |rna_lfc|, then QUAL), used to break AVI ties
    "chrom": "chrom",
    "pos": "pos",
    "ref": "ref",
    "alt": "alt",
    "gene": "gene",
    "avi": "avi",
    "top_track": "top_track",
    "csq": "consequence",
    "source": "source",
    "rna_lfc": "rna_lfc",  # RNA_SEQ log fold change (ALT vs REF, signed) of the gene's expression (Atlas rows only)
}
REQUIRED = ("chrom", "pos", "ref", "alt", "avi")
SORTS = ("desc", "asc")  # by AVI; rows without a score go last either way


def annotation_key(sample_id: str) -> str:
    """Where the annotate stage writes the table: keys_for()'s name for it, or stats/<id>/alphagenome.tsv."""
    return keys_for(sample_id).get(KEY_NAME) or f"stats/{sample_id}/alphagenome.tsv"


@dataclass(frozen=True)
class Scored:
    chrom: str
    pos: int
    ref: str
    alt: str
    avi: float | None
    gene: str = ""
    top_track: str = ""
    consequence: str = ""
    source: str = ""
    rna_lfc: float | None = None
    rank: int | None = None  # the file's rank, if it has one


@dataclass
class AnnotationView:
    state: str  # rows | empty | not_annotated | failed | unreadable
    rows: list = field(default_factory=list)  # at most `limit` Scored, in `sort` order, from row `offset` on
    total: int = 0  # every row in the file
    offset: int = 0  # how many rows (in `sort` order) come before rows[0]
    sort: str = "desc"
    sources: list = field(default_factory=list)  # [(source, count)], most first
    error: str = ""  # failed / unreadable: why
    stage: dict = field(default_factory=dict)  # the run record's Annotate stage, as recorded

    @property
    def truncated(self) -> bool:
        return self.total > len(self.rows)

    @property
    def first(self) -> int:
        return self.offset + 1 if self.rows else 0

    @property
    def last(self) -> int:
        return self.offset + len(self.rows)

    @property
    def current(self) -> bool:
        """This run's annotation is on disk and readable: its files may be offered for download."""
        return self.state in ("rows", "empty")

    @property
    def test_data(self) -> bool:
        """Scores from the fake backend: fixtures, not AlphaGenome's predictions."""
        return any(source == "fake" for source, _ in self.sources)


def _float(value: str):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def parse_tsv(text: str) -> tuple[list, str]:
    """(rows, problem): every row as a Scored, or a reason the table can't be read ('' when it can).
    A row with an unreadable position is a broken file; one without a usable AVI keeps its place, unscored.
    Comment lines before the header ('#', e.g. AlphaGenome's terms notice) are skipped."""
    lines = text.splitlines(keepends=True)
    while lines and lines[0].startswith("#"):
        lines.pop(0)
    reader = csv.DictReader(io.StringIO("".join(lines)), delimiter="\t")
    header = [h.strip() for h in reader.fieldnames or []]
    missing = [name for name in REQUIRED if name not in header]
    if missing:
        return [], f"columns missing: {', '.join(missing)}"
    rows = []
    for line, raw in enumerate(reader, start=2):
        values = {COLUMNS[k.strip()]: (v or "").strip() for k, v in raw.items() if k and k.strip() in COLUMNS}
        try:
            pos = int(values["pos"])
        except ValueError:
            return [], f"line {line}: position {values['pos']!r} is not a number"
        rows.append(
            Scored(
                chrom=values["chrom"],
                pos=pos,
                ref=values["ref"],
                alt=values["alt"],
                avi=_float(values["avi"]),
                gene=values.get("gene", ""),
                top_track=values.get("top_track", ""),
                consequence=values.get("consequence", ""),
                source=values.get("source", ""),
                rna_lfc=_float(values.get("rna_lfc")),
                rank=int(values["rank"]) if values.get("rank", "").isdigit() else None,
            )
        )
    return rows, ""


def sort_rows(rows, order: str = "desc") -> list:
    """By AVI, highest first (or lowest with 'asc'); unscored rows last. Ties keep the file's rank (#207 breaks
    them by |rna_lfc|, then QUAL), so the default order is the downloaded table's; then by position."""
    sign = 1 if order == "asc" else -1
    return sorted(
        rows,
        key=lambda r: (
            r.avi is None,
            sign * (r.avi or 0.0),
            r.rank if r.rank is not None else math.inf,
            r.chrom,
            r.pos,
            r.ref,
            r.alt,
        ),
    )


def read_annotation(storage, sample_id: str, record=None, order: str = "desc", limit: int = 500, offset: int = 0):
    """The sample's variants of interest, in every state the page can show. Never raises.

    record: the assembled run record. When it exists but has no Annotate stage, this run wasn't annotated,
    and a table on disk is an earlier run's (nothing deletes it): not shown as this sample's. Without a
    record (imported data, runs from before the record), the table is trusted, as the annotated VCF is.
    """
    order = order if order in SORTS else "desc"
    stage = ((record or {}).get("stages") or {}).get(STAGE)
    stage = stage if isinstance(stage, dict) else {}
    if record and not stage:
        return AnnotationView(state="not_annotated", sort=order)
    if stage.get("status") == "Failure":  # a part is written when its stage ends: Success or Failure
        return AnnotationView(state="failed", sort=order, stage=stage, error=stage.get("error") or "no reason recorded")
    try:
        with storage.open_read(annotation_key(sample_id)) as fh:
            raw = fh.read(MAX_TSV_BYTES + 1)
    except ObjectNotFound:
        if stage:  # the stage ran but its table isn't there
            return AnnotationView(state="unreadable", sort=order, stage=stage, error="the table is missing")
        return AnnotationView(state="not_annotated", sort=order)
    except Exception as err:  # noqa: BLE001 - the panel is extra; never fail the sample page over it
        return AnnotationView(state="unreadable", sort=order, stage=stage, error=f"could not read the table: {err}")
    if len(raw) > MAX_TSV_BYTES:
        return AnnotationView(state="unreadable", sort=order, stage=stage, error="table too large to show")
    rows, problem = parse_tsv(raw.decode("utf-8", errors="replace"))
    if problem:
        return AnnotationView(state="unreadable", sort=order, stage=stage, error=problem)
    counts = {}
    for row in rows:
        counts[row.source or "unknown"] = counts.get(row.source or "unknown", 0) + 1
    if offset >= len(rows):  # past the end (a stale link): the last page
        offset = max(0, (len(rows) - 1) // limit * limit) if rows else 0
    return AnnotationView(
        state="rows" if rows else "empty",
        rows=sort_rows(rows, order)[offset : offset + limit],
        total=len(rows),
        offset=offset,
        sort=order,
        sources=sorted(counts.items(), key=lambda item: (-item[1], item[0])),
        stage=stage,
    )
