"""scripts/lifecycle_break_even.py: the ski-rental retention arithmetic for storage_lifecycle.tf."""

import csv
import json

import pytest

import lifecycle_break_even as lb

GIB = 2**30


def test_break_even_is_recompute_cost_over_daily_storage_cost():
    # $0.024/GB-month = $0.000789/GiB-day: 1 GiB that costs $0.0789 to recompute is worth 100 days.
    assert lb.price_per_gib_day() == pytest.approx(0.024 * 12 / 365)
    assert lb.break_even_days(0.024 * 12 / 365 * 100, GIB) == pytest.approx(100)
    # Size-independent: twice the data and twice the compute keep the same number of days.
    assert lb.break_even_days(0.1, GIB) == pytest.approx(lb.break_even_days(0.2, 2 * GIB))
    with pytest.raises(ValueError):
        lb.break_even_days(0.1, 0)


def _results(tmp_path, sample="S1", run="S1-20261102T000000Z-abc"):
    rows = [
        {"SampleId": sample, "ExecutionName": run, "Stage": "Align_Genome", "EstCostUSD": "0.05"},
        {"SampleId": sample, "ExecutionName": run, "Stage": "Call_Shard", "EstCostUSD": "0.01"},
        # An older run of the same sample in the same CSV: not this record's run, not counted.
        {"SampleId": sample, "ExecutionName": "S1-old", "Stage": "Align_Genome", "EstCostUSD": "9"},
    ]
    with open(tmp_path / f"{sample}.stages.csv", "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (tmp_path / "bucket" / "runs").mkdir(parents=True)
    record = {
        "run_name": run,
        "outputs": [
            {"key": f"alignments/{sample}.cram", "bytes": GIB // 4},
            {"key": f"alignments/{sample}.cram.crai", "bytes": 1024},
            {"key": f"variants/{sample}.vcf.gz", "bytes": 5_000_000},
        ],
    }
    (tmp_path / "bucket" / "runs" / f"{sample}.provenance.json").write_text(json.dumps(record))


def test_a_runs_align_cost_and_alignment_bytes_come_from_its_own_run(tmp_path):
    _results(tmp_path)
    ((sample, cost, size),) = lb.runs(str(tmp_path))
    assert (sample, cost, size) == ("S1", 0.05, GIB // 4 + 1024)


def test_cli_prints_the_break_even_range(tmp_path, capsys):
    _results(tmp_path)
    assert lb.main(["--results", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    days = lb.break_even_days(0.05, GIB // 4 + 1024)
    assert f"alignments/ break-even: {days:.0f}-{days:.0f} days" in out
    assert lb.main(["--results", str(tmp_path / "nothing")]) == 1
