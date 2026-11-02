#!/usr/bin/env python3
"""Benchmark history: every hap.py result as one tracked series, not one-off CSVs (#58).

docs/benchmarks/history.csv holds one row per (scoring run, caller, GIAB region, SNP/indel), with what produced it:
date, sample, coverage, alignment, aligner, caller, caller settings, graph, region, commit and source. It is
tracked in git, so a change in accuracy shows up as a diff and the series keeps its past.

    python scripts/benchmark_history.py backfill --root ../geyser-data   # the benchmarks scored before this script
    python scripts/benchmark_history.py report                          # README.md + figure CSV next to the history

benchmark_happy.py appends each caller it scores (and so do benchmark_giraffe.py / benchmark_calls.py, which run
it). Re-scoring the same run and caller on the same day replaces those rows; a later day adds new ones.

Numbers come from hap.py's extended.csv (PASS calls, all subtypes and genotypes), which also has the GIAB
stratification rows when hap.py ran with them; summary.csv is the fallback for the whole region only.
"""

import argparse
import csv
import json
import os
import subprocess
import sys
from datetime import datetime

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH_DIR = os.path.join(REPO_ROOT, "docs", "benchmarks")
HISTORY = os.path.join(BENCH_DIR, "history.csv")
REPORT_MD = os.path.join(BENCH_DIR, "README.md")
FIGURE_CSV = os.path.join(BENCH_DIR, "accuracy_by_configuration.csv")

FIELDS = [
    "date",
    "run",
    "sample",
    "coverage",
    "alignment",
    "aligner",
    "caller",
    "caller_settings",
    "graph",
    "region",
    "commit",
    "source",
    "subset",
    "type",
    "truth_total",
    "tp",
    "fn",
    "fp",
    "recall",
    "precision",
    "f1",
]
# Rows that identify one scoring of one caller: re-recording them replaces the old rows.
RUN_KEY = ("date", "run", "caller", "caller_settings")
CALLERS = ("bcftools", "deepvariant", "pangenome")
CALLER_LABELS = {"bcftools": "bcftools", "deepvariant": "linear DV", "pangenome": "pangenome DV"}
PANGENOME_GRAPH = "HPRC v1.1 MC GRCh38, chr20"
# GIAB stratifications shown as error columns in the report (when a run has them).
REPORT_SUBSETS = (
    ("alldifficultregions", "difficult"),
    ("segdups", "segdups"),
    ("lowmappabilityall", "low mappability"),
)

GOOGLE_BWA = "Google case-study BAM: BWA-MEM, whole genome, cut to chr20"
GOOGLE_30X = "Google 30x NovaSeq BAM: BWA-MEM 0.7.17 to hs38DH, chr20"
PIPELINE_READS = "pipeline align stage on HG003's chr20 reads (19.0 M)"
MAPQ5 = "MAPQ>=5"
VG_FLAGS = "VG flags"
# What produced each benchmark folder scored before this script existed (geyser-data/benchmarks/<run>/), from the
# PRs that made them. New runs record their own metadata.
KNOWN_RUNS = {
    "HG003_chr20": dict(sample="HG003", coverage="35x", alignment=GOOGLE_BWA, aligner="bwa", source="#70"),
    "HG003_chr20_mq5": dict(
        sample="HG003", coverage="35x", alignment=GOOGLE_BWA, aligner="bwa", source="#145", caller_settings=MAPQ5
    ),
    "HG003_30x_chr20": dict(sample="HG003", coverage="30x", alignment=GOOGLE_30X, aligner="bwa", source="#95"),
    "HG003_30x_chr20_mq5": dict(
        sample="HG003", coverage="30x", alignment=GOOGLE_30X, aligner="bwa", source="#95", caller_settings=MAPQ5
    ),
    "HG005_chr20": dict(sample="HG005", coverage="30x", alignment=GOOGLE_30X, aligner="bwa", source="#95"),
    "HG005_chr20_mq5": dict(
        sample="HG005", coverage="30x", alignment=GOOGLE_30X, aligner="bwa", source="#95", caller_settings=MAPQ5
    ),
    "HG003_chr20_bwa": dict(sample="HG003", coverage="35x", alignment=PIPELINE_READS, aligner="bwa", source="#110"),
    "HG003_chr20_bwamem2": dict(
        sample="HG003", coverage="35x", alignment=PIPELINE_READS, aligner="bwa-mem2", source="#110, #129"
    ),
    "HG003_chr20_giraffe": dict(
        sample="HG003",
        coverage="35x",
        alignment="vg giraffe to the HPRC v1.1 chr20 graph, HG003's chr20 reads (19.0 M)",
        aligner="vg giraffe",
        source="#129",
    ),
    "HG003_chr20_vg": dict(
        sample="HG003",
        coverage="35x",
        alignment="Google case-study BAM: vg giraffe, whole genome, cut to chr20",
        aligner="vg giraffe",
        source="#145",
    ),
    "HG003_chr20_vg_mq5": dict(
        sample="HG003",
        coverage="35x",
        alignment="Google case-study BAM: vg giraffe, whole genome, cut to chr20",
        aligner="vg giraffe",
        source="#145",
        caller_settings=MAPQ5,
    ),
}
# Linear DeepVariant on giraffe reads ran with the VG case study's make_examples flags.
KNOWN_SETTINGS = {("HG003_chr20_giraffe", "deepvariant"): VG_FLAGS, ("HG003_chr20_vg", "deepvariant"): VG_FLAGS}


# ---------- reading hap.py output ----------


def _num(text, kind=float):
    text = (text or "").strip()
    if text in ("", "nan", "NA"):
        return ""
    try:
        return kind(float(text)) if kind is int else round(float(text), 6)
    except ValueError:
        return ""


def _metrics(row) -> dict:
    return {
        "truth_total": _num(row.get("TRUTH.TOTAL"), int),
        "tp": _num(row.get("TRUTH.TP"), int),
        "fn": _num(row.get("TRUTH.FN"), int),
        "fp": _num(row.get("QUERY.FP"), int),
        "recall": _num(row.get("METRIC.Recall")),
        "precision": _num(row.get("METRIC.Precision")),
        "f1": _num(row.get("METRIC.F1_Score")),
    }


def read_happy(prefix) -> list:
    """[{subset, type, metrics...}] for PASS calls from <prefix>.extended.csv (every subset), else summary.csv."""
    rows = []
    extended = prefix + ".extended.csv"
    if os.path.exists(extended):
        with open(extended, newline="") as fh:
            for r in csv.DictReader(fh):
                if r.get("Filter") != "PASS" or r.get("Subtype") != "*" or r.get("Genotype", "*") != "*":
                    continue
                if r.get("Type") in ("SNP", "INDEL"):
                    rows.append({"subset": r["Subset"], "type": r["Type"], **_metrics(r)})
        if rows:
            return rows
    with open(prefix + ".summary.csv", newline="") as fh:
        for r in csv.DictReader(fh):
            if r.get("Filter") == "PASS" and r.get("Type") in ("SNP", "INDEL"):
                rows.append({"subset": "*", "type": r["Type"], **_metrics(r)})
    return rows


def happy_run_info(prefix) -> dict:
    """date (YYYY-MM-DD) and region from <prefix>.runinfo.json; the date falls back to the file's mtime."""
    info = {"date": "", "region": ""}
    path = prefix + ".runinfo.json"
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        info["region"] = (data.get("final_args") or {}).get("locations") or ""
        info["date"] = datetime.strptime(data["timestamp"], "%a %b %d %H:%M:%S %Y").strftime("%Y-%m-%d")
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if not info["date"]:
        for candidate in (path, prefix + ".extended.csv", prefix + ".summary.csv"):
            if os.path.exists(candidate):
                info["date"] = datetime.fromtimestamp(os.path.getmtime(candidate)).strftime("%Y-%m-%d")
                break
    return info


# ---------- metadata ----------


def settings_tag(extra_args: str) -> str:
    """A short name for DeepVariant flags that change results (batch size and shards only change speed)."""
    extra_args = extra_args or ""
    tags = []
    if "keep_legacy_allele_counter_behavior" in extra_args:
        tags.append(VG_FLAGS)
    if "min_mapping_quality=5" in extra_args:
        tags.append(MAPQ5)
    return ", ".join(tags)


def git_commit(repo=REPO_ROOT) -> str:
    """Short HEAD of the repo running the benchmark, '-dirty' with uncommitted changes, '' outside git."""
    try:
        sha = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(["git", "-C", repo, "diff", "--quiet", "HEAD"], capture_output=True).returncode != 0
    except (OSError, subprocess.CalledProcessError):
        return ""
    return sha + ("-dirty" if dirty else "")


def run_metadata(run: str, caller: str, **given) -> dict:
    """Metadata for one scoring: KNOWN_RUNS for runs scored before this script, then what the caller passed."""
    meta = {"run": run, "caller": caller, "sample": run.split("_")[0], "coverage": "", "alignment": "", "aligner": ""}
    meta["caller_settings"] = ""
    meta["graph"] = PANGENOME_GRAPH if caller == "pangenome" else ""
    meta["source"] = ""
    meta.update(KNOWN_RUNS.get(run, {}))
    if (run, caller) in KNOWN_SETTINGS:
        meta["caller_settings"] = KNOWN_SETTINGS[(run, caller)]
    meta.update({k: v for k, v in given.items() if v not in (None, "")})
    return meta


# ---------- the history file ----------


def read_history(path=HISTORY) -> list:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_history(rows, path=HISTORY) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    rows = sorted(rows, key=lambda r: (r["date"], r["run"], r["caller"], r["caller_settings"], r["subset"], r["type"]))
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows({k: r.get(k, "") for k in FIELDS} for r in rows)


def record(prefix, meta: dict, path=HISTORY) -> int:
    """Append one caller's hap.py results (<prefix>.*) to the history; returns the number of rows written.

    Rows with the same date, run, caller and settings are replaced (a re-score the same day)."""
    info = happy_run_info(prefix)
    base = {**{k: "" for k in FIELDS}, **info, **{k: v for k, v in meta.items() if k in FIELDS}}
    new = [{**base, **{k: str(v) for k, v in r.items()}} for r in read_happy(prefix)]
    if not new:
        return 0
    key = tuple(base[k] for k in RUN_KEY)
    kept = [r for r in read_history(path) if tuple(r[k] for k in RUN_KEY) != key]
    write_history(kept + new, path)
    return len(new)


def backfill(root, path=HISTORY) -> int:
    """Record every KNOWN_RUNS folder under <root>/benchmarks that hap.py scored. Safe to run again."""
    total = 0
    for run in sorted(KNOWN_RUNS):
        for caller in CALLERS:
            prefix = os.path.join(root, "benchmarks", run, caller)
            if os.path.exists(prefix + ".summary.csv"):
                written = record(prefix, run_metadata(run, caller, commit=""), path)
                print(f"{run} {caller}: {written} rows")
                total += written
    return total


# ---------- report ----------


def configuration_label(r) -> str:
    label = f"{r['aligner'] or '?'} + {CALLER_LABELS.get(r['caller'], r['caller'])}"
    return label + (f", {r['caller_settings']}" if r["caller_settings"] else "")


def latest(rows) -> list:
    """The latest scoring of each (run, caller, settings): its rows for every subset and type."""
    newest = {}
    for r in rows:
        key = (r["run"], r["caller"], r["caller_settings"])
        newest[key] = max(newest.get(key, ""), r["date"])
    return [r for r in rows if r["date"] == newest[(r["run"], r["caller"], r["caller_settings"])]]


def _int(text):
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def configurations(rows) -> list:
    """One dict per configuration (latest scoring), wide: F1 and FN/FP for SNPs and indels, plus SNP errors
    (FN + FP) in the REPORT_SUBSETS where the run was stratified. This is the figure CSV's row."""
    by_key = {}
    for r in latest(rows):
        key = (r["run"], r["caller"], r["caller_settings"])
        c = by_key.setdefault(
            key,
            {
                "sample": r["sample"],
                "coverage": r["coverage"],
                "run": r["run"],
                "aligner": r["aligner"],
                "caller": r["caller"],
                "caller_settings": r["caller_settings"],
                "configuration": configuration_label(r),
                "alignment": r["alignment"],
                "date": r["date"],
                "source": r["source"],
            },
        )
        t = r["type"].lower()
        if r["subset"] == "*":
            c[f"{t}_f1"], c[f"{t}_fn"], c[f"{t}_fp"] = r["f1"], r["fn"], r["fp"]
        for subset, _ in REPORT_SUBSETS:
            if r["subset"] == subset and t == "snp":
                fn, fp = _int(r["fn"]), _int(r["fp"])
                c[f"snp_errors_{subset}"] = "" if fn is None or fp is None else fn + fp
    out = []
    for c in by_key.values():
        errors = [_int(c.get(k)) for k in ("snp_fn", "snp_fp", "indel_fn", "indel_fp")]
        c["errors"] = "" if None in errors else sum(errors)
        out.append(c)
    return sorted(out, key=lambda c: (c["sample"], c["coverage"], c["alignment"], c["caller"], c["caller_settings"]))


FIGURE_FIELDS = [
    "sample",
    "coverage",
    "run",
    "configuration",
    "aligner",
    "caller",
    "caller_settings",
    "snp_f1",
    "indel_f1",
    "snp_fn",
    "snp_fp",
    "indel_fn",
    "indel_fp",
    "errors",
    *(f"snp_errors_{s}" for s, _ in REPORT_SUBSETS),
    "date",
    "source",
]


def _f1(text):
    return f"{float(text):.6f}" if text not in ("", None) else ""


def report_markdown(rows) -> str:
    configs = configurations(rows)
    lines = [
        "# Benchmark history",
        "",
        "Generated by `scripts/benchmark_history.py report` from [`history.csv`](history.csv): the latest hap.py",
        "scoring of each configuration (vcfeval, PASS calls, GIAB confident regions). Errors are FN + FP.",
        "Figure data: [`accuracy_by_configuration.csv`](accuracy_by_configuration.csv). Don't edit by hand.",
        "",
    ]
    for (sample, coverage, alignment), group in _groups(configs):
        lines += [f"## {sample} {coverage}: {alignment}", ""]
        head = "| Configuration | SNP F1 | SNP FN / FP | Indel F1 | Indel FN / FP | Errors |"
        head += "".join(f" SNP errors, {label} |" for _, label in REPORT_SUBSETS) + " Scored | Source |"
        lines += [head, "|" + " :--- |" + " ---: |" * (5 + len(REPORT_SUBSETS)) + " :--- | :--- |"]
        for c in group:
            cells = [
                c["configuration"],
                _f1(c.get("snp_f1")),
                f"{c.get('snp_fn', '')} / {c.get('snp_fp', '')}",
                _f1(c.get("indel_f1")),
                f"{c.get('indel_fn', '')} / {c.get('indel_fp', '')}",
                str(c["errors"]),
                *(str(c.get(f"snp_errors_{s}", "")) for s, _ in REPORT_SUBSETS),
                c["date"],
                c["source"],
            ]
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")
    changes = _changes(rows)
    if changes:
        lines += [
            "## Re-scored configurations",
            "",
            "| Configuration | Run | Date | SNP F1 | Indel F1 |",
            "| :--- | :--- | :--- | ---: | ---: |",
        ]
        lines += changes
        lines.append("")
    return "\n".join(lines)


def _groups(configs):
    groups = {}
    for c in configs:
        groups.setdefault((c["sample"], c["coverage"], c["alignment"]), []).append(c)
    return groups.items()


def _changes(rows) -> list:
    """Table lines for configurations scored on more than one date (the series' point)."""
    dates = {}
    for r in rows:
        if r["subset"] == "*":
            dates.setdefault((r["run"], r["caller"], r["caller_settings"]), {}).setdefault(r["date"], {})[r["type"]] = r
    lines = []
    for (run, _, _), by_date in sorted(dates.items()):
        if len(by_date) < 2:
            continue
        for date in sorted(by_date):
            snp, indel = by_date[date].get("SNP", {}), by_date[date].get("INDEL", {})
            label = configuration_label(snp or indel)
            lines.append(f"| {label} | {run} | {date} | {_f1(snp.get('f1'))} | {_f1(indel.get('f1'))} |")
    return lines


def write_report(rows, md_path=REPORT_MD, csv_path=FIGURE_CSV) -> None:
    with open(md_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(report_markdown(rows))
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIGURE_FIELDS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(configurations(rows))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--history", default=HISTORY, help="history CSV (default: docs/benchmarks/history.csv)")
    sub = p.add_subparsers(dest="command", required=True)
    b = sub.add_parser("backfill", help="record the benchmark folders scored before this script (KNOWN_RUNS)")
    b.add_argument("--root", required=True, help="data root with benchmarks/<run>/ (e.g. ../geyser-data)")
    r = sub.add_parser("report", help="write the Markdown report and the figure CSV next to the history")
    r.add_argument("--markdown", help="default: README.md next to the history")
    r.add_argument("--figure-csv", help="default: accuracy_by_configuration.csv next to the history")
    a = p.parse_args(argv)

    if a.command == "backfill":
        print(f"{backfill(os.path.abspath(a.root), a.history)} rows recorded in {a.history}")
        return 0
    rows = read_history(a.history)
    if not rows:
        print(f"No history at {a.history}")
        return 1
    folder = os.path.dirname(os.path.abspath(a.history))
    md = a.markdown or os.path.join(folder, "README.md")
    fig = a.figure_csv or os.path.join(folder, "accuracy_by_configuration.csv")
    write_report(rows, md, fig)
    print(f"Wrote {md} and {fig} ({len(configurations(rows))} configurations)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
