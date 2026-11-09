"""Signature figures (scripts/figures/): the data helpers on tiny synthetic fixtures, and that every
figure renders to PNG + SVG deterministically. Fixtures in tests/fixtures/figures are made up (no reads,
no real calls); the grid's costs and times are invented for the test."""

import os

import pytest

import cost_surface
import coverage_terrain
import dv_batch_shards
import figkit
import quality_surface
import variant_density

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "figures")


def fx(name):
    return os.path.join(FIX, name)


# ---- one heat ramp ----


def test_the_web_figures_use_the_same_heat_ramp():
    """The 3D figures on the web page and these 2D figures colour a value the same way."""
    import ast

    path = os.path.join(os.path.dirname(__file__), "..", "web", "samples", "figures.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    lists = {
        t.id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.List)
        for t in node.targets
        if getattr(t, "id", "").startswith("HEAT_")
    }
    assert lists == {
        "HEAT_TURBO": figkit.HEAT_TURBO,
        "HEAT_TURBO_DARK": figkit.HEAT_TURBO_DARK,
        "HEAT_CIVIDIS": figkit.HEAT_CIVIDIS,
    }
    assert figkit.HEAT == figkit.HEAT_TURBO  # the default ramp, on both sides (web: HEAT = HEAT_TURBO)


def _luminance(h):
    r, g, b = (int(h[i : i + 2], 16) / 255 for i in (1, 3, 5))
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a, b):
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_cividis_lightness_rises_and_text_flips_on_the_dark_end():
    # The high-contrast ramp only: Turbo is a rainbow, dark at both ends by design.
    lum = [_luminance(h) for h in figkit.HEAT_CIVIDIS]
    assert lum == sorted(lum) and len(set(lum)) == len(lum)  # low = dark, high = light, every step lighter
    assert figkit.ink_on(0.0, figkit.HEAT_CIVIDIS) == "#ffffff"
    assert figkit.ink_on(1.0, figkit.HEAT_CIVIDIS) == figkit.INK


def test_turbo_is_googles_turbo_all_of_it_and_trimmed_for_the_dark_stage():
    # The reference: matplotlib's "turbo", Google's lookup table (Mikhailov 2019, Apache 2.0).
    from matplotlib import colormaps
    from matplotlib.colors import to_hex

    turbo = colormaps["turbo"]
    assert figkit.HEAT_TURBO == [to_hex(turbo(k / 16)) for k in range(17)]
    assert figkit.HEAT_TURBO_DARK == [to_hex(turbo(0.09 + 0.82 * k / 14)) for k in range(15)]


@pytest.mark.parametrize(
    "ramp, stage",
    [
        ("HEAT_TURBO", "#f3efe6"),  # light
        ("HEAT_TURBO", "#ffffff"),  # retro
        ("HEAT_TURBO_DARK", "#262626"),  # dark (and the #2d2d2d it was first specified with)
        ("HEAT_TURBO_DARK", "#2d2d2d"),
    ],
)
def test_both_ends_of_turbo_clear_2_to_1_on_their_stage(ramp, stage):
    stops = getattr(figkit, ramp)
    assert _contrast(stops[0], stage) >= 2.0 and _contrast(stops[-1], stage) >= 2.0


def test_cividis_clears_2_to_1_on_the_high_contrast_stage():
    # data-theme="contrast" is the low-vision mode on a black stage.
    assert min(_contrast(c, "#000000") for c in figkit.HEAT_CIVIDIS) >= 2.0


def test_cividis_is_cividis_sampled_from_020_to_1():
    from matplotlib import colormaps
    from matplotlib.colors import to_hex

    cividis, n = colormaps["cividis"], len(figkit.HEAT_CIVIDIS)
    assert figkit.HEAT_CIVIDIS == [to_hex(cividis(0.20 + 0.80 * k / (n - 1))) for k in range(n)]


def test_text_on_turbo_cells_is_whichever_contrasts_more():
    # Dark at both ends (white text), bright in the middle (ink).
    assert figkit.ink_on(0.0) == "#ffffff" and figkit.ink_on(1.0) == "#ffffff"
    assert figkit.ink_on(0.5) == figkit.INK


# ---- cost x turnaround ----


def test_makespan_schedules_on_free_slots():
    assert cost_surface.makespan([], 4) == 0.0
    assert cost_surface.makespan([10, 10, 10], 2) == 20
    assert cost_surface.makespan([10, 10, 10], 4) == 10


def test_runs_read_their_grid_cell_from_their_own_rows():
    rows = cost_surface.read_stage_rows([fx("grid.stages.csv")])
    runs = {r["execution"]: r for r in cost_surface.summarise_runs(rows, cost_surface.read_durations(None))}
    assert "FIXTURE-failed" not in runs  # failed runs are left out
    run = runs["FIXTURE-a2-s2"]
    assert (run["align_vcpus"], run["shards"]) == (2.0, 2)
    assert run["cost_usd"] == pytest.approx(0.019)  # QC + align + plan + 2 shards + merge
    # max(QC 40, align 210) + plan 15 + two 75 s shards side by side + merge 15
    assert run["turnaround_s"] == pytest.approx(315) and not run["measured"]


def test_measured_duration_wins_and_failed_executions_are_dropped():
    rows = cost_surface.read_stage_rows([fx("grid.stages.csv")])
    durations = cost_surface.read_durations(fx("grid.durations.csv"))
    runs = {r["execution"]: r for r in cost_surface.summarise_runs(rows, durations, cost_factor=0.5)}
    assert runs["FIXTURE-a2-s1"]["turnaround_s"] == 999 and runs["FIXTURE-a2-s1"]["measured"]
    assert runs["FIXTURE-a2-s1"]["cost_usd"] == pytest.approx(0.0075)
    # Every job of a2-s2 succeeded, but the execution failed afterwards (e.g. at the gate): left out.
    assert "FIXTURE-a2-s2" not in runs


def test_runs_missing_a_required_stage_are_dropped():
    rows = [r for r in cost_surface.read_stage_rows([fx("grid.stages.csv")]) if r["Stage"] != "Call_Variants"]
    assert cost_surface.summarise_runs(rows) == []


def test_shards_beyond_the_map_concurrency_queue():
    spans = {"Call_Shard": [60, 60, 60]}
    assert cost_surface.estimated_turnaround(spans, 3) == 60
    assert cost_surface.estimated_turnaround(spans, 1) == 180
    # The quality gate runs after the merge, so it is on the critical path too.
    assert cost_surface.estimated_turnaround({**spans, "Quality_Gate": [40]}, 3) == 100


def test_grid_medians_and_pareto_front():
    rows = cost_surface.read_stage_rows([fx("grid.stages.csv")])
    cells = cost_surface.grid(cost_surface.summarise_runs(rows))
    assert set(cells) == {(2.0, 1), (2.0, 2), (4.0, 1), (4.0, 2)}
    # (2, 2) is beaten by (4, 1): cheaper and faster.
    assert cost_surface.pareto(cells) == [(2.0, 1), (4.0, 1), (4.0, 2)]


def test_cost_cli_writes_figures_and_table(tmp_path):
    out = str(tmp_path / "cost")
    table = tmp_path / "cells.csv"
    cost_surface.main([fx("grid.stages.csv"), "--out", out, "--surface", "--table", str(table)])
    for suffix in (".png", ".svg", "-surface.png", "-surface.svg"):
        assert os.path.getsize(out + suffix) > 1000
    assert table.read_text().splitlines()[0] == "align_vcpus,shards,runs,median_cost_usd,median_turnaround_s"


# ---- coverage terrain ----


def test_mosdepth_windows_for_one_contig(tmp_path):
    rows = coverage_terrain.from_mosdepth([f"S1={fx('sample.regions.bed')}"], chrom="chr20")
    assert [r["mean_depth"] for r in rows] == [0, 30, 32, 300, 28, 0]
    series = coverage_terrain.ridges(rows)
    assert series["S1"][0][:2] == [0.25, 0.75]  # window midpoints in Mb
    assert coverage_terrain.clip_level(series, 2.5) == pytest.approx(77.5)  # 2.5 x median of covered windows


# ---- quality surface ----


def test_fastp_curves_paired_and_single():
    rows = quality_surface.from_fastp([f"P={fx('paired.fastp.json')}", f"S={fx('single.fastp.json')}"])
    labels, cycles, grids = quality_surface.matrices(rows)
    assert labels == ["P", "S"] and cycles == 5
    assert grids["R1"][0][0] == 36 and grids["R2"][0][0] == 35
    assert grids["R2"][1] == [None] * 5  # single-end: no R2


# ---- variant density ----


def test_only_passing_or_unfiltered_records_on_the_contig_are_counted():
    # The contig length (2,600,000) comes from the VCF header: the last bin is cut at the contig's end.
    rows = variant_density.from_vcf([f"S|caller={fx('calls.vcf')}"], "chr20", 1_000_000)
    assert [(r["bin_start"], r["bin_end"], r["variants"]) for r in rows] == [
        (0, 1_000_000, 3),
        (1_000_000, 2_000_000, 0),
        (2_000_000, 2_600_000, 1),
    ]
    labels, edges, grid = variant_density.matrix(rows)
    assert labels == ["S|caller"] and edges == [0, 1_000_000, 2_000_000, 2_600_000]
    assert grid[0] == [3.0, 0.0, pytest.approx(1 / 0.6)]


def test_labels_are_grouped_by_sample_whatever_the_input_order():
    rows = variant_density.from_vcf(
        [f"A|x={fx('calls.vcf')}", f"B|x={fx('calls.vcf')}", f"A|y={fx('calls.vcf')}"], "chr20", 1_000_000
    )
    assert variant_density.matrix(rows)[0] == ["A|x", "A|y", "B|x"]


def test_wrong_contig_name_stops_with_a_message():
    with pytest.raises(SystemExit, match="no counted records"):
        variant_density.from_vcf([f"S={fx('calls.vcf')}"], "20", 1_000_000)
    with pytest.raises(SystemExit, match="no windows"):
        coverage_terrain.from_mosdepth([f"S={fx('sample.regions.bed')}"], chrom="20")


# ---- DeepVariant batch x shards ----


def test_dv_cells_take_the_median_of_their_successful_runs():
    rows = {(r["batch_size"], r["shards"]): r for r in dv_batch_shards.summarise(figkit.read_csv(fx("dv-grid.csv")))}
    cell = rows[(256, 16)]
    assert (cell["runs"], cell["succeeded"]) == (2, 2)
    assert cell["stage_s"] == 610.0 and cell["call_variants_s"] == 155.0  # medians of 600/620 and 150/160
    assert cell["examples"] == "1000" and cell["same_calls"] == "yes" and cell["error"] == ""
    # Shards rise in the output order, then batch sizes: the table reads like the figure's rows.
    assert list(rows)[:2] == [(256, 4), (1024, 4)]


def test_a_dv_cell_with_no_successful_run_keeps_its_error_and_draws_nothing():
    rows = {(r["batch_size"], r["shards"]): r for r in dv_batch_shards.summarise(figkit.read_csv(fx("dv-grid.csv")))}
    failed = rows[(2048, 16)]
    assert (failed["succeeded"], failed["stage_s"], failed["same_calls"]) == (0, "", "")
    assert failed["error"].startswith("WrapperStopped") and failed["gpu_memory_peak_mib"] == 9900


def test_dv_runs_with_different_calls_are_flagged():
    rows = figkit.read_csv(fx("dv-grid.csv"))
    rows[0] = dict(rows[0], records_md5="bbb")
    assert {r["same_calls"] for r in dv_batch_shards.summarise(rows) if r["succeeded"]} == {"no"}


# ---- every figure renders, and identically twice ----


def _render(kind, prefix, tmp_path):
    if kind == "coverage":
        csv_path = str(tmp_path / "cov.csv")
        coverage_terrain.main(
            [
                "--from-mosdepth",
                f"A={fx('sample.regions.bed')}",
                f"B={fx('sample.regions.bed')}",
                "--csv",
                csv_path,
                "--out",
                prefix,
            ]
        )
    elif kind == "quality":
        csv_path = str(tmp_path / "q.csv")
        quality_surface.main(
            [
                "--from-fastp",
                f"P={fx('paired.fastp.json')}",
                f"S={fx('single.fastp.json')}",
                "--csv",
                csv_path,
                "--out",
                prefix,
            ]
        )
    elif kind == "density":
        csv_path = str(tmp_path / "v.csv")
        variant_density.main(
            [
                "--from-vcf",
                f"S|a={fx('calls.vcf')}",
                f"S|b={fx('calls.vcf')}",
                f"T|a={fx('calls.vcf')}",
                "--csv",
                csv_path,
                "--out",
                prefix,
            ]
        )
    elif kind == "dv":
        csv_path = str(tmp_path / "dv.csv")
        dv_batch_shards.main(["--from-grid", fx("dv-grid.csv"), "--csv", csv_path, "--out", prefix])
    else:
        cost_surface.main([fx("grid.stages.csv"), "--out", prefix])


@pytest.mark.parametrize("kind", ["coverage", "quality", "density", "cost", "dv"])
def test_figures_render_deterministically(kind, tmp_path):
    a, b = str(tmp_path / "a"), str(tmp_path / "b")
    _render(kind, a, tmp_path)
    _render(kind, b, tmp_path)
    for ext in ("png", "svg"):
        with open(f"{a}.{ext}", "rb") as fa, open(f"{b}.{ext}", "rb") as fb:
            assert fa.read() == fb.read(), f"{kind}.{ext} differs between two renders"


# ---- edge cases (second review) ----


def _grid_rows():
    return cost_surface.read_stage_rows([fx("grid.stages.csv")])


def test_failed_job_off_the_critical_path_keeps_the_run_and_its_cost():
    rows = _grid_rows()
    extra = dict(rows[0], Stage="DeepVariant", Status="FAILED", EstCostUSD="0.005")  # caught, run went on
    runs = {r["execution"]: r for r in cost_surface.summarise_runs([*rows, extra])}
    assert runs[extra["ExecutionName"]]["cost_usd"] == pytest.approx(0.015 + 0.005)


def test_run_without_align_vcpu_is_left_out(capsys):
    rows = [
        dict(r, vCPU="") if r["ExecutionName"] == "FIXTURE-a4-s2" and r["Stage"] == "Align_Genome" else r
        for r in _grid_rows()
    ]
    names = {r["execution"] for r in cost_surface.summarise_runs(rows)}
    assert "FIXTURE-a4-s2" not in names and len(names) == 3
    assert "no vCPU" in capsys.readouterr().err


def test_run_missing_from_durations_is_left_out_with_a_warning(capsys):
    durations = cost_surface.read_durations(fx("grid.durations.csv"))
    del durations["FIXTURE-a4-s1"]
    names = {r["execution"] for r in cost_surface.summarise_runs(_grid_rows(), durations)}
    assert names == {"FIXTURE-a2-s1", "FIXTURE-a4-s2"}
    assert "FIXTURE-a4-s1" in capsys.readouterr().err


def _vcf(tmp_path, name, contig_length, records):
    path = tmp_path / name
    lines = ["##fileformat=VCFv4.2"]
    if contig_length:
        lines.append(f"##contig=<ID=chr20,length={contig_length}>")
    lines.append("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO")
    lines += records
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def test_disagreeing_contig_lengths_need_length(tmp_path):
    rec = ["chr20\t10\t.\tA\tG\t30\tPASS\t."]
    a, b = _vcf(tmp_path, "a.vcf", 2_600_000, rec), _vcf(tmp_path, "b.vcf", 3_000_000, rec)
    with pytest.raises(SystemExit, match="disagree"):
        variant_density.from_vcf([f"A={a}", f"B={b}"], "chr20", 1_000_000)
    rows = variant_density.from_vcf([f"A={a}", f"B={b}"], "chr20", 1_000_000, length=2_600_000)
    assert rows[-1]["bin_end"] == 2_600_000


def test_records_past_the_length_are_reported(tmp_path, capsys):
    path = _vcf(tmp_path, "c.vcf", None, ["chr20\t10\t.\tA\tG\t30\tPASS\t.", "chr20\t2500000\t.\tA\tG\t30\tPASS\t."])
    rows = variant_density.from_vcf([f"S={path}"], "chr20", 1_000_000, length=1_000_000)
    assert [r["variants"] for r in rows] == [1]
    assert "1 record(s) past" in capsys.readouterr().err


def test_malformed_vcf_record_names_file_and_line(tmp_path):
    path = _vcf(tmp_path, "bad.vcf", 2_600_000, ["chr20\t10\t.\tA"])
    with pytest.raises(SystemExit, match=r"bad\.vcf:4: malformed"):
        variant_density.from_vcf([f"S={path}"], "chr20", 1_000_000)


def test_fastp_duplicate_label_or_wrong_file_stops(tmp_path):
    with pytest.raises(SystemExit, match="given twice"):
        quality_surface.from_fastp([f"P={fx('paired.fastp.json')}", f"P={fx('single.fastp.json')}"])
    other = tmp_path / "other.json"
    other.write_text('{"summary": {}}')
    with pytest.raises(SystemExit, match="not a fastp JSON"):
        quality_surface.from_fastp([f"X={other}"])
