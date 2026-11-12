#!/usr/bin/env python3
"""The web UI's page-weight budget and a summary of the Lighthouse CI runs (.github/workflows/web-perf.yml).

Lighthouse CI asserts the timing budgets itself (lighthouserc.json: LCP, CLS, TBT, each on its median over the runs). What it
can't assert is the combined weight of a page's HTML, CSS and JS, which is the budget the web plan sets
(<= 30 KB transferred, data downloads excluded). This script reads the runs `lhci collect` stored
(.lighthouseci/lhr-*.json), and per page:

- sums the transferred bytes of its Document, Stylesheet and Script requests (the median over runs);
- reports the median LCP, CLS and TBT against lighthouserc.json's limits;
- writes a Markdown table (to $GITHUB_STEP_SUMMARY in CI);
- exits 1 if any page is over the weight budget (the timing budgets fail the `lhci assert` step).

    python scripts/web_perf_budget.py --dir .lighthouseci --budget-kb 30 [--summary FILE]
"""

import argparse
import glob
import json
import os
import re
import statistics
import sys

WEIGHT_TYPES = ("Document", "Stylesheet", "Script")  # network-requests resourceType values counted
METRICS = {  # Lighthouse audit id -> (label, unit)
    "largest-contentful-paint": ("LCP", "ms"),
    "cumulative-layout-shift": ("CLS", ""),
    "total-blocking-time": ("TBT", "ms"),
}


def load_runs(directory: str) -> list:
    """Every Lighthouse result (LHR) `lhci collect` stored in directory."""
    runs = []
    for path in sorted(glob.glob(os.path.join(directory, "lhr-*.json"))):
        with open(path, encoding="utf-8") as fh:
            runs.append(json.load(fh))
    return runs


def page_url(lhr: dict) -> str:
    return lhr.get("requestedUrl") or lhr.get("finalDisplayedUrl") or lhr.get("finalUrl") or "?"


def page_weight(lhr: dict) -> tuple:
    """(bytes transferred, requests) for the page's HTML, CSS and JS in one run."""
    items = lhr["audits"]["network-requests"]["details"]["items"]
    counted = [i for i in items if i.get("resourceType") in WEIGHT_TYPES]
    return sum(int(i.get("transferSize") or 0) for i in counted), len(counted)


def limits_from_config(path: str) -> list:
    """[(URL pattern, {audit id: maxNumericValue})] from lighthouserc.json (the one copy of the timing budgets): its
    assertMatrix, one entry per site, or its plain assertions for every URL."""
    with open(path, encoding="utf-8") as fh:
        config = json.load(fh)["ci"]["assert"]
    entries = config.get("assertMatrix") or [{"matchingUrlPattern": ".*", "assertions": config["assertions"]}]
    return [
        (e["matchingUrlPattern"], {a: o[1]["maxNumericValue"] for a, o in e["assertions"].items() if a in METRICS})
        for e in entries
    ]


def limits_for(limits, url: str) -> dict:
    """The limits that apply to url: a dict is the same for every page; a list is limits_from_config's entries."""
    if isinstance(limits, dict):
        return limits
    return next((lim for pattern, lim in limits if re.search(pattern, url)), {})


def summarise(runs: list, budget_bytes: int, limits: dict) -> list:
    """One row per page: its median weight, requests and metrics, and whether each is within budget."""
    by_page = {}
    for lhr in runs:
        by_page.setdefault(page_url(lhr), []).append(lhr)
    rows = []
    for url, page_runs in sorted(by_page.items()):
        weights = [page_weight(r) for r in page_runs]
        row = {
            "url": url,
            "runs": len(page_runs),
            "bytes": int(statistics.median(w[0] for w in weights)),
            "requests": int(statistics.median(w[1] for w in weights)),
        }
        row["weight_ok"] = row["bytes"] <= budget_bytes
        page_limits = limits_for(limits, url)
        for audit in METRICS:
            values = [r["audits"][audit]["numericValue"] for r in page_runs if audit in r["audits"]]
            value = statistics.median(values) if values else None
            row[audit] = value
            ok = None if value is None or audit not in page_limits else value <= page_limits[audit]
            row[f"{audit}_ok"] = ok
        rows.append(row)
    return rows


def _mark(ok) -> str:
    return "" if ok is None else (" ✅" if ok else " ❌")


def markdown(rows: list, budget_bytes: int, limits) -> str:
    def limit(audit, lim):
        label, unit = METRICS[audit]
        return f"{label} ≤ {lim[audit]:g}{(' ' + unit) if unit else ''}" if audit in lim else label

    sets = [("", limits)] if isinstance(limits, dict) else limits
    timing = "; ".join(
        (f"`{pattern}`: " if len(sets) > 1 else "") + ", ".join(limit(a, lim) for a in METRICS) for pattern, lim in sets
    )
    lines = [
        "## Web performance (Lighthouse CI, mobile)",
        "",
        f"Budgets: HTML+CSS+JS ≤ {budget_bytes / 1024:g} KB transferred; {timing}. Median of the runs per page.",
        "",
        "| Page | HTML+CSS+JS | Requests | LCP | CLS | TBT |",
        "| :--- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in rows:
        lcp, cls, tbt = (r[a] for a in METRICS)
        lines.append(
            f"| `{r['url']}` ({r['runs']} runs) | {r['bytes'] / 1024:.1f} KB{_mark(r['weight_ok'])} | {r['requests']} | "
            + (f"{lcp:.0f} ms" if lcp is not None else "–")
            + _mark(r["largest-contentful-paint_ok"])
            + " | "
            + (f"{cls:.3f}" if cls is not None else "–")
            + _mark(r["cumulative-layout-shift_ok"])
            + " | "
            + (f"{tbt:.0f} ms" if tbt is not None else "–")
            + _mark(r["total-blocking-time_ok"])
            + " |"
        )
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dir", default=".lighthouseci", help="where lhci collect stored its lhr-*.json")
    p.add_argument("--budget-kb", type=float, default=30, help="HTML+CSS+JS transferred per page, in KB (1024 B)")
    p.add_argument("--config", default="lighthouserc.json", help="for the timing limits shown in the table")
    p.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"), help="append the table to this file")
    a = p.parse_args(argv)

    runs = load_runs(a.dir)
    if not runs:
        print(f"No Lighthouse results (lhr-*.json) in {a.dir}", file=sys.stderr)
        return 1
    budget = int(a.budget_kb * 1024)
    limits = limits_from_config(a.config)
    rows = summarise(runs, budget, limits)
    table = markdown(rows, budget, limits)
    print(table)
    if a.summary:
        with open(a.summary, "a", encoding="utf-8") as fh:
            fh.write(table)
    over = [r["url"] for r in rows if not r["weight_ok"]]
    if over:
        print(f"Over the {a.budget_kb:g} KB HTML+CSS+JS budget: {', '.join(over)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
