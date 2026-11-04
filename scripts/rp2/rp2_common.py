"""Shared helpers for the RP-2 extraction scripts (an empirical study of AI-agent-directed development on this project).

Every script here is read-only on its sources (git, the GitHub API through `gh`, the dissertation notes) and writes
CSVs only to an output folder **outside every git repository** (out_dir refuses anything else), so mined data can't
be committed by accident.
"""

import csv
import json
import os
import re
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENT_TRAILER = re.compile(r"Co-Authored-By:\s*Claude", re.I)
AGENT_PR_FOOTER = "Generated with [Claude Code]"
ISSUE_REF = re.compile(r"#(\d+)")
PLAN_COUNTS = re.compile(r"Plan:\s*(\d+) to add,\s*(\d+) to change,\s*(\d+) to destroy")

# Path prefix -> area, first match wins. Infrastructure (and the deploy workflow) is what needs a plan and an apply.
AREAS = (
    ("infrastructure/", "infra"),
    (".github/workflows/deploy-platform.yml", "infra"),
    ("app/", "app"),
    ("app-deepvariant/", "app"),
    ("app-giraffe/", "app"),
    ("lambda/", "app"),
    ("gate/", "gate"),
    ("web/", "web"),
    ("tests/", "tests"),
    ("scripts/", "scripts"),
    (".github/", "ci"),
    ("docs/", "docs"),
)


def area_of(path: str) -> str:
    """The area a repository path belongs to (AREAS); top-level Markdown is docs, anything else 'other'."""
    for prefix, area in AREAS:
        if path.startswith(prefix):
            return area
    if path.endswith(".md"):
        return "docs"
    return "other"


def run(cmd, cwd=REPO_ROOT) -> str:
    """A command's stdout (it must succeed). The one place scripts touch git and gh, so tests can fake it."""
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise SystemExit(f"{' '.join(cmd[:3])} ... failed (exit {result.returncode}): {result.stderr.strip()[:500]}")
    return result.stdout


def gh_json(args, cwd=REPO_ROOT):
    return json.loads(run(["gh", *args], cwd=cwd) or "null")


def inside_git_repo(path: str) -> bool:
    path = os.path.abspath(path)
    while True:
        if os.path.exists(os.path.join(path, ".git")):
            return True
        parent = os.path.dirname(path)
        if parent == path:
            return False
        path = parent


def out_dir(path: str) -> str:
    """The output folder, created if needed. Refuses one inside a git repository (outputs are never committed)."""
    if inside_git_repo(path):
        raise SystemExit(f"refusing to write inside a git repository: {path} (choose a folder outside every repo)")
    os.makedirs(path, exist_ok=True)
    return path


def write_csv(path: str, rows: list, columns: list) -> int:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _cell(row.get(k)) for k in columns})
    return len(rows)


def _cell(value):
    if isinstance(value, (list, tuple, set)):
        return ";".join(str(v) for v in value)
    if isinstance(value, bool):
        return int(value)
    return "" if value is None else value


def hours_between(start, end):
    """Hours between two ISO-8601 timestamps (GitHub's ...Z form), or None if either is missing."""
    from datetime import datetime

    if not start or not end:
        return None
    a = datetime.fromisoformat(start.replace("Z", "+00:00"))
    b = datetime.fromisoformat(end.replace("Z", "+00:00"))
    return round((b - a).total_seconds() / 3600, 3)
