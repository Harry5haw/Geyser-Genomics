#!/usr/bin/env python3
"""RP-2: the cloud runs in the dissertation's data/cloud_runs.csv, one row per execution (read-only).

    python scripts/rp2/mine_cloud_runs.py --runs <geyser-dissertation>/data/cloud_runs.csv --out <folder>
    # writes cloud_runs_by_execution.csv

cloud_runs.csv has one row per stage of each run. This folds them into one row per execution: date, result,
sample, stages, total stage seconds, the issues and PRs its notes cite (a failed run that revealed a defect
cites it, e.g. CLOUD001's #71), and a first-pass purpose (check run / real sample / grid / other) for the coders.
"""

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rp2_common as c  # noqa: E402

COLUMNS = ["execution", "date", "result", "sample", "stages", "stage_seconds", "purpose", "refs", "notes"]


def purpose(sample: str) -> str:
    s = (sample or "").upper()
    if s.startswith("CLOUD"):
        return "check run"
    if s.startswith("REAL-"):
        return "real sample"
    if "GRID" in s:
        return "grid"
    return "other"


def fold(rows) -> list:
    runs, order = {}, []
    for r in rows:
        ex = r.get("execution")
        if not ex:
            continue
        if ex not in runs:
            order.append(ex)
            runs[ex] = {
                "execution": ex,
                "date": r.get("date"),
                "result": r.get("result"),
                "sample": r.get("sample"),
                "stages": [],
                "stage_seconds": 0.0,
                "notes": [],
            }
        run = runs[ex]
        if r.get("stage"):
            run["stages"].append(r["stage"])
        try:
            run["stage_seconds"] += float(r.get("seconds") or 0)
        except ValueError:
            pass
        if r.get("notes") and r["notes"] not in run["notes"]:
            run["notes"].append(r["notes"])
    out = []
    for ex in order:
        run = runs[ex]
        notes = " | ".join(run["notes"])
        run.update(
            notes=notes,
            stage_seconds=round(run["stage_seconds"], 1),
            purpose=purpose(run["sample"]),
            refs=sorted({int(n) for n in c.ISSUE_REF.findall(notes)}),
        )
        out.append(run)
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--runs", required=True, help="the dissertation repo's data/cloud_runs.csv (read only)")
    p.add_argument("--out", required=True, help="output folder, outside every git repository")
    a = p.parse_args(argv)
    out = c.out_dir(a.out)
    with open(a.runs, newline="", encoding="utf-8") as fh:
        rows = fold(csv.DictReader(fh))
    n = c.write_csv(os.path.join(out, "cloud_runs_by_execution.csv"), rows, COLUMNS)
    print(f"cloud_runs_by_execution.csv: {n} executions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
