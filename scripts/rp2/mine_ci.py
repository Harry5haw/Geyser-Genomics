#!/usr/bin/env python3
"""RP-2: CI runs, and for each failed run the jobs that failed (read-only: `gh run list` / `gh run view`).

    python scripts/rp2/mine_ci.py --out <folder outside every repo> [--limit 1000] [--no-jobs]
    # writes ci_runs.csv, and ci_failed_jobs.csv unless --no-jobs

A failed job on a PR branch is a candidate "caught by CI" event for the defect log (RQ1): which check (unit, E2E,
Step Functions Local, lint, gate) went red, on which branch and commit.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rp2_common as c  # noqa: E402

RUN_FIELDS = "databaseId,workflowName,event,headBranch,headSha,status,conclusion,createdAt,updatedAt,attempt"
RUN_COLUMNS = [
    "run",
    "workflow",
    "event",
    "branch",
    "sha",
    "status",
    "conclusion",
    "created",
    "updated",
    "minutes",
    "attempt",
]
JOB_COLUMNS = ["run", "workflow", "branch", "sha", "job", "conclusion", "started", "completed", "failed_steps"]


def run_row(r: dict) -> dict:
    hours = c.hours_between(r.get("createdAt"), r.get("updatedAt"))
    return {
        "run": r["databaseId"],
        "workflow": r.get("workflowName"),
        "event": r.get("event"),
        "branch": r.get("headBranch"),
        "sha": r.get("headSha"),
        "status": r.get("status"),
        "conclusion": r.get("conclusion"),
        "created": r.get("createdAt"),
        "updated": r.get("updatedAt"),
        "minutes": None if hours is None else round(hours * 60, 1),
        "attempt": r.get("attempt"),
    }


def failed_jobs(run: dict) -> list:
    view = c.gh_json(["run", "view", str(run["run"]), "--json", "jobs"])
    rows = []
    for job in (view or {}).get("jobs") or []:
        if job.get("conclusion") not in ("failure", "timed_out", "cancelled"):
            continue
        steps = [s["name"] for s in job.get("steps") or [] if s.get("conclusion") == "failure"]
        rows.append(
            {
                "run": run["run"],
                "workflow": run["workflow"],
                "branch": run["branch"],
                "sha": run["sha"],
                "job": job.get("name"),
                "conclusion": job.get("conclusion"),
                "started": job.get("startedAt"),
                "completed": job.get("completedAt"),
                "failed_steps": steps,
            }
        )
    return rows


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", required=True, help="output folder, outside every git repository")
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--no-jobs", action="store_true", help="skip the per-failed-run job lookups (one call each)")
    a = p.parse_args(argv)
    out = c.out_dir(a.out)
    runs = [run_row(r) for r in c.gh_json(["run", "list", "--limit", str(a.limit), "--json", RUN_FIELDS]) or []]
    print(f"ci_runs.csv: {c.write_csv(os.path.join(out, 'ci_runs.csv'), runs, RUN_COLUMNS)} runs")
    if not a.no_jobs:
        jobs = [j for r in runs if r["conclusion"] == "failure" for j in failed_jobs(r)]
        print(f"ci_failed_jobs.csv: {c.write_csv(os.path.join(out, 'ci_failed_jobs.csv'), jobs, JOB_COLUMNS)} jobs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
