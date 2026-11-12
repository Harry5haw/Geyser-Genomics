"""Cohort table from the pipeline's run records, in plain PySpark (so the same code runs on AWS Glue).

Every run leaves runs/{id}.provenance.json (app/provenance.py): inputs, reference, tool versions, per-stage
timings and resources, and the stats/{id}/ summaries (flagstat, mosdepth, bcftools stats). This job reads all of
them and writes one row per sample: what ran, how long each stage took, what it cost in CPU, and the QC numbers
the quality gate looks at. Parquet for querying (Athena, Spark, pandas), plus an optional CSV.

    python scripts/cohort_runs.py --runs local_bucket/runs --out cohort/runs
    python scripts/cohort_runs.py --runs s3a://<bucket>/runs --out s3a://<bucket>/cohort/runs   # needs hadoop-aws

On Glue, scripts/glue/cohort_runs_job.py calls build_cohort() with the job's arguments (docs/cohort-qc.md).

Only runs/*.provenance.json is read: runs/{id}.provenance/ holds the per-stage parts (the record is assembled from
them) and runs/{id}.json is the local web worker's state file, neither is a record.
"""

from __future__ import annotations

import argparse
import sys

# The stages a record can carry (app/provenance.py, one part each). A stage a cohort never ran is simply a null
# column, so a table from bcftools-only runs has the same columns as one with DeepVariant.
STAGES = (
    "Preflight",
    "QualityControl",
    "Align",
    "CallVariants",
    "DeepVariant",
    "PangenomeDeepVariant",
    "QualityGate",
    "Annotate",
)

# (output column, path in the record). Paths into the stats block are the G4 summaries the gate reads.
FIELDS = (
    ("sample_id", "sample_id"),
    ("run_name", "run_name"),
    ("status", "status"),
    ("complete", "complete"),
    ("layout", "layout"),
    ("aligner", "aligner"),
    ("app_version", "app_version"),
    ("reference_name", "reference.name"),
    ("reference_md5", "reference.md5"),
    ("updated", "updated"),
    ("reads_total", "stats.flagstat.in_total"),
    ("mapped_pct", "stats.flagstat.mapped_pct"),
    ("properly_paired_pct", "stats.flagstat.properly_paired_pct"),
    ("duplicates", "stats.flagstat.duplicates"),
    ("mean_depth", "stats.mosdepth.mean_depth"),
    ("variant_records", "stats.bcftools_stats.records"),
    ("snps", "stats.bcftools_stats.snps"),
    ("indels", "stats.bcftools_stats.indels"),
    ("tstv", "stats.bcftools_stats.tstv"),
)

RECORD_GLOB = "*.provenance.json"


def _has_path(schema, path: str) -> bool:
    """True if the dotted path exists in a (nested) Spark schema. Missing paths become null columns."""
    from pyspark.sql.types import StructType

    node = schema
    for part in path.split("."):
        if not isinstance(node, StructType) or part not in node.fieldNames():
            return False
        node = node[part].dataType
    return True


def _col(df, path: str, name: str, cast: str | None = None):
    from pyspark.sql import functions as F

    column = F.col(path) if _has_path(df.schema, path) else F.lit(None)
    if cast:
        column = column.cast(cast)
    return column.alias(name)


def read_records(spark, runs_dir: str):
    """All run records under runs_dir as a DataFrame (one row per record, schema inferred across them)."""
    path = runs_dir.rstrip("/") + "/" + RECORD_GLOB
    # multiLine: each record is one pretty-printed JSON document, not JSON Lines.
    return spark.read.option("multiLine", True).json(path)


def cohort_frame(records):
    """One flat row per sample: identity, QC stats, and per-stage duration/CPU/memory/status."""
    from pyspark.sql import functions as F

    numeric = {
        "reads_total": "long",
        "duplicates": "long",
        "variant_records": "long",
        "snps": "long",
        "indels": "long",
        "mapped_pct": "double",
        "properly_paired_pct": "double",
        "mean_depth": "double",
        "tstv": "double",
    }
    columns = [_col(records, path, name, numeric.get(name)) for name, path in FIELDS]
    for stage in STAGES:
        base = f"stages.{stage}"
        columns += [
            _col(records, f"{base}.status", f"{stage}_status", "string"),
            _col(records, f"{base}.duration_seconds", f"{stage}_seconds", "double"),
            _col(records, f"{base}.resources.cpu_seconds", f"{stage}_cpu_seconds", "double"),
            _col(records, f"{base}.resources.peak_memory_mib", f"{stage}_peak_memory_mib", "double"),
        ]
    flat = records.select(*columns)

    seconds = [F.coalesce(F.col(f"{s}_seconds"), F.lit(0.0)) for s in STAGES]
    cpu = [F.coalesce(F.col(f"{s}_cpu_seconds"), F.lit(0.0)) for s in STAGES]
    memory = [F.col(f"{s}_peak_memory_mib") for s in STAGES]
    flat = (
        flat.withColumn("stage_seconds_total", sum(seconds[1:], seconds[0]))
        .withColumn("cpu_seconds_total", sum(cpu[1:], cpu[0]))
        # greatest() skips nulls; all-null (no stage reported resources) stays null.
        .withColumn("peak_memory_mib_max", F.greatest(*memory))
        .withColumn("duplicate_pct", F.round(100.0 * F.col("duplicates") / F.col("reads_total"), 2))
    )
    return flat.orderBy("sample_id")


def summary(frame):
    """Cohort-level numbers over the samples: counts by status and the spread of the QC stats."""
    from pyspark.sql import functions as F

    stats = ["mean_depth", "mapped_pct", "duplicate_pct", "tstv", "snps", "indels", "stage_seconds_total"]
    aggregates = [F.count(F.lit(1)).alias("samples")]
    aggregates += [F.sum(F.when(F.col("status") == "succeeded", 1).otherwise(0)).alias("succeeded")]
    for name in stats:
        aggregates += [
            F.round(F.min(name), 2).alias(f"{name}_min"),
            F.round(F.avg(name), 2).alias(f"{name}_mean"),
            F.round(F.max(name), 2).alias(f"{name}_max"),
        ]
    return frame.agg(*aggregates)


def build_cohort(spark, runs_dir: str, out_dir: str, csv: bool = False) -> int:
    """Read the records, write {out}/samples and {out}/summary as Parquet (and CSV if asked). Returns the row count."""
    records = read_records(spark, runs_dir)
    if "sample_id" not in records.columns:
        raise SystemExit(f"no run records (*.provenance.json with a sample_id) under {runs_dir}")
    frame = cohort_frame(records.where("sample_id IS NOT NULL")).cache()
    out = out_dir.rstrip("/")
    frame.write.mode("overwrite").parquet(f"{out}/samples")
    summary(frame).write.mode("overwrite").parquet(f"{out}/summary")
    if csv:
        # One small file: a cohort is tens to thousands of rows.
        frame.coalesce(1).write.mode("overwrite").option("header", True).csv(f"{out}/samples_csv")
    return frame.count()


def spark_session(app_name: str = "geyser-cohort-runs"):
    from pyspark.sql import SparkSession

    return (
        SparkSession.builder.appName(app_name)
        .master("local[*]")
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.ui.enabled", "false")
        .getOrCreate()
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--runs", required=True, help="directory holding runs/*.provenance.json (local or s3a://)")
    parser.add_argument("--out", required=True, help="output directory: samples/ and summary/ Parquet")
    parser.add_argument("--csv", action="store_true", help="also write samples_csv/ (one CSV with a header)")
    args = parser.parse_args(argv)
    spark = spark_session()
    try:
        rows = build_cohort(spark, args.runs, args.out, csv=args.csv)
    finally:
        spark.stop()
    print(f"cohort: {rows} samples -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
