#!/usr/bin/env python3
"""RP-2: every commit on a ref (default origin/main) as one CSV row (read-only: `git log --numstat`).

    python scripts/rp2/mine_commits.py --out <folder outside every repo> [--ref origin/main]   # writes commits.csv

Per commit: author and commit times, whether it is a merge, a revert, or agent co-authored (the Claude trailer),
the PR a merge commit merged, files and lines added/deleted per area (rp2_common.AREAS). No e-mail addresses.
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rp2_common as c  # noqa: E402

AREA_NAMES = sorted({area for _, area in c.AREAS} | {"docs", "other"})
COLUMNS = (
    ["sha", "author", "authored", "committed", "subject", "merge", "revert", "agent", "pr", "files", "added", "deleted"]
    + [f"added_{a}" for a in AREA_NAMES]
    + [f"deleted_{a}" for a in AREA_NAMES]
)
START = "\x1e"  # one record per commit
SEP = "\x1f"
FORMAT = START + SEP.join(["%H", "%P", "%an", "%aI", "%cI", "%s", "%b"]) + SEP
MERGED_PR = re.compile(r"^Merge pull request #(\d+)")


def renamed_path(path: str) -> str:
    """A numstat path's new name: git writes renames as 'a/{old => new}/b' or 'old => new'."""
    if "{" in path and " => " in path:
        prefix, rest = path.split("{", 1)
        inner, suffix = rest.split("}", 1)
        return (prefix + inner.split(" => ", 1)[1] + suffix).replace("//", "/")
    return path.split(" => ", 1)[1] if " => " in path else path


def parse_log(text: str) -> list:
    rows = []
    for record in text.split(START)[1:]:
        fields = record.split(SEP)
        sha, parents, author, authored, committed, subject, body = fields[:7]
        numstat = fields[7] if len(fields) > 7 else ""
        row = {
            "sha": sha,
            "author": author,
            "authored": authored,
            "committed": committed,
            "subject": subject,
            "merge": len(parents.split()) > 1,
            "revert": subject.startswith("Revert "),
            "agent": bool(c.AGENT_TRAILER.search(body)),
            "pr": (MERGED_PR.match(subject) or [None, None])[1],
            "files": 0,
            "added": 0,
            "deleted": 0,
        }
        for area in AREA_NAMES:
            row[f"added_{area}"] = row[f"deleted_{area}"] = 0
        for line in numstat.strip().splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            added, deleted, path = parts
            area = c.area_of(renamed_path(path))  # a rename counts under its new path
            a = int(added) if added.isdigit() else 0  # '-' for binary files
            d = int(deleted) if deleted.isdigit() else 0
            row["files"] += 1
            row["added"] += a
            row["deleted"] += d
            row[f"added_{area}"] += a
            row[f"deleted_{area}"] += d
        rows.append(row)
    return rows


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", required=True, help="output folder, outside every git repository")
    p.add_argument("--ref", default="origin/main")
    a = p.parse_args(argv)
    out = c.out_dir(a.out)
    text = c.run(["git", "log", a.ref, "--numstat", f"--format={FORMAT}"])
    n = c.write_csv(os.path.join(out, "commits.csv"), parse_log(text), COLUMNS)
    print(f"commits.csv: {n} commits on {a.ref}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
