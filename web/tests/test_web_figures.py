"""W1b: the signature figures as 3D terrains on /figures/ (grids, posters, data endpoints, budgets)."""

import gzip
import json
import os
import re

import pytest

from samples import figures

STATIC = os.path.join(os.path.dirname(figures.__file__), "static", "samples")
PAGE_BUDGET_BYTES = 30 * 1024

COVERAGE = "label,chrom,start,end,mean_depth\n" + "".join(
    f"{label},chr20,{i * 500000},{(i + 1) * 500000},{depth}\n"
    for label, depths in (("A · bwa", [30, 32, 300, 31]), ("B · giraffe", [29, 31, 60, 30]))
    for i, depth in enumerate(depths)
)
QUALITY = "label,read,cycle,mean,A,C,G,T\n" + "".join(
    f"{label},{read},{c},{q},{q},{q},{q},{q}\n"
    for label, read, start in (("S1", "R1", 36.0), ("S1", "R2", 35.0))
    for c, q in enumerate([start, start - 0.2, start - 1.5], start=1)
)


@pytest.fixture
def figdir(tmp_path, settings):
    settings.GEYSER_FIGURES_DIR = tmp_path
    figures._load.cache_clear()
    (tmp_path / "coverage-terrain.csv").write_bytes(COVERAGE.encode())
    (tmp_path / "quality-surface.csv").write_bytes(QUALITY.encode())
    yield tmp_path
    figures._load.cache_clear()


# ------------------------------------------------------------------ grids


def test_coverage_grid_rows_window_midpoints_and_cap(figdir):
    grid = figures.load("coverage")
    assert grid.rows == ["A · bwa", "B · giraffe"]
    assert grid.x == [0.25, 0.75, 1.25, 1.75]  # window midpoints in Mb
    assert grid.values[0] == [30, 32, 300, 31]
    assert grid.clip == round(2.5 * 31, 2)  # 2.5 × the median: the centromere spike is drawn at the cap
    assert (grid.base, grid.vmax) == (0.0, 300)


def test_quality_rows_per_read_file_and_a_stated_baseline(figdir):
    grid = figures.load("quality")
    assert grid.rows == ["S1 R1", "S1 R2"] and grid.x == [1, 2, 3]
    assert grid.clip is None
    assert grid.base < grid.vmin and grid.base == int(grid.base)  # below the data, a whole Phred value
    assert grid.base == 32  # 33.5 - 0.25 × (36.0 - 33.5) = 32.875, rounded down


def test_missing_cells_are_gaps_not_zeros(figdir):
    (figdir / "coverage-terrain.csv").write_bytes(
        COVERAGE.replace("B · giraffe,chr20,500000,1000000,31\n", "").encode()
    )
    grid = figures.load("coverage")
    assert grid.values[1][1] is None and grid.values[0][1] == 32


def test_a_figure_without_its_csv_is_pending(figdir):
    assert figures.load("cost") is None  # the cost grid hasn't been run


def test_the_cost_grid_builds_from_cost_surface_tables(figdir):
    (figdir / "cost-surface.csv").write_bytes(
        b"align_vcpu,align_memory_mb,window_mb,median_cost_usd\n2,4096,17,0.071\n2,4096,33,0.065\n4,8192,17,0.079\n4,8192,33,0.07\n"
    )
    grid = figures.load("cost")
    assert grid.rows == ["2 vCPU / 4 GiB", "4 vCPU / 8 GiB"] and grid.x == [17, 33]
    assert grid.values == [[7.1, 6.5], [7.9, 7.0]]  # cents


def test_the_committed_figure_data_loads(settings):
    # The real CSVs #189 committed: 7 alignments × 129 windows, 10 read files × 151 cycles.
    settings.GEYSER_FIGURES_DIR = settings.REPO_ROOT / "docs" / "figures" / "csv"
    figures._load.cache_clear()
    coverage, quality = figures.load("coverage"), figures.load("quality")
    assert (len(coverage.rows), len(coverage.x)) == (7, 129)
    assert (len(quality.rows), len(quality.x)) == (10, 151)
    figures._load.cache_clear()


# ------------------------------------------------------------------ poster and JSON


def test_the_poster_is_one_ridge_per_row_with_escaped_labels(figdir):
    (figdir / "coverage-terrain.csv").write_bytes(COVERAGE.replace("B · giraffe", "<b>x</b>").encode())
    svg = figures.poster_svg(figures.load("coverage"))
    assert svg.startswith('<svg class="poster"') and 'role="img"' in svg and "aria-label=" in svg
    assert svg.count("<path ") == 2
    assert "<b>x</b>" not in svg and "&lt;b&gt;x&lt;/b&gt;" in svg


def test_the_poster_resamples_long_rows(figdir):
    rows = "".join(f"A,chr20,{i * 1000},{(i + 1) * 1000},{30 + i % 7}\n" for i in range(1000))
    (figdir / "coverage-terrain.csv").write_bytes(("label,chrom,start,end,mean_depth\n" + rows).encode())
    svg = figures.poster_svg(figures.load("coverage"))
    points = re.search(r" L([^Z]*) L", svg).group(1).split()
    assert len(points) <= 120  # a small inline picture, whatever the grid size


def test_grid_json_is_what_the_renderer_reads(figdir):
    data = json.loads(figures.grid_json(figures.load("quality")))
    assert set(data) == {"title", "x", "rows", "z", "values"}
    assert data["x"] == {"label": "Sequencing cycle", "unit": "cycle", "values": [1, 2, 3]}
    assert data["rows"]["names"] == ["S1 R1", "S1 R2"]
    assert data["z"]["base"] == 32 and data["z"]["unit"] == "Phred" and data["z"]["clip"] is None
    assert data["values"][1] == [35.0, 34.8, 33.5]


# ------------------------------------------------------------------ the page and endpoints


def test_the_page_shows_posters_and_defers_the_3d(client, figdir):
    body = client.get("/figures/").content.decode()
    assert body.count('<svg class="poster"') == 2  # coverage and quality; cost is pending
    assert 'data-surface="/figures/coverage.json"' in body and 'data-surface="/figures/quality.json"' in body
    assert re.search(r'data-surface-js="/static/samples/surface[^"]*\.js"', body)
    assert re.search(r'data-surface-gl="/static/samples/surface-gl[^"]*\.js"', body)  # the WebGL half, for the worker
    assert "<script src=" in body and body.count("<script src=") == 1  # surface.js is not loaded with the page
    assert "Waiting for the cost grid" in body
    assert 'href="/figures/coverage.csv" download' in body
    assert "Heights start at 32 Phred, not 0" in body and "drawn at that cap" in body


def test_a_broken_csv_is_reported_on_its_card(client, figdir):
    (figdir / "quality-surface.csv").write_bytes(b"label,read,cycle,mean\n")
    body = client.get("/figures/").content.decode()
    assert "quality-surface.csv can&#x27;t be read" in body or "quality-surface.csv can't be read" in body
    assert body.count('<svg class="poster"') == 1


def test_data_endpoints(client, figdir):
    resp = client.get("/figures/coverage.json")
    assert resp.status_code == 200 and resp["Content-Type"] == "application/json"
    assert json.loads(resp.content)["rows"]["names"] == ["A · bwa", "B · giraffe"]
    csv_resp = client.get("/figures/coverage.csv")
    assert csv_resp.status_code == 200 and "attachment" in csv_resp["Content-Disposition"]
    assert b"".join(csv_resp.streaming_content).decode() == COVERAGE
    assert client.get("/figures/cost.json").status_code == 404  # pending
    assert client.get("/figures/nope.json").status_code == 404


def test_the_csv_download_is_never_prerendered(client, figdir):
    body = client.get("/figures/").content.decode()
    (rules,) = re.findall(r'<script type="speculationrules">(.*?)</script>', body, re.S)
    assert {"not": {"href_matches": "/figures/*.csv"}} in json.loads(rules)["prerender"][0]["where"]["and"]


# ------------------------------------------------------------------ budgets


def test_the_figures_page_fits_30_kb_even_with_a_3d_chart_loaded(client, settings):
    # The real data. Base page + geyser.js, plus what the first chart loads when it scrolls into view.
    settings.GEYSER_FIGURES_DIR = settings.REPO_ROOT / "docs" / "figures" / "csv"
    figures._load.cache_clear()
    page = client.get("/figures/").content
    data = client.get("/figures/coverage.json").content
    figures._load.cache_clear()
    with open(os.path.join(STATIC, "geyser.js"), "rb") as fh:
        base_js = fh.read()
    renderer = b""
    for name in ("surface.js", "surface-gl.js"):
        with open(os.path.join(STATIC, name), "rb") as fh:
            renderer += fh.read()
    base = len(gzip.compress(page)) + len(gzip.compress(base_js))
    with_3d = base + len(gzip.compress(renderer)) + len(gzip.compress(data))
    assert base <= 20 * 1024, base
    assert with_3d <= PAGE_BUDGET_BYTES, with_3d


def test_the_renderer_stays_small():
    with open(os.path.join(STATIC, "surface.js"), "rb") as fh:
        main = fh.read()
    with open(os.path.join(STATIC, "surface-gl.js"), "rb") as fh:
        webgl = fh.read()
    both = main + webgl
    assert len(both) < 20 * 1024 and len(gzip.compress(main)) + len(gzip.compress(webgl)) < 8 * 1024  # vs ~1 MB Plotly
    text = main.decode()
    assert "textContent" in text and "innerHTML" not in both.decode()  # labels from CSV headers are never HTML
    assert "Math.min(2, window.devicePixelRatio" in text  # DPR capped at 2
    assert "transferControlToOffscreen" in text and "WorkerGlobalScope" in webgl.decode()  # WebGL off the main thread
