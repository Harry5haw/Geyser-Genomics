#!/usr/bin/env python3
"""Break-even retention for the data lake's recomputable outputs (#62; plan: "lifecycle rule by break-even").

Keeping an object is renting; recomputing it when it's needed again is buying. The deterministic
ski-rental rule keeps it until the storage paid so far equals what recomputing it would cost, then lets it
expire. Whatever the future access pattern, that never costs more than twice the best choice made with
hindsight:

    keep_days = recompute_cost_usd / (size_GiB x storage_price_per_GiB_day)

The ratio doesn't depend on the sample's size: a whole genome's CRAM and its alignment both grow ~50x
over chr20, so the break-even stays the same.

Inputs, per run, from a results directory (as T3's real-sample runs are stored):
  <sample>.stages.csv               query_pipeline_duration.py --stages-csv: one row per Batch job, with
                                    EstCostUSD (on-demand Fargate list price)
  bucket/runs/<sample>.provenance.json   the run record: each output's key and size in bytes

  python scripts/lifecycle_break_even.py --results ../geyser-data/real-inputs/results

The recompute cost of an alignment is its align job, with the raw reads and the reference still stored
(neither ever expires). The region parts under intermediate/ are left out: nothing reads them after the
merge (the merged VCF holds the same records, and a rerun writes its own parts), so they have no reuse
value to break even against, and storage_lifecycle.tf keeps them only as a short debugging window.
"""

import argparse
import csv
import glob
import json
import os
import statistics
import sys

# S3 Standard, eu-west-2, first 50 TB per month (AWS price list, published 2026-10-15). AWS bills
# GB-months in binary gigabytes.
S3_STANDARD_GB_MONTH_USD = 0.024
GIB = 2**30
DAYS_PER_MONTH = 365 / 12
# The job queue prefers Fargate Spot (about 70% below on-demand). The break-even uses on-demand, the
# conservative case (a recompute may fall back to on-demand, and costs wall time too); Spot is shown.
SPOT_FACTOR = 0.3
ALIGN_STAGE = "Align_Genome"
ALIGNMENT_PREFIX = "alignments/"  # app/tasks.py: alignments/{id}.cram (+ .crai), or .bam before #99


def price_per_gib_day(gb_month_usd=S3_STANDARD_GB_MONTH_USD):
    return gb_month_usd / DAYS_PER_MONTH


def break_even_days(recompute_usd, size_bytes, gb_month_usd=S3_STANDARD_GB_MONTH_USD):
    """Days of storage that cost as much as recomputing the object once."""
    if not size_bytes:
        raise ValueError("size must be positive")
    return recompute_usd / (size_bytes / GIB * price_per_gib_day(gb_month_usd))


def alignment_inputs(stage_rows, record):
    """(align job cost in USD, bytes stored under alignments/) for one run."""
    execution = record.get("run_name")
    rows = [r for r in stage_rows if r["Stage"] == ALIGN_STAGE and (not execution or r["ExecutionName"] == execution)]
    if not rows:
        raise ValueError(f"no {ALIGN_STAGE} row for run {execution!r}")
    cost = sum(float(r["EstCostUSD"]) for r in rows)
    size = sum(o.get("bytes") or 0 for o in record.get("outputs", []) if o["key"].startswith(ALIGNMENT_PREFIX))
    return cost, size


def runs(results_dir):
    """[(sample, align cost, alignment bytes)] for every <sample>.stages.csv with a run record."""
    found = []
    for path in sorted(glob.glob(os.path.join(results_dir, "*.stages.csv"))):
        sample = os.path.basename(path)[: -len(".stages.csv")]
        record_path = os.path.join(results_dir, "bucket", "runs", f"{sample}.provenance.json")
        if not os.path.exists(record_path):
            continue
        with open(path, encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        with open(record_path, encoding="utf-8") as fh:
            record = json.load(fh)
        found.append((sample, *alignment_inputs(rows, record)))
    return found


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--results", required=True, help="directory of <sample>.stages.csv + bucket/runs/ records")
    p.add_argument("--gb-month", type=float, default=S3_STANDARD_GB_MONTH_USD, help="S3 USD per GB-month")
    a = p.parse_args(argv)
    found = runs(a.results)
    if not found:
        print(f"No <sample>.stages.csv with a run record under {a.results}")
        return 1
    print(f"S3 Standard ${a.gb_month}/GB-month = ${price_per_gib_day(a.gb_month):.6f}/GiB-day\n")
    print(f"{'run':28} {'CRAM GiB':>8} {'align $':>8} {'keep days':>9} {'(Spot)':>7}")
    days = []
    for sample, cost, size in found:
        d = break_even_days(cost, size, a.gb_month)
        days.append(d)
        print(f"{sample:28} {size / GIB:8.3f} {cost:8.4f} {d:9.0f} {d * SPOT_FACTOR:7.0f}")
    print(
        f"\nalignments/ break-even: {min(days):.0f}-{max(days):.0f} days (median {statistics.median(days):.0f}); "
        f"on Spot {min(days) * SPOT_FACTOR:.0f}-{max(days) * SPOT_FACTOR:.0f}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
