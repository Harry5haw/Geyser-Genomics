"""scripts/cohort_runs.py: the cohort table from run records, in plain PySpark (the code a Glue job runs).

Synthetic records shaped like app/provenance.py's (the real ones are in geyser-data, never in tests). Needs pyspark
(requirements-cohort.txt) and a JVM, else it skips: the CI "cohort" job runs it, and app-cohort/Dockerfile locally.
"""

import json
import os

import pytest

import cohort_runs


def record(sample_id, depth, status="succeeded", caller_stage="CallVariants", layout="paired", stats=True):
    stage = {
        "duration_seconds": 10.0,
        "error": None,
        "resources": {"cpu_seconds": 20.0, "peak_memory_mib": 100.0},
        "run_name": f"{sample_id}-20261102T000000Z-abc",
        "status": "Success",
    }
    rec = {
        "schema": 1,
        "sample_id": sample_id,
        "run_name": f"{sample_id}-20261102T000000Z-abc",
        "status": status,
        "complete": status == "succeeded",
        "layout": layout,
        "aligner": "bwa-mem2",
        "app_version": "dev-local",
        "reference": {"name": "chr20.fa", "md5": "cbfd7d9a", "bytes": 1},
        "updated": "2026-11-02T00:10:00Z",
        "stages": {
            "QualityControl": stage,
            "Align": stage
            | {"duration_seconds": 100.0, "resources": {"cpu_seconds": 300.0, "peak_memory_mib": 2000.0}},
            caller_stage: stage,
        },
    }
    if stats:
        rec["stats"] = {
            "flagstat": {"in_total": 1000, "duplicates": 50, "primary_mapped_pct": 99.5, "properly_paired_pct": 98.0},
            "mosdepth": {"mean_depth": depth, "length": 64444167, "bases": 1},
            "bcftools_stats": {"records": 120, "snps": 100, "indels": 20, "tstv": 2.0},
        }
    return rec


@pytest.fixture(scope="module")
def spark(cohort_spark):
    """The shared session (tests/conftest.py): Hail's when Hail is installed, so test order doesn't matter."""
    return cohort_spark[0]


@pytest.fixture
def runs(tmp_path):
    runs = tmp_path / "runs"
    (runs / "A.provenance").mkdir(parents=True)
    records = {
        "A": record("A", 32.0),
        "B": record("B", 14.5, layout="single"),
        # DeepVariant instead of bcftools, and a failed run with no stats yet.
        "C": record("C", 30.0, status="failed", caller_stage="DeepVariant", stats=False)
        | {
            # A run Finalize closed after a shard failed: the failing job's stage is in stages too.
            "failure": {"stage": "CallVariantsShard", "reason": "OutOfMemoryError: Container killed", "exit_code": 137},
        },
    }
    for sample_id, rec in records.items():
        (runs / f"{sample_id}.provenance.json").write_text(json.dumps(rec, indent=2))
    # Not records: a per-stage part, and the local web worker's state file (runs/{id}.json).
    (runs / "A.provenance" / "Align.json").write_text(json.dumps({"task": "Align", "sample_id": "PART"}))
    (runs / "A.json").write_text(json.dumps({"sample_id": "WEB", "state": "done"}))
    return runs


def test_one_row_per_record(spark, runs, tmp_path):
    out = tmp_path / "cohort"
    assert cohort_runs.build_cohort(spark, str(runs), str(out)) == 3
    rows = {r["sample_id"]: r.asDict() for r in spark.read.parquet(str(out / "samples")).collect()}
    assert sorted(rows) == ["A", "B", "C"]  # neither the part nor the web state file

    a = rows["A"]
    assert (a["layout"], a["aligner"], a["reference_md5"], a["mean_depth"]) == ("paired", "bwa-mem2", "cbfd7d9a", 32.0)
    assert (a["Align_seconds"], a["Align_cpu_seconds"], a["CallVariants_status"]) == (100.0, 300.0, "Success")
    assert a["stage_seconds_total"] == 120.0  # 10 + 100 + 10
    assert a["cpu_seconds_total"] == 340.0
    assert a["peak_memory_mib_max"] == 2000.0
    assert a["duplicate_records_pct"] == 5.0
    # A stage the run never had is a null column, not an error.
    assert a["DeepVariant_seconds"] is None

    c = rows["C"]
    assert (c["status"], c["DeepVariant_status"], c["CallVariants_status"]) == ("failed", "Success", None)
    assert c["mean_depth"] is None and c["duplicate_records_pct"] is None
    # Where and why it failed, from the record's failure block.
    assert (c["failed_stage"], c["failure_exit_code"]) == ("CallVariantsShard", 137)
    assert c["failure_reason"].startswith("OutOfMemoryError")
    assert a["failed_stage"] is None
    assert a["primary_mapped_pct"] == 99.5


def test_summary_and_csv(spark, runs, tmp_path):
    out = tmp_path / "cohort"
    cohort_runs.build_cohort(spark, str(runs), str(out), csv=True)
    summary = spark.read.parquet(str(out / "summary")).collect()[0].asDict()
    assert (summary["samples"], summary["succeeded"]) == (2 + 1, 2)
    # Averages skip the failed run's missing depth.
    assert (summary["mean_depth_min"], summary["mean_depth_max"]) == (14.5, 32.0)
    assert summary["mean_depth_mean"] == round((32.0 + 14.5) / 2, 2)
    csv_files = [f for f in os.listdir(out / "samples_csv") if f.endswith(".csv")]
    assert len(csv_files) == 1
    header = (out / "samples_csv" / csv_files[0]).read_text().splitlines()[0]
    assert header.startswith("sample_id,run_name,status")


def test_columns_are_stable_without_stats(spark, tmp_path):
    """A cohort where no record has stats yet (all failed early) still gets every column."""
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "X.provenance.json").write_text(json.dumps(record("X", 0, status="failed", stats=False)))
    out = tmp_path / "cohort"
    cohort_runs.build_cohort(spark, str(runs), str(out))
    columns = spark.read.parquet(str(out / "samples")).columns
    for name, _ in cohort_runs.FIELDS:
        assert name in columns
    for stage in cohort_runs.STAGES:
        assert f"{stage}_seconds" in columns


def test_no_records_is_an_error(spark, tmp_path):
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "A.json").write_text("{}")
    with pytest.raises(Exception, match="(?i)no run records|path does not exist|unable to infer"):
        cohort_runs.build_cohort(spark, str(tmp_path / "runs"), str(tmp_path / "out"))


def test_stage_list_covers_the_pipeline():
    """Every stage app/provenance.py can put in a record has columns here."""
    import provenance

    known = set(provenance.CORE_STAGES) | set(provenance.CALLER_STAGES.values()) | set(provenance.UPSTREAM)
    # The jobs a Finalize part can name as the failing one go into stages too.
    known |= {stage for _, stage in provenance.JOB_NAME_STAGES}
    assert known <= set(cohort_runs.STAGES)
