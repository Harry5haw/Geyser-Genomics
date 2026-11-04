#!/usr/bin/env python3
"""RP-2: every pull request as one CSV row (read-only: `gh pr list`).

    python scripts/rp2/mine_prs.py --out <folder outside every repo>      # writes prs.csv

Per PR: state, draft, opened/merged/closed times and the cycle time in hours; size (additions, deletions, files)
and the areas it touched; its class (infra if it touches infrastructure/ or the deploy workflow, else offline);
whether it was agent-written (the Claude Code footer, or a commit with the Claude co-author trailer); the issues
it refers to; the Terraform plan counts written in its body (the expected/posted plans); labels.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rp2_common as c  # noqa: E402

FIELDS = (
    "number,title,state,isDraft,createdAt,mergedAt,closedAt,author,headRefName,baseRefName,additions,deletions,"
    "changedFiles,files,labels,body"
)  # commits come per PR (pr_commits): with them in one list query, GitHub's 500,000-node GraphQL limit is exceeded
COLUMNS = [
    "number",
    "title",
    "state",
    "draft",
    "created",
    "merged",
    "closed",
    "cycle_hours",
    "class",
    "areas",
    "additions",
    "deletions",
    "files",
    "commits",
    "agent_commits",
    "agent_written",
    "issue_refs",
    "plan_counts",
    "labels",
    "head",
    "base",
]


def pr_row(pr: dict) -> dict:
    paths = [f["path"] for f in pr.get("files") or []]
    areas = sorted({c.area_of(p) for p in paths})
    body = pr.get("body") or ""
    commits = pr.get("commits") or []
    agent_commits = sum(
        bool(c.AGENT_TRAILER.search((m.get("messageBody") or "") + (m.get("messageHeadline") or ""))) for m in commits
    )
    end = pr.get("mergedAt") or pr.get("closedAt")
    return {
        "number": pr["number"],
        "title": pr.get("title"),
        "state": pr.get("state"),
        "draft": pr.get("isDraft"),
        "created": pr.get("createdAt"),
        "merged": pr.get("mergedAt"),
        "closed": pr.get("closedAt"),
        "cycle_hours": c.hours_between(pr.get("createdAt"), end),
        "class": "infra" if "infra" in areas else "offline",
        "areas": areas,
        "additions": pr.get("additions"),
        "deletions": pr.get("deletions"),
        "files": len(paths),
        "commits": len(commits),
        "agent_commits": agent_commits,
        "agent_written": c.AGENT_PR_FOOTER in body or agent_commits > 0,
        "issue_refs": sorted({int(n) for n in c.ISSUE_REF.findall(body)} - {pr["number"]}),
        "plan_counts": ["/".join(m) for m in c.PLAN_COUNTS.findall(body)],
        "labels": [label["name"] for label in pr.get("labels") or []],
        "head": pr.get("headRefName"),
        "base": pr.get("baseRefName"),
    }


def pr_commits(number: int) -> list:
    return (c.gh_json(["pr", "view", str(number), "--json", "commits"]) or {}).get("commits") or []


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", required=True, help="output folder, outside every git repository")
    p.add_argument("--limit", type=int, default=2000)
    p.add_argument("--no-commits", action="store_true", help="skip the per-PR commit lookups (one call per PR)")
    a = p.parse_args(argv)
    out = c.out_dir(a.out)
    prs = c.gh_json(["pr", "list", "--state", "all", "--limit", str(a.limit), "--json", FIELDS])
    if not a.no_commits:
        for pr in prs:
            pr["commits"] = pr_commits(pr["number"])
    n = c.write_csv(os.path.join(out, "prs.csv"), [pr_row(pr) for pr in prs], COLUMNS)
    print(f"prs.csv: {n} pull requests")
    return 0


if __name__ == "__main__":
    sys.exit(main())
