"""Signature figures (scripts/figures/): the data helpers on tiny synthetic fixtures, and that every
figure renders to PNG + SVG deterministically. Fixtures in tests/fixtures/figures are made up (no reads,
no real calls); the grid's costs and times are invented for the test."""

import os

import pytest

import cost_surface
import coverage_terrain
import quality_surface
import variant_density

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "figures")


def fx(name):
    return os.path.join(FIX, name)


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
    else:
        cost_surface.main([fx("grid.stages.csv"), "--out", prefix])


@pytest.mark.parametrize("kind", ["coverage", "quality", "density", "cost"])
def test_figures_render_deterministically(kind, tmp_path):
    a, b = str(tmp_path / "a"), str(tmp_path / "b")
    _render(kind, a, tmp_path)
    _render(kind, b, tmp_path)
    for ext in ("png", "svg"):
        with open(f"{a}.{ext}", "rb") as fa, open(f"{b}.{ext}", "rb") as fb:
            assert fa.read() == fb.read(), f"{kind}.{ext} differs between two renders"
