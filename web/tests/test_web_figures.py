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
    assert figures.load("cost") is None  # this folder has no cost-surface.csv


COST = (
    b"align_vcpus,shards,runs,median_cost_usd,median_turnaround_s\n"
    b"1,1,2,0.087747,4786.9\n1,8,2,0.102490,5084.0\n4,1,3,0.085664,2067.8\n4,8,2,0.079422,1474.2\n"
)


def test_the_cost_grid_reads_cost_surface_tables_as_committed(figdir):
    # cost_surface.py --table (#221): rows = align vCPUs, x = shards on a log2 axis, cents and minutes.
    (figdir / "cost-surface.csv").write_bytes(COST)
    cost, turnaround = figures.load("cost"), figures.load("turnaround")
    assert cost.rows == ["1 vCPU", "4 vCPUs"] and cost.x == [0.0, 3.0]
    assert cost.x_ticks == [(0.0, "1"), (3.0, "8")]
    assert cost.values == [[8.775, 10.249], [8.566, 7.942]]  # cents
    assert turnaround.values[1] == [round(2067.8 / 60, 3), round(1474.2 / 60, 3)]  # minutes


def test_a_flat_grid_draws_instead_of_failing(client, figdir):
    # Every value the same whole number: a stated baseline would equal the top (a division by zero before).
    flat = "label,read,cycle,mean,A,C,G,T\n" + "".join(f"S1,R1,{c},36,36,36,36,36\n" for c in range(1, 4))
    (figdir / "quality-surface.csv").write_bytes(flat.encode())
    assert figures.poster_svg(figures.load("quality")).count("<path ") == 2  # one ridge per ramp
    assert client.get("/figures/").status_code == 200


def test_a_short_row_is_skipped_not_a_crash(client, figdir):
    # csv gives None for missing trailing fields: a row without its label is left out.
    short = "chrom,start,end,mean_depth,label\nchr20,0,500000,30,A\nchr20,500000,1000000,31\n"
    (figdir / "coverage-terrain.csv").write_bytes(short.encode())
    assert figures.load("coverage").rows == ["A"]
    assert client.get("/figures/").status_code == 200


def test_an_unreadable_csv_never_shows_a_server_path(client, figdir):
    (figdir / "quality-surface.csv").unlink()
    (figdir / "quality-surface.csv").mkdir()  # exists, but can't be opened as a file
    resp = client.get("/figures/")
    body = resp.content.decode()
    assert resp.status_code == 200 and "quality-surface.csv can" in body
    assert str(figdir) not in body and "Error)" in body  # the error's type only, never its message


def test_the_committed_figure_data_loads(settings):
    # The real CSVs #189 committed: 7 alignments × 129 windows, 10 read files × 151 cycles.
    settings.GEYSER_FIGURES_DIR = settings.REPO_ROOT / "docs" / "figures" / "csv"
    figures._load.cache_clear()
    coverage, quality = figures.load("coverage"), figures.load("quality")
    assert (len(coverage.rows), len(coverage.x)) == (7, 129)
    assert (len(quality.rows), len(quality.x)) == (10, 151)
    # #221's cost grid: 3 align sizes × 4 shard counts, every cell measured, cost and turnaround.
    for name in ("cost", "turnaround"):
        grid = figures.load(name)
        assert grid.rows == ["1 vCPU", "2 vCPUs", "4 vCPUs"] and [t[1] for t in grid.x_ticks] == ["1", "2", "4", "8"]
        assert all(v is not None for line in grid.values for v in line)
    figures._load.cache_clear()


# ------------------------------------------------------------------ poster and JSON


def test_the_poster_is_one_ridge_per_row_with_escaped_labels(figdir):
    (figdir / "coverage-terrain.csv").write_bytes(COVERAGE.replace("B · giraffe", "<b>x</b>").encode())
    svg = figures.poster_svg(figures.load("coverage"))
    assert svg.startswith('<svg class="poster"') and 'role="img"' in svg and "aria-label=" in svg
    assert svg.count("<path ") == 4  # one ridge per row per ramp (turbo, cividis)
    assert "<b>x</b>" not in svg and "&lt;b&gt;x&lt;/b&gt;" in svg


def test_the_poster_fills_each_ridge_by_value(figdir):
    svg = figures.poster_svg(figures.load("coverage"))
    # One heat gradient per ridge, from the ridge's baseline (bottom of the scale) up to the top of the scale.
    # One per ramp: the theme's CSS shows one group (.ramp-turbo, or .ramp-cividis under data-theme=contrast).
    gradients = re.findall(
        r'<linearGradient id="(heat-coverage-\d+-\w+)" gradientUnits="userSpaceOnUse"[^>]*>(.*?)</linear', svg
    )
    assert [g for g, _ in gradients] == [
        "heat-coverage-0-turbo",
        "heat-coverage-0-cividis",
        "heat-coverage-1-turbo",
        "heat-coverage-1-cividis",
    ]
    assert re.findall(r'stop-color="(#[0-9a-f]{6})"', gradients[0][1]) == figures.HEAT_TURBO
    assert re.findall(r'stop-color="(#[0-9a-f]{6})"', gradients[1][1]) == figures.HEAT_CIVIDIS
    for ramp in ("turbo", "cividis"):
        group = re.search(rf'<g class="ramp-{ramp}">(.*?)</g>', svg).group(1)
        assert group.count(f'-{ramp})"') == 2 and "style=" not in svg  # attributes only (CSP)


def test_the_colour_bar_shows_the_scale_with_its_cap(figdir):
    coverage = figures.legend_svg(figures.load("coverage"))
    assert coverage.startswith('<svg class="colorbar"') and 'role="img"' in coverage
    assert re.findall(r'stop-color="(#[0-9a-f]{6})"', coverage) == figures.HEAT_TURBO + figures.HEAT_CIVIDIS
    assert re.findall(r'<rect class="(ramp-\w+)"', coverage) == ["ramp-turbo", "ramp-cividis"]
    grid = figures.load("coverage")
    assert [t for t in re.findall(r">([^<]+)</text>", coverage)] == [
        "0",
        figures._num(grid.clip / 2),
        f"≥ {figures._num(grid.clip)}",
    ]
    quality = figures.legend_svg(figures.load("quality"))  # narrow range: from the stated baseline, no cap
    assert re.findall(r">([^<]+)</text>", quality) == ["32", "34", "36"]
    assert "Mean base quality from 32 to 36 Phred" in quality


def test_ticks_read_like_the_3d_labels():
    assert [figures._num(v) for v in (0, 0.5, 2.25, 12.34, 150.4)] == ["0", "0.5", "2.25", "12.3", "150"]


def test_the_poster_resamples_long_rows(figdir):
    rows = "".join(f"A,chr20,{i * 1000},{(i + 1) * 1000},{30 + i % 7}\n" for i in range(1000))
    (figdir / "coverage-terrain.csv").write_bytes(("label,chrom,start,end,mean_depth\n" + rows).encode())
    svg = figures.poster_svg(figures.load("coverage"))
    points = re.search(r" L([^Z]*) L", svg).group(1).split()
    assert len(points) <= 120  # a small inline picture, whatever the grid size


def test_grid_json_is_what_the_renderer_reads(figdir):
    data = json.loads(figures.grid_json(figures.load("quality")))
    assert set(data) == {"title", "x", "rows", "z", "values", "ramp", "ramps"}
    assert data["ramp"] == figures.HEAT == figures.HEAT_TURBO  # the default, for a page cached before the themes
    # surface.js picks one by data-theme: the same stops as the poster and the colour bar.
    assert data["ramps"] == {"turbo": figures.HEAT_TURBO, "cividis": figures.HEAT_CIVIDIS}
    assert data["x"] == {
        "label": "Sequencing cycle",
        "unit": "cycle",
        "values": [1, 2, 3],
        "ticks": [],
    }  # evenly spaced
    assert data["rows"]["names"] == ["S1 R1", "S1 R2"]
    assert data["z"]["base"] == 32 and data["z"]["unit"] == "Phred" and data["z"]["clip"] is None
    assert data["values"][1] == [35.0, 34.8, 33.5]


# ------------------------------------------------------------------ the page and endpoints


def test_the_page_shows_posters_and_defers_the_3d(client, figdir):
    body = client.get("/figures/").content.decode()
    assert body.count('<svg class="poster"') == 2  # coverage and quality: this folder has only those two CSVs
    assert body.count('<svg class="colorbar"') == 2  # each with its colour bar
    assert len(set(re.findall(r'<linearGradient id="([^"]+)"', body))) == body.count("<linearGradient ")  # ids unique
    assert 'data-surface="/figures/coverage.json"' in body and 'data-surface="/figures/quality.json"' in body
    assert re.search(r'data-surface-js="/static/samples/surface[^"]*\.js"', body)
    assert re.search(r'data-surface-gl="/static/samples/surface-gl[^"]*\.js"', body)  # the WebGL half, for the worker
    assert "<script src=" in body and body.count("<script src=") == 1  # surface.js is not loaded with the page
    assert "No data yet: cost-surface.csv" in body  # the grid's CSV isn't in this test folder
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
    assert client.get("/figures/cost.json").status_code == 404  # no CSV in this folder
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


# ------------------------------------------------------------------ DeepVariant batch × shards (#216)

DV = (
    "batch_size,shards,runs,succeeded,stage_s,make_examples_s,call_variants_s\n"
    "128,4,1,1,807.6,657.2,102.0\n256,4,1,1,849.6,695.4,104.9\n1024,4,1,1,962.9,778.6,131.2\n"
    "128,16,1,1,500.9,350.3,107.2\n256,16,2,2,479.4,333.4,104.8\n1024,16,1,1,493.3,326.8,127.8\n"
)


def test_dv_grids_put_batch_size_on_a_log2_axis_with_named_ticks(figdir):
    (figdir / "dv-batch-shards.csv").write_bytes(DV.encode())
    stage, call = figures.load("dv-stage"), figures.load("dv-call")
    assert stage.rows == ["4 shards", "16 shards"]
    assert stage.x == [7.0, 8.0, 10.0]  # log2(128, 256, 1024): evenly spaced doublings
    assert stage.x_ticks == [(7.0, "128"), (8.0, "256"), (10.0, "1024")]
    assert stage.values[0][0] == round(807.6 / 60, 3)  # minutes
    assert call.values[1] == [107.2, 104.8, 127.8]  # seconds, the GPU step alone
    data = json.loads(figures.grid_json(stage))
    assert data["x"]["ticks"] == [[7.0, "128"], [8.0, "256"], [10.0, "1024"]]


def test_dv_cards_state_the_gpu_nondeterminism(client, figdir):
    (figdir / "dv-batch-shards.csv").write_bytes(DV.encode())
    body = client.get("/figures/").content.decode()
    assert body.count("differ by up to ±2 between cells (GPU nondeterminism") == 2
    assert 'data-surface="/figures/dv-stage.json"' in body and 'data-surface="/figures/dv-call.json"' in body
    assert ">1024</text>" in body  # the poster's ticks are the batch sizes, not log2 values
    assert "2 shard settings × 3 points" in body and "shardss" not in body


def test_a_missing_csv_without_a_pending_message_says_so(client, figdir):
    body = client.get("/figures/").content.decode()
    assert "No data yet: dv-batch-shards.csv" in body


def test_the_committed_dv_grid_loads(settings):
    settings.GEYSER_FIGURES_DIR = settings.REPO_ROOT / "docs" / "figures" / "csv"
    figures._load.cache_clear()
    grid = figures.load("dv-stage")
    figures._load.cache_clear()
    assert grid.rows == ["4 shards", "8 shards", "16 shards"] and [t[1] for t in grid.x_ticks] == [
        "128",
        "256",
        "512",
        "1024",
    ]
    assert all(v is not None for line in grid.values for v in line)  # 12 cells, all succeeded
