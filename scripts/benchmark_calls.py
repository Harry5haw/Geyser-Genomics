#!/usr/bin/env python3
"""Score caller configurations on existing alignments: one CSV row per (alignment, caller, settings) (#51).

Where benchmark_giraffe.py makes its own alignments, this compares callers on alignments that already exist, e.g.
Google's two pangenome-aware case-study BAMs of the same HG003 35x reads (whole-genome BWA and whole-genome vg
giraffe, both cut to chr20). That removes the bias of aligning only reads a linear aligner put on chr20.

    python scripts/fetch_dv_testdata.py --dataset hg003-chr20 --pangenome --stratifications
    python scripts/fetch_dv_testdata.py --dataset hg003-chr20-vg
    python scripts/benchmark_calls.py --root <root> --preset google-case-studies --reference GRCh38_chr20.fa \\
        --region chr20 --stratification stratifications/GRCh38_chr20.tsv --pg-shards 12 --pg-memory 18g

Each configuration runs on its own sample name: a configuration whose sample differs from its alignment's (e.g.
a MAPQ>=5 run) gets a hard link of that alignment, so every caller output has its own key. Calls and hap.py results
that already exist are reused (delete a sample's VCF to redo it). Outputs: benchmarks/calls_<preset>.csv and
benchmarks/calls_<preset>/summary.md.
"""

import argparse
import os
import sys
import time

import benchmark_aligners as ba
import benchmark_giraffe as bg
import benchmark_happy
import run_local

PANGENOME_MQ5 = {
    **bg.PANGENOME_ENV,
    "DEEPVARIANT_EXTRA_ARGS": bg.PANGENOME_ENV["DEEPVARIANT_EXTRA_ARGS"] + bg.MAPQ5_ARGS,
}
VG_LINEAR = {"DEEPVARIANT_EXTRA_ARGS": bg.VG_LINEAR_DV_ARGS}

# preset: {"base": truth/<base>.*, "configs": [(label, sample, alignment sample, caller, env, DV memory)]}
PRESETS = {
    "google-case-studies": {
        "base": "HG003_chr20",
        "configs": [
            ("BWA + linear DV", "HG003_chr20", "HG003_chr20", "deepvariant", {}, "12g"),
            ("BWA + pangenome DV", "HG003_chr20", "HG003_chr20", "pangenome", bg.PANGENOME_ENV, "14g"),
            ("BWA + pangenome DV, MAPQ>=5", "HG003_chr20_mq5", "HG003_chr20", "pangenome", PANGENOME_MQ5, "14g"),
            ("giraffe + linear DV (VG flags)", "HG003_chr20_vg", "HG003_chr20_vg", "deepvariant", VG_LINEAR, "12g"),
            ("giraffe + pangenome DV", "HG003_chr20_vg", "HG003_chr20_vg", "pangenome", bg.PANGENOME_ENV, "14g"),
            (
                "giraffe + pangenome DV, MAPQ>=5",
                "HG003_chr20_vg_mq5",
                "HG003_chr20_vg",
                "pangenome",
                PANGENOME_MQ5,
                "14g",
            ),
        ],
    },
}


def summary_markdown(preset, rows):
    lines = [
        f"# Caller configurations: {preset}",
        "",
        "| Configuration | SNP FN / FP | SNP F1 | Indel FN / FP | Indel F1 | SNP+indel errors | SNP errors in segdups "
        "| SNP errors, low mappability |",
        "| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in rows:
        f1 = {k: f"{r[k]:.6f}" if isinstance(r[k], float) else "" for k in ("snp_f1", "indel_f1")}
        total = sum(r[k] for k in ("snp_fn", "snp_fp", "indel_fn", "indel_fp") if isinstance(r[k], int))
        lines.append(
            f"| {r['configuration']} | {r['snp_fn']} / {r['snp_fp']} | {f1['snp_f1']} | {r['indel_fn']} / "
            f"{r['indel_fp']} | {f1['indel_f1']} | {total} | {r['segdups_snp_errors']} | "
            f"{r['lowmappabilityall_snp_errors']} |"
        )
    return "\n".join(lines) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default=os.path.join(run_local.REPO_ROOT, "local_bucket"))
    p.add_argument("--preset", choices=sorted(PRESETS), required=True)
    p.add_argument("--reference", required=True)
    p.add_argument("--region", help="hap.py -l, e.g. chr20")
    p.add_argument("--stratification", help="hap.py stratification TSV key (benchmark_happy --stratification)")
    p.add_argument("--pg-shards", type=int, default=6, help="make_examples workers for pangenome DV")
    p.add_argument("--pg-memory", default="14g", help="memory cap for the pangenome DV container")
    a = p.parse_args(argv)

    root = os.path.abspath(a.root)
    preset = PRESETS[a.preset]
    out_dir = ba.path_of(root, f"benchmarks/calls_{a.preset}")
    os.makedirs(out_dir, exist_ok=True)
    subsets = ["*"] + (
        benchmark_happy.read_stratification_names(ba.path_of(root, a.stratification)) if a.stratification else []
    )
    for _, _, source, _, _, _ in preset["configs"]:
        if ba.alignment_path(root, source) is None:
            p.error(f"no alignments/{source}.cram or .bam: fetch it first (see --help)")

    rows = []
    for label, sid, source, caller, env, dv_memory in preset["configs"]:
        aln = ba.alignment_path(root, source)
        if sid != source:
            bg.link_or_copy(aln, ba.path_of(root, f"alignments/{sid}.{aln.rsplit('.', 1)[-1]}"))
        env, dv_memory = bg.caller_settings(caller, env, dv_memory, a.pg_shards, a.pg_memory)
        cpath = os.path.join(out_dir, f"calls_{sid}_{caller}.json")
        if not os.path.exists(run_local.vcf_path(root, caller, sid)):
            print(f">>> calls: {label}", flush=True)
            started = time.monotonic()
            if bg.run_caller(root, sid, a.reference, caller, env, dv_memory) != 0:
                return 1
            ba.save_json(
                cpath,
                {
                    "calls_wall_s": round(time.monotonic() - started, 1),
                    "calls_shards": env.get("DEEPVARIANT_SHARDS", "default"),
                },
            )
        if sid != preset["base"]:
            ba.ensure_truth(root, preset["base"], sid)
        prefix = ba.path_of(root, f"benchmarks/{sid}/{caller}")
        if not os.path.exists(prefix + ".extended.csv") or (a.stratification and not bg._has_subsets(prefix, subsets)):
            args = ["--root", root, "--sample", sid, "--reference", a.reference, "--callers", caller]
            args += ["--region", a.region] if a.region else []
            args += ["--stratification", a.stratification] if a.stratification else []
            if benchmark_happy.main(args) != 0:
                return 1
        summary = benchmark_happy.read_summary(prefix + ".summary.csv")
        extended = benchmark_happy.read_extended(prefix + ".extended.csv", subsets)
        aligner = label.split(" + ")[0]
        rows.append(bg.result_row(label, aligner, sid, caller, summary, extended, ba.load_json(cpath) or {}))

    csv_path = ba.path_of(root, f"benchmarks/calls_{a.preset}.csv")
    ba.write_csv(csv_path, rows)
    text = summary_markdown(a.preset, rows)
    with open(os.path.join(out_dir, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"\n{text}\nWritten to {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
