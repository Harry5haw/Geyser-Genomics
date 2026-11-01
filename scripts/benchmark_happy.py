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
"""

import argparse
import csv
import os
import subprocess
import sys

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
    name = None
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


def happy_command(sample_id, reference_name, vcf_key, out_prefix, region=None):
    """hap.py arguments, with paths as seen inside the container (the storage root is mounted at /data)."""
    cmd = [
        HAPPY_BIN,
        f"/data/truth/{sample_id}.vcf.gz",
        f"/data/{vcf_key}",
        "-f", f"/data/truth/{sample_id}.bed",
        "-r", f"/data/reference/{reference_name}",
        "-o", f"/data/{out_prefix}",
        "--engine=vcfeval",
        "--pass-only",
    ]
    if region:
        cmd += ["-l", region]
    return cmd


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
                lines.append(f"| {CALLER_LABELS[caller]} | {kind} | {r['tp']:,} | {r['fn']:,} | {r['fp']:,} | "
                             f"{r['recall']:.6f} | {r['precision']:.6f} | {r['f1']:.6f} |")
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default=os.path.join(run_local.REPO_ROOT, "local_bucket"), help="local storage root")
    p.add_argument("--sample", required=True)
    p.add_argument("--reference", required=True, help="reference name under reference/")
    p.add_argument("--region", help="restrict to one contig, e.g. chr20 (hap.py -l)")
    p.add_argument("--callers", nargs="+", choices=CALLER_ORDER, default=list(CALLER_ORDER),
                   help="callers to score (default: every one with a VCF)")
    p.add_argument("--image", default=HAPPY_IMAGE)
    a = p.parse_args(argv)

    root = os.path.abspath(a.root)
    ref = os.path.join(root, "reference", a.reference)
    if not os.path.exists(ref + ".fai"):
        print(f"Writing {ref}.fai")
        write_fai(ref)
    out_dir = f"benchmarks/{a.sample}"
    os.makedirs(os.path.join(root, *out_dir.split("/")), exist_ok=True)

    results = {}
    for caller in a.callers:
        vcf_key = run_local.VCF_KEYS[caller].format(id=a.sample)
        if not os.path.exists(os.path.join(root, *vcf_key.split("/"))):
            print(f"skip {caller}: no {vcf_key}")
            continue
        prefix = f"{out_dir}/{caller}"
        cmd = run_local.docker_command(happy_command(a.sample, a.reference, vcf_key, prefix, a.region), root, a.image)
        print(f"\n>>> hap.py: {caller}", flush=True)
        if subprocess.run(cmd).returncode != 0:
            print(f"hap.py failed for {caller}")
            return 1
        results[caller] = read_summary(os.path.join(root, *f"{prefix}.summary.csv".split("/")))

    if not results:
        print("No caller VCFs found to score.")
        return 1
    table = summary_table(results)
    summary_path = os.path.join(root, *out_dir.split("/"), "summary.md")
    with open(summary_path, "w", encoding="utf-8") as fh:
        fh.write(f"# hap.py results: {a.sample}\n\nvcfeval engine, PASS only, GIAB confident regions"
                 f"{', ' + a.region if a.region else ''}.\n\n{table}\n")
    print(f"\n{table}\n\nWritten to {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
