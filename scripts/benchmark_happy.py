#!/usr/bin/env python3
"""Score each caller's VCF against the GIAB truth set with hap.py, the way Google's DeepVariant case studies do.

This replaces the exact-match counts that run_local.py --check prints for real data (which are indicative only:
no variant normalisation) with a proper benchmark: hap.py with the vcfeval engine, PASS calls only, restricted to
the truth set's confident regions. Results are comparable with the numbers Google publishes for the same data.

    python scripts/fetch_dv_testdata.py --dataset hg003-chr20 [--pangenome]
    python scripts/run_local.py --sample HG003_chr20 --reference GRCh38_chr20.fa --caller all --skip-to-variants
    python scripts/benchmark_happy.py --sample HG003_chr20 --reference GRCh38_chr20.fa --region chr20

Inputs, in the pipeline's key layout under --root:
    truth/<sample>.vcf.gz and truth/<sample>.bed     (from fetch_dv_testdata.py)
    reference/<reference> (+ .fai, written here if missing)
    variants/<sample>.vcf.gz, variants_deepvariant/..., variants_pangenome/...   (whichever exist)
Outputs: benchmarks/<sample>/<caller>.* (hap.py's own files) and benchmarks/<sample>/summary.md.

--stratification stratifications/GRCh38_chr20.tsv (from fetch_dv_testdata.py --stratifications) also scores each
GIAB region type (difficult regions, segmental duplications, low mappability) and adds a per-region table to the
summary. The overall numbers are unchanged: hap.py reports stratified rows next to the whole-region ones.

Each caller scored is also appended to the benchmark history (docs/benchmarks/history.csv, benchmark_history.py)
with the repo's commit; --aligner / --alignment / --caller-settings describe what produced the calls, and
--no-history leaves the history alone. Then `python scripts/benchmark_history.py report` and commit both.
"""

import argparse
import csv
import math
import os
import subprocess
import sys

import benchmark_history
import run_local

HAPPY_IMAGE = "jmcdani20/hap.py:v0.3.12"  # the image Google's case studies use
HAPPY_BIN = "/opt/hap.py/bin/hap.py"
CALLER_ORDER = ("bcftools", "deepvariant", "pangenome")
CALLER_LABELS = {
    "bcftools": "bcftools",
    "deepvariant": "DeepVariant (linear)",
    "pangenome": "Pangenome-aware DeepVariant",
}


def write_fai(fasta):
    """Write a samtools-compatible .fai for an uncompressed FASTA (hap.py needs one). Returns the .fai path.

    Each line: name, length, byte offset of the first base, bases per line, bytes per line. Assumes every
    sequence uses one line width (true for the references fetch_dv_testdata.py writes).
    """
    fai = fasta + ".fai"
    rows = []
    name = length = start = line_bases = line_bytes = None  # set at each header line
    with open(fasta, "rb") as fh:
        offset = 0
        for raw in fh:
            if raw.startswith(b">"):
                if name is not None:
                    rows.append((name, length, start, line_bases, line_bytes))
                name = raw[1:].split()[0].decode("ascii")
                length, start, line_bases, line_bytes = 0, offset + len(raw), None, None
            elif name is not None:
                seq = raw.rstrip(b"\r\n")
                if line_bases is None:
                    line_bases, line_bytes = len(seq), len(raw)
                length += len(seq)
            offset += len(raw)
    if name is not None:
        rows.append((name, length, start, line_bases or 0, line_bytes or 0))
    with open(fai, "w", encoding="ascii", newline="\n") as out:
        for row in rows:
            out.write("\t".join(str(v) for v in row) + "\n")
    return fai


def happy_command(sample_id, reference_name, vcf_key, out_prefix, region=None, stratification=None):
    """hap.py arguments, with paths as seen inside the container (the storage root is mounted at /data).

    stratification: key of a hap.py stratification TSV under the root (BED paths in it are relative to the TSV).
    """
    cmd = [
        HAPPY_BIN,
        f"/data/truth/{sample_id}.vcf.gz",
        f"/data/{vcf_key}",
        "-f",
        f"/data/truth/{sample_id}.bed",
        "-r",
        f"/data/reference/{reference_name}",
        "-o",
        f"/data/{out_prefix}",
        "--engine=vcfeval",
        "--pass-only",
    ]
    if region:
        cmd += ["-l", region]
    if stratification:
        cmd += ["--stratification", f"/data/{stratification}"]
    return cmd


def read_stratification(path):
    """[(region name, BED path)] from a hap.py stratification TSV, in file order. Relative BED paths are resolved
    against the TSV's folder, as hap.py does."""
    entries = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip() and not line.startswith("#"):
                cols = line.rstrip("\r\n").split("\t")
                if len(cols) < 2:
                    raise ValueError(f"{path}: expected '<name><TAB><bed>', got {line.strip()!r}")
                name, bed = cols[:2]
                entries.append((name, os.path.join(os.path.dirname(path), *bed.split("/"))))
    return entries


def read_stratification_names(path):
    """Region names from a hap.py stratification TSV (first column), in file order."""
    return [name for name, _ in read_stratification(path)]


def _number(text, kind=float):
    """A hap.py CSV cell as a number; None for empty, '.' or NaN cells."""
    if text in ("", ".", None):
        return None
    value = float(text)
    return None if math.isnan(value) else kind(value)


def has_subsets(extended_csv, subsets):
    """True if a hap.py extended.csv already has rows for every stratification region in subsets."""
    with open(extended_csv, newline="", encoding="utf-8") as fh:
        found = {row["Subset"] for row in csv.DictReader(fh)}
    return all(s in found for s in subsets)


def read_extended(path, subsets):
    """{(subset, 'SNP'|'INDEL'): {...}} from a hap.py extended.csv: PASS rows over all subtypes, for the given
    stratification regions ('*' is the whole confident region). F1 is None when a region has no variants."""
    results = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("Filter") != "PASS" or row.get("Subtype") != "*" or row.get("Subset") not in subsets:
                continue
            results[(row["Subset"], row["Type"])] = {
                "truth": _number(row["TRUTH.TOTAL"], int) or 0,
                "fn": _number(row["TRUTH.FN"], int) or 0,
                "fp": _number(row["QUERY.FP"], int) or 0,
                "f1": _number(row["METRIC.F1_Score"]),
            }
    return results


def stratified_table(results, subsets):
    """Markdown table of FN / FP and F1 per caller, one row per region and variant type.

    results: {caller: {(subset, type): {...}}} from read_extended.
    """
    callers = [c for c in CALLER_ORDER if c in results]
    lines = [
        "| Region | Type | Truth | " + " | ".join(f"{CALLER_LABELS[c]} FN / FP (F1)" for c in callers) + " |",
        "| :--- | :--- | ---: | " + " | ".join("---:" for _ in callers) + " |",
    ]
    for subset in subsets:
        for kind in ("SNP", "INDEL"):
            cells = [results[c].get((subset, kind)) for c in callers]
            if not any(cells):
                continue
            truth = next(r["truth"] for r in cells if r)
            shown = [
                f"{r['fn']:,} / {r['fp']:,} ({r['f1']:.5f})"
                if r and r["f1"] is not None
                else (f"{r['fn']:,} / {r['fp']:,}" if r else "")
                for r in cells
            ]
            name = "all confident regions" if subset == "*" else subset
            lines.append(f"| {name} | {kind} | {truth:,} | " + " | ".join(shown) + " |")
    return "\n".join(lines)


def read_summary(path):
    """{'SNP': {...}, 'INDEL': {...}} from a hap.py summary.csv, PASS rows only, numbers converted."""
    results = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("Filter") != "PASS":
                continue
            results[row["Type"]] = {
                "truth": int(row["TRUTH.TOTAL"]),
                "tp": int(row["TRUTH.TP"]),
                "fn": int(row["TRUTH.FN"]),
                "fp": int(row["QUERY.FP"]),
                "recall": float(row["METRIC.Recall"]),
                "precision": float(row["METRIC.Precision"]),
                "f1": float(row["METRIC.F1_Score"]),
            }
    return results


def summary_table(results):
    """Markdown table from {caller: {'SNP': {...}, 'INDEL': {...}}}."""
    lines = [
        "| Caller | Type | TP | FN | FP | Recall | Precision | F1 |",
        "| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for caller in CALLER_ORDER:
        for kind in ("SNP", "INDEL"):
            r = results.get(caller, {}).get(kind)
            if r:
                lines.append(
                    f"| {CALLER_LABELS[caller]} | {kind} | {r['tp']:,} | {r['fn']:,} | {r['fp']:,} | "
                    f"{r['recall']:.6f} | {r['precision']:.6f} | {r['f1']:.6f} |"
                )
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default=os.path.join(run_local.REPO_ROOT, "local_bucket"), help="local storage root")
    p.add_argument("--sample", required=True)
    p.add_argument("--reference", required=True, help="reference name under reference/")
    p.add_argument("--region", help="restrict to one contig, e.g. chr20 (hap.py -l)")
    p.add_argument(
        "--callers",
        nargs="+",
        choices=CALLER_ORDER,
        default=list(CALLER_ORDER),
        help="callers to score (default: every one with a VCF)",
    )
    p.add_argument("--image", default=HAPPY_IMAGE)
    p.add_argument("--aligner", help="history: the aligner that made the alignment (e.g. bwa-mem2, vg giraffe)")
    p.add_argument("--alignment", help="history: where the alignment came from, in words")
    p.add_argument("--coverage", help="history: e.g. 30x")
    p.add_argument(
        "--graph", help=f"history: the pangenome caller's graph (default: {benchmark_history.PANGENOME_GRAPH})"
    )
    p.add_argument(
        "--caller-settings",
        help="history: DeepVariant flags that change results (default: from $DEEPVARIANT_EXTRA_ARGS, e.g. MAPQ>=5)",
    )
    p.add_argument("--history", default=benchmark_history.HISTORY, help="benchmark history CSV")
    p.add_argument("--no-history", action="store_true", help="don't append to the benchmark history")
    p.add_argument(
        "--stratification",
        metavar="TSV_KEY",
        help="hap.py stratification TSV under the root, e.g. stratifications/GRCh38_chr20.tsv "
        "(fetch_dv_testdata.py --stratifications): adds a per-region table",
    )
    a = p.parse_args(argv)

    root = os.path.abspath(a.root)
    subsets = None
    if a.stratification:
        # a key, not a host path: always '/' (hap.py sees it inside the Linux container)
        a.stratification = a.stratification.replace("\\", "/")
        tsv = os.path.join(root, *a.stratification.split("/"))
        if not os.path.exists(tsv):
            p.error(f"no {tsv}: run fetch_dv_testdata.py --stratifications first")
        try:
            entries = read_stratification(tsv)
        except ValueError as err:
            p.error(str(err))
        missing = [bed for _, bed in entries if not os.path.exists(bed)]
        if missing:
            p.error(
                f"{len(missing)} BED(s) listed in {tsv} are missing, e.g. {missing[0]}: "
                "run fetch_dv_testdata.py --stratifications again"
            )
        subsets = ["*"] + [name for name, _ in entries]
    ref = os.path.join(root, "reference", a.reference)
    if not os.path.exists(ref + ".fai"):
        print(f"Writing {ref}.fai")
        write_fai(ref)
    out_dir = f"benchmarks/{a.sample}"
    os.makedirs(os.path.join(root, *out_dir.split("/")), exist_ok=True)

    results, stratified = {}, {}
    for caller in a.callers:
        vcf_key = run_local.VCF_KEYS[caller].format(id=a.sample)
        if not os.path.exists(os.path.join(root, *vcf_key.split("/"))):
            print(f"skip {caller}: no {vcf_key}")
            continue
        prefix = f"{out_dir}/{caller}"
        cmd = run_local.docker_command(
            happy_command(a.sample, a.reference, vcf_key, prefix, a.region, a.stratification), root, a.image
        )
        print(f"\n>>> hap.py: {caller}", flush=True)
        if subprocess.run(cmd).returncode != 0:
            print(f"hap.py failed for {caller}")
            return 1
        results[caller] = read_summary(os.path.join(root, *f"{prefix}.summary.csv".split("/")))
        if not a.no_history:
            settings = a.caller_settings
            if settings is None and caller != "bcftools":
                settings = benchmark_history.settings_tag(os.environ.get("DEEPVARIANT_EXTRA_ARGS", ""))
            meta = benchmark_history.run_metadata(
                a.sample,
                caller,
                aligner=a.aligner,
                alignment=a.alignment,
                coverage=a.coverage,
                graph=a.graph if caller == "pangenome" else None,
                caller_settings=settings,
                commit=benchmark_history.git_commit(),
                source="benchmark_happy.py",
            )
            written = benchmark_history.record(os.path.join(root, *prefix.split("/")), meta, a.history)
            print(f"{written} rows added to {a.history}")
        if subsets:
            stratified[caller] = read_extended(os.path.join(root, *f"{prefix}.extended.csv".split("/")), subsets)

    if not results:
        print("No caller VCFs found to score.")
        return 1
    table = summary_table(results)
    text = (
        f"# hap.py results: {a.sample}\n\nvcfeval engine, PASS only, GIAB confident regions"
        f"{', ' + a.region if a.region else ''}.\n\n{table}\n"
    )
    if stratified:
        text += (
            f"\n## By region type (GIAB stratifications, {a.stratification})\n\n"
            f"{stratified_table(stratified, subsets)}\n"
        )
    summary_path = os.path.join(root, *out_dir.split("/"), "summary.md")
    with open(summary_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"\n{text}\nWritten to {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
