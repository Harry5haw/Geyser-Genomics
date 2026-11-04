"""scripts/benchmark_dv_grid.py: the grid plan, the parsers of what the DeepVariant stage leaves behind, and the
runner's resume/stop/failure handling with run_local.py and the host checks faked (no Docker, no GPU)."""

import gzip
import json
import os
import subprocess

import pytest

import benchmark_dv_grid as grid

# What deepvariant_task.py prints after a run (its own format strings).
STAGE_OUTPUT = """
DeepVariant step timings for HG003_chr20 (shards=16, gpu=NVIDIA GeForce RTX 3080):
  make_examples             388.0s
  call_variants             132.5s
  postprocess_variants       19.0s
  examples sent to call_variants: 231085; called by the small model in make_examples (CPU): 152000
"""
STATS = "# SN\nSN\t0\tnumber of samples:\t1\nSN\t0\tnumber of records:\t80\nSN\t0\tnumber of SNPs:\t70\nSN\t0\tnumber of indels:\t10\n"


def test_cells_run_shards_outer_then_the_repeats():
    cells = grid.plan_cells([256, 1024], [16, 4], [(256, 16), (256, 16)])
    assert [grid.cell_name(*c) for c in cells] == [
        "b256-s16",
        "b1024-s16",
        "b256-s4",
        "b1024-s4",
        "b256-s16-r2",
        "b256-s16-r3",
    ]


def test_cell_settings_reach_deepvariant_through_run_local():
    env = grid.cell_env(512, 8, 1, 3600)
    assert env["DEEPVARIANT_SHARDS"] == "8"
    assert env["DEEPVARIANT_EXTRA_ARGS"] == "--call_variants_extra_args=batch_size=512"
    assert env["DEEPVARIANT_CHECKPOINT"] == "0"  # a cell never resumes another cell's make_examples
    assert set(env) <= set(grid_run_local_env())


def grid_run_local_env():
    import run_local

    return run_local.DEEPVARIANT_ENV_VARS


@pytest.mark.parametrize("text", ["8", "128,256", " 4 , 16 "])
def test_int_lists(text):
    assert grid.int_list(text) == [int(v) for v in text.split(",")]


@pytest.mark.parametrize("text", ["", "0", "4,-1"])
def test_bad_int_lists(text):
    with pytest.raises(Exception, match="positive integers"):
        grid.int_list(text)


def test_log_gives_step_times_and_example_counts():
    parsed = grid.parse_log(STAGE_OUTPUT)
    assert (parsed["make_examples_s"], parsed["call_variants_s"], parsed["postprocess_variants_s"]) == (
        388.0,
        132.5,
        19.0,
    )
    assert (parsed["examples"], parsed["small_model_calls"]) == (231085, 152000)
    assert grid.parse_log("nothing here")["call_variants_s"] is None


def test_part_gives_status_time_and_gpu_figures():
    part = {
        "status": "Success",
        "duration_seconds": 570.2,
        "resources": {"cpu_seconds": 5100.0},
        "gpu": {
            "model": "RTX",
            "utilization_mean_pct": 8.1,
            "utilization_peak_pct": 100,
            "memory_peak_mib": 9565,
            "steps": {"call_variants": {"utilization_mean_pct": 29.1, "memory_peak_mib": 9565}},
        },
    }
    parsed = grid.parse_part(part)
    assert parsed["stage_s"] == 570.2 and parsed["call_variants_gpu_util_mean_pct"] == 29.1
    assert parsed["cpu_seconds"] == 5100.0
    assert grid.parse_part({})["gpu_model"] is None  # no GPU (CPU image): empty, not an error


def test_stats_give_pass_counts():
    assert grid.parse_stats(STATS) == {"pass_snps": 70, "pass_indels": 10, "pass_records": 80}


def test_records_digest_ignores_the_header(tmp_path):
    a, b = tmp_path / "a.vcf.gz", tmp_path / "b.vcf.gz"
    with gzip.open(a, "wt") as fh:
        fh.write("##date=1\n#CHROM\nchr20\t1\n")
    with gzip.open(b, "wt") as fh:
        fh.write("##date=2\n#CHROM\nchr20\t1\n")
    assert grid.records_md5(str(a)) == grid.records_md5(str(b))


def test_light_containers_may_run_alongside(monkeypatch):
    out = "geyser-sfn-local-1\tamazon/aws-stepfunctions-local\nquizzical\tgeyser-deepvariant:dv-grid\n"
    monkeypatch.setattr(grid.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out))
    assert grid.other_containers() == ["quizzical geyser-deepvariant:dv-grid"]


# ---- the runner, with run_local.py faked ----


@pytest.fixture
def fake_stage(tmp_path, monkeypatch):
    """run_local.py replaced by a fake that writes the stage's outputs (or fails for batch 2048)."""
    root, out = tmp_path / "bucket", tmp_path / "out"
    calls = []

    def fake_run(cmd, env=None, stdout=None, **kwargs):
        calls.append(env["DEEPVARIANT_EXTRA_ARGS"])
        files = grid.outputs(str(root), "S1")
        for path in files.values():
            os.makedirs(os.path.dirname(path), exist_ok=True)
        failed = env["DEEPVARIANT_EXTRA_ARGS"].endswith("=2048")
        stdout.write(STAGE_OUTPUT if not failed else "ResourceExhaustedError\n")
        part = {"status": "Failure" if failed else "Success", "duration_seconds": 500.0, "error": None}
        with open(files["part"], "w") as fh:
            json.dump(part, fh)
        if not failed:
            with open(files["stats"], "w") as fh:
                fh.write(STATS)
            with gzip.open(files["vcf"], "wt") as fh:
                fh.write("#h\nchr20\t1\n")
        return subprocess.CompletedProcess(cmd, 1 if failed else 0)

    monkeypatch.setattr(grid.subprocess, "run", fake_run)
    monkeypatch.setattr(grid, "blocked", lambda *a: None)
    args = ["--root", str(root), "--sample", "S1", "--reference", "ref.fa", "--out", str(out)]
    return args, out, calls


def test_grid_runs_every_cell_and_records_a_failed_one(fake_stage):
    args, out, calls = fake_stage
    assert grid.main(args + ["--batch-sizes", "256,2048", "--shards", "4"]) == 0
    rows = {r["cell"]: r for r in grid.write_table(str(out), grid.plan_cells([256, 2048], [4]))}
    assert rows["b256-s4"]["status"] == "Success" and rows["b256-s4"]["call_variants_s"] == 132.5
    assert rows["b256-s4"]["pass_snps"] == 70 and rows["b256-s4"]["records_md5"]
    failed = rows["b2048-s4"]
    assert failed["status"] == "Failure" and failed["pass_snps"] is None and not failed.get("records_md5")
    assert "exited with code 1" in failed["error"]
    assert (out / "dv-grid.csv").read_text().count("\n") == 3


def test_a_failed_cell_never_reports_the_previous_cells_outputs(fake_stage, monkeypatch):
    # The failing fake writes no stats or VCF: the cell must not pick up b256's, left from the run before.
    args, out, _ = fake_stage
    grid.main(args + ["--batch-sizes", "256,2048", "--shards", "4"])
    assert json.loads((out / "b2048-s4" / "cell.json").read_text())["pass_records"] is None
    assert not (out / "b2048-s4" / "bcftools.stats.deepvariant.txt").exists()


def test_finished_cells_are_skipped_on_rerun(fake_stage):
    args, out, calls = fake_stage
    grid.main(args + ["--batch-sizes", "256", "--shards", "4"])
    grid.main(args + ["--batch-sizes", "256,512", "--shards", "4"])
    assert calls == ["--call_variants_extra_args=batch_size=256", "--call_variants_extra_args=batch_size=512"]


def test_a_busy_host_stops_the_grid_before_the_next_cell(fake_stage, monkeypatch, capsys):
    args, out, calls = fake_stage
    monkeypatch.setattr(grid, "blocked", lambda *a: "only 5.0 GB free (need 8 GB)")
    assert grid.main(args + ["--batch-sizes", "256", "--shards", "4"]) == 3
    assert calls == [] and "Rerun to resume" in capsys.readouterr().out
