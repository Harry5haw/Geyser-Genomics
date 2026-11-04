#!/usr/bin/env python3
"""RP-2: every issue as one CSV row (read-only: `gh issue list`).

    python scripts/rp2/mine_issues.py --out <folder outside every repo>      # writes issues.csv

Per issue: state and how it closed, open time in hours, labels (bug / decision / security / cost ... feed the
defect log and the decision map), milestone, and the PRs and issues its body refers to.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rp2_common as c  # noqa: E402

FIELDS = "number,title,state,stateReason,createdAt,closedAt,labels,milestone,body"
COLUMNS = ["number", "title", "state", "state_reason", "created", "closed", "open_hours", "labels", "milestone", "refs"]


def issue_row(issue: dict) -> dict:
    return {
        "number": issue["number"],
        "title": issue.get("title"),
        "state": issue.get("state"),
        "state_reason": issue.get("stateReason"),
        "created": issue.get("createdAt"),
        "closed": issue.get("closedAt"),
        "open_hours": c.hours_between(issue.get("createdAt"), issue.get("closedAt")),
        "labels": [label["name"] for label in issue.get("labels") or []],
        "milestone": (issue.get("milestone") or {}).get("title"),
        "refs": sorted({int(n) for n in c.ISSUE_REF.findall(issue.get("body") or "")} - {issue["number"]}),
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", required=True, help="output folder, outside every git repository")
    p.add_argument("--limit", type=int, default=2000)
    a = p.parse_args(argv)
    out = c.out_dir(a.out)
    issues = c.gh_json(["issue", "list", "--state", "all", "--limit", str(a.limit), "--json", FIELDS]) or []
    n = c.write_csv(os.path.join(out, "issues.csv"), [issue_row(i) for i in issues], COLUMNS)
    print(f"issues.csv: {n} issues")
    return 0


if __name__ == "__main__":
    sys.exit(main())
