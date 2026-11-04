"""The signature figures (#189) as interactive 3D terrains on /figures/ (web plan W1b).

Each figure is a grid: one row per category (a sample, a read set), one column per position along x, a value
per cell. The rows are categories, so the 3D view draws each as its own ribbon and never interpolates between
them. The page shows a small inline-SVG ridgeline of the same grid first (the poster: no request, no effect on
LCP); the WebGL renderer (static/samples/surface.js) and the grid's JSON load only when the chart scrolls into
view. The CSV the grid comes from is the table view.

Data: the derived CSVs in docs/figures/csv/ (GEYSER_FIGURES_DIR), committed by #189: per-window or per-cycle
summaries, no reads or calls.
"""

import csv
import json
import math
import os
import statistics
from dataclasses import dataclass, field
from functools import lru_cache
from xml.sax.saxutils import escape

from django.conf import settings

# The sequential ramp (dataviz reference: one hue, blue), from the dark end that recedes into the page to the light
# end that stands out: low values dark, high values light, so it reads by lightness alone (colour-blind safe).
RAMP = [
    "#0d366b",
    "#104281",
    "#184f95",
    "#1c5cab",
    "#256abf",
    "#2a78d6",
    "#3987e5",
    "#5598e7",
    "#6da7ec",
    "#86b6ef",
    "#9ec5f4",
    "#b7d3f6",
    "#cde2fb",
]


@dataclass(frozen=True)
class Figure:
    name: str
    title: str
    csv: str
    description: str
    x_label: str
    x_unit: str
    rows_label: str
    rows_plural: str  # how the caption counts the rows: "7 alignments"
    z_label: str
    z_unit: str
    clip_factor: float | None = None  # heights above factor × median are drawn at the cap (the readout is exact)
    zero_based: bool = True  # False: heights start at a stated baseline below the data (narrow-range values)
    pending: str = ""  # shown while the CSV doesn't exist yet
    note: str = ""  # a caveat shown on the card (e.g. how far the calls can be compared)


@dataclass
class Grid:
    figure: Figure
    x: list  # column positions, ascending
    rows: list  # row names, in the CSV's order
    values: list  # values[row][col]: float, or None where the CSV has no cell
    clip: float | None = None  # the height cap (None: no cap)
    x_ticks: list = field(default_factory=list)  # [(position, label)] when x is a transform (log2 batch size)
    base: float = 0.0  # the value at zero height (stated on the axis, the legend and the caption)
    vmin: float = 0.0
    vmax: float = 0.0
    extra: dict = field(default_factory=dict)


FIGURES = {
    "coverage": Figure(
        name="coverage",
        title="Coverage terrain",
        csv="coverage-terrain.csv",
        description="Mean read depth along chromosome 20 in 500 kb windows, one ridge per alignment of the same "
        "GIAB samples. The pile-up at the centromere (around 26-30 Mb) is reads from repeats that map nowhere "
        "else; the vg giraffe ridges smooth some of it out.",
        x_label="Position on chr20",
        x_unit="Mb",
        rows_label="Alignment",
        rows_plural="alignments",
        z_label="Mean depth",
        z_unit="×",
        clip_factor=2.5,
    ),
    "quality": Figure(
        name="quality",
        title="Per-cycle base quality",
        csv="quality-surface.csv",
        description="fastp's mean base quality at each sequencing cycle, before filtering, one ridge per read file. "
        "Quality falls towards the end of each read, and R2 sits a little below R1.",
        x_label="Sequencing cycle",
        x_unit="cycle",
        rows_label="Read file",
        rows_plural="read files",
        z_label="Mean base quality",
        z_unit="Phred",
        zero_based=False,
    ),
    "dv-stage": Figure(
        name="dv-stage",
        title="DeepVariant: batch size × shards",
        csv="dv-batch-shards.csv",
        description="Wall time of the whole DeepVariant stage on HG003 chr20 35x (local RTX 3080), over call_variants' "
        "batch size and the number of make_examples shards. Shards set the time; batch size hardly matters up to "
        "512 (scripts/benchmark_dv_grid.py, #216).",
        x_label="Batch size",
        x_unit="log₂ scale",
        rows_label="make_examples shards",
        rows_plural="shard settings",
        z_label="Stage time",
        z_unit="min",
        note="The calls differ by up to ±2 between cells (GPU nondeterminism in call_variants), so the cells are "
        "compared on time only, not on calls.",
    ),
    "dv-call": Figure(
        name="dv-call",
        title="DeepVariant: call_variants alone",
        csv="dv-batch-shards.csv",
        description="The GPU step on its own: call_variants takes 100-108 s at batch 128-512 and about 20% longer at "
        "1024 (Google's default), whatever the shards; the GPU stays only 26-33% busy.",
        x_label="Batch size",
        x_unit="log₂ scale",
        rows_label="make_examples shards",
        rows_plural="shard settings",
        z_label="call_variants time",
        z_unit="s",
        note="The calls differ by up to ±2 between cells (GPU nondeterminism in call_variants), so the cells are "
        "compared on time only, not on calls.",
    ),
    "cost": Figure(
        name="cost",
        title="Cost × turnaround",
        csv="cost-surface.csv",
        description="On-demand cost per run over align size and variant-calling window, from the cloud grid "
        "(docs/figures/cost-surface-grid.md).",
        x_label="Variant window",
        x_unit="Mb",
        rows_label="Align size",
        rows_plural="align sizes",
        z_label="Cost per run",
        z_unit="US cents",
        pending="Waiting for the cost grid: the runs that measure it (#192's per-run overrides) haven't been made "
        "yet. This chart appears here as soon as docs/figures/csv/cost-surface.csv exists.",
    ),
}


def csv_path(figure: Figure) -> str:
    return os.path.join(settings.GEYSER_FIGURES_DIR, figure.csv)


def _float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _rows_of(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _assemble(figure, cells):
    """cells: [(row name, x, value)] -> a Grid with rows in first-seen order and x ascending."""
    rows, xs = [], set()
    for row, x, _ in cells:
        if row not in rows:
            rows.append(row)
        xs.add(x)
    x = sorted(xs)
    col = {v: i for i, v in enumerate(x)}
    values = [[None] * len(x) for _ in rows]
    index = {r: i for i, r in enumerate(rows)}
    for row, xv, value in cells:
        values[index[row]][col[xv]] = value
    present = [v for line in values for v in line if v is not None]
    if not present:
        raise ValueError(f"{figure.csv}: no values")
    clip = None
    if figure.clip_factor:
        clip = round(figure.clip_factor * statistics.median(present), 2)
    vmin = min(present)
    vmax = max(present)
    # A stated baseline a quarter of the spread below the lowest value, rounded down (Phred 33.5-36.3 -> 32).
    base = 0.0 if figure.zero_based else float(math.floor(vmin - 0.25 * (vmax - vmin)))
    return Grid(figure, x, rows, values, clip=clip, base=base, vmin=vmin, vmax=vmax)


def _coverage(figure, path):
    cells = []
    for r in _rows_of(path):
        start, end, depth = _float(r.get("start")), _float(r.get("end")), _float(r.get("mean_depth"))
        if start is None or end is None:
            continue
        cells.append((r["label"], round((start + end) / 2 / 1e6, 3), depth))
    return _assemble(figure, cells)


def _quality(figure, path):
    cells = []
    for r in _rows_of(path):
        cycle = _float(r.get("cycle"))
        if cycle is None:
            continue
        cells.append((f"{r['label']} {r['read']}", int(cycle), _float(r.get("mean"))))
    return _assemble(figure, cells)


def _cost(figure, path):
    # cost_surface.py --table: align_vcpu, align_memory_mb, window_mb, median_cost_usd (one row per grid cell).
    cells = []
    for r in _rows_of(path):
        window, cost = _float(r.get("window_mb")), _float(r.get("median_cost_usd"))
        if window is None:
            continue
        name = f"{r.get('align_vcpu', '?')} vCPU / {int(_float(r.get('align_memory_mb')) or 0) // 1024} GiB"
        cells.append((name, window, None if cost is None else round(cost * 100, 3)))
    return _assemble(figure, cells)


def _dv(figure, path, column, scale):
    # dv_batch_shards.py's per-cell table (#216): batch_size, shards, ..., stage_s, call_variants_s, ...
    cells, batches = [], set()
    for r in _rows_of(path):
        batch, shards, value = _float(r.get("batch_size")), _float(r.get("shards")), _float(r.get(column))
        if not batch or batch <= 0 or shards is None:
            continue
        batches.add(int(batch))
        cells.append(
            (f"{int(shards)} shards", round(math.log2(batch), 4), None if value is None else round(value * scale, 3))
        )
    grid = _assemble(figure, cells)
    grid.x_ticks = [(round(math.log2(b), 4), str(b)) for b in sorted(batches)]
    return grid


BUILDERS = {
    "coverage": _coverage,
    "quality": _quality,
    "dv-stage": lambda figure, path: _dv(figure, path, "stage_s", 1 / 60),
    "dv-call": lambda figure, path: _dv(figure, path, "call_variants_s", 1),
    "cost": _cost,
}


@lru_cache(maxsize=16)
def _load(name: str, path: str, mtime: float):
    return BUILDERS[name](FIGURES[name], path)


def load(name: str):
    """The figure's Grid, or None if its CSV doesn't exist (yet). Re-read when the file changes."""
    figure = FIGURES[name]
    path = csv_path(figure)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    return _load(name, path, mtime)


def grid_json(grid: Grid) -> str:
    """What surface.js draws: axes, rows, values (2 decimals; null where there's no cell) and the height cap."""
    f = grid.figure
    return json.dumps(
        {
            "title": f.title,
            "x": {"label": f.x_label, "unit": f.x_unit, "values": grid.x, "ticks": grid.x_ticks},
            "rows": {"label": f.rows_label, "names": grid.rows},
            "z": {
                "label": f.z_label,
                "unit": f.z_unit,
                "min": grid.vmin,
                "max": grid.vmax,
                "clip": grid.clip,
                "base": grid.base,
            },
            "values": [[None if v is None else round(v, 2) for v in line] for line in grid.values],
        },
        separators=(",", ":"),
    )


# --- The poster: a 2D ridgeline of the same grid, inline SVG ---------------------------------------------------

POSTER_W, POSTER_H = 640, 360
MARGIN_L, MARGIN_R, MARGIN_T, MARGIN_B = 8, 8, 10, 26


def poster_svg(grid: Grid) -> str:
    """One ridge per row, stacked like the 3D view seen from the front, in the ramp's colours (back rows darker).
    Small on purpose: the x axis is resampled to at most 120 points per ridge. Labels are escaped."""
    f = grid.figure
    n = len(grid.rows)
    top = grid.clip or grid.vmax or 1
    step = max(1, math.ceil(len(grid.x) / 120))
    x0, x1 = grid.x[0], grid.x[-1] if grid.x[-1] != grid.x[0] else grid.x[0] + 1
    plot_w = POSTER_W - MARGIN_L - MARGIN_R
    band = (POSTER_H - MARGIN_T - MARGIN_B) / (n + 1.5)  # each ridge rises up to 2.5 bands: overlap like a ridgeline
    paths = []
    for i, name in enumerate(grid.rows):
        base = MARGIN_T + band * (i + 2.5)
        points = []
        for j in range(0, len(grid.x), step):
            v = grid.values[i][j]
            if v is None:
                continue
            px = MARGIN_L + (grid.x[j] - x0) / (x1 - x0) * plot_w
            py = base - max(0.0, min(v, top) - grid.base) / (top - grid.base) * band * 2.5
            points.append(f"{px:.1f},{py:.1f}")
        if not points:
            continue
        line = " ".join(points)
        first, last = points[0].split(",")[0], points[-1].split(",")[0]
        shade = RAMP[min(len(RAMP) - 1, 3 + round((n - 1 - i) / max(1, n - 1) * 5))]
        paths.append(
            f'<path d="M{first},{base:.1f} L{line} L{last},{base:.1f}Z" fill="{RAMP[1]}" fill-opacity=".92" '
            f'stroke="{shade}" stroke-width="1.5"><title>{escape(name)}</title></path>'
        )
    ticks = []
    named = grid.x_ticks or [(x0 + (x1 - x0) * k / 4, f"{x0 + (x1 - x0) * k / 4:.3g}") for k in range(5)]
    for value, text in named:
        frac = (value - x0) / (x1 - x0)
        px = MARGIN_L + plot_w * frac
        anchor = "start" if frac <= 0.01 else "end" if frac >= 0.99 else "middle"
        ticks.append(f'<text x="{px:.0f}" y="{POSTER_H - 8}" text-anchor="{anchor}">{escape(text)}</text>')
    label = escape(
        f"{f.title}: {f.z_label} ({f.z_unit}) by {f.x_label.lower()} ({f.x_unit}), one ridge per {f.rows_label.lower()}"
    )
    return (
        f'<svg class="poster" viewBox="0 0 {POSTER_W} {POSTER_H}" role="img" aria-label="{label}">'
        + "".join(paths)
        + f'<g class="ticks">{"".join(ticks)}</g></svg>'
    )
