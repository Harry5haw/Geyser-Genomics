#!/usr/bin/env python3
"""RP-2: the design decisions in the dissertation's notes/decisions.md as one CSV row each (read-only).

    python scripts/rp2/mine_decisions.py --notes <geyser-dissertation>/notes/decisions.md --out <folder>
    # writes decisions.csv

Per decision: its number, date, title and sources (the index table), and its "Who decided" text, kept verbatim
(who_raw) and mapped to a coarse class for RQ2 (who_class). The mapping is a first pass for the coders, not a
coding: the raw text is the evidence.
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rp2_common as c  # noqa: E402

COLUMNS = ["id", "date", "decision", "sources", "issue_refs", "who_raw", "who_class"]
INDEX_ROW = re.compile(r"^\|\s*(P?\d+)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*$")
SECTION = re.compile(r"^###\s+(P?\d+)\.\s")
WHO = re.compile(r"^-\s+\*\*Who decided:\*\*\s*(.+)$")


def who_class(text: str) -> str:
    """A first-pass class of a 'Who decided' line:
    user (the maintainer alone), user+claude (the maintainer, with Claude proposing or designing),
    claude-approved (Claude decided, the user approved or applied), claude-offline (Claude, in an offline PR it
    merged under the general rule), unknown."""
    t = text.lower()
    if t.startswith("claude (offline pr)"):
        return "claude-offline"
    if t.startswith("claude") and ("approved" in t or "applied" in t):
        return "claude-approved"
    if t.startswith("user/maintainer") or t.startswith("user"):
        return "user+claude" if "claude" in t else "user"
    if "not recorded" in t or "no decider" in t or "unknown" in t:
        return "unknown"
    return "claude" if t.startswith("claude") else "other"


def parse(text: str) -> list:
    rows, order = {}, []
    in_index = False
    section = None
    for line in text.splitlines():
        if line.startswith("## "):
            in_index = line.strip() == "## Index"
            section = None
            continue
        if in_index:
            m = INDEX_ROW.match(line)
            if m and m.group(1) not in ("#",):
                id_, date, decision, sources = m.groups()
                rows[id_] = {"id": id_, "date": date, "decision": decision, "sources": sources}
                rows[id_]["issue_refs"] = sorted({int(n) for n in c.ISSUE_REF.findall(sources)})
                order.append(id_)
            continue
        m = SECTION.match(line)
        if m:
            section = m.group(1)
            continue
        m = WHO.match(line)
        if m and section:
            row = rows.setdefault(section, {"id": section})
            if section not in order:
                order.append(section)
            row["who_raw"] = m.group(1).strip()
            row["who_class"] = who_class(row["who_raw"])
    return [rows[i] for i in order]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--notes", required=True, help="the dissertation repo's notes/decisions.md (read only)")
    p.add_argument("--out", required=True, help="output folder, outside every git repository")
    a = p.parse_args(argv)
    out = c.out_dir(a.out)
    with open(a.notes, encoding="utf-8") as fh:
        rows = parse(fh.read())
    print(f"decisions.csv: {c.write_csv(os.path.join(out, 'decisions.csv'), rows, COLUMNS)} decisions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
