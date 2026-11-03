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
a MAPQ>=5 run) gets a hard link of that alignment, so every caller output has its own key. Calls are redone when the
alignment or the caller settings change, and hap.py when the VCF changes (benchmark_giraffe.score_config); a VCF
made before this script recorded its settings is reused and marked 'unrecorded' in the CSV. Outputs:
benchmarks/calls_<preset>.csv and benchmarks/calls_<preset>/summary.md.
"""

import argparse
import os
import sys

import benchmark_aligners as ba
import benchmark_giraffe as bg
import benchmark_happy
import benchmark_history
import run_local

PANGENOME_MQ5 = {
    **bg.PANGENOME_ENV,
    "DEEPVARIANT_EXTRA_ARGS": bg.PANGENOME_ENV["DEEPVARIANT_EXTRA_ARGS"] + bg.MAPQ5_ARGS,
}
VG_LINEAR = {"DEEPVARIANT_EXTRA_ARGS": bg.VG_LINEAR_DV_ARGS}

# preset: {"base": truth/<base>.*, "configs": [(label, aligner, sample, alignment sample, caller, env, DV memory
# [, pangenome graph under pangenome/; default run_local.DEFAULT_PANGENOME, the HPRC v1.1 chr20 graph])]}
PRESETS = {
    "google-case-studies": {
        "base": "HG003_chr20",
        "configs": [
            ("BWA + linear DV", "bwa", "HG003_chr20", "HG003_chr20", "deepvariant", {}, "12g"),
            ("BWA + pangenome DV", "bwa", "HG003_chr20", "HG003_chr20", "pangenome", bg.PANGENOME_ENV, "14g"),
            (
                "BWA + pangenome DV, MAPQ>=5",
                "bwa",
                "HG003_chr20_mq5",
                "HG003_chr20",
                "pangenome",
                PANGENOME_MQ5,
                "14g",
            ),
            (
                "giraffe + linear DV (VG flags)",
                "giraffe",
                "HG003_chr20_vg",
                "HG003_chr20_vg",
                "deepvariant",
                VG_LINEAR,
                "12g",
            ),
            (
                "giraffe + pangenome DV",
                "giraffe",
                "HG003_chr20_vg",
                "HG003_chr20_vg",
                "pangenome",
                bg.PANGENOME_ENV,
                "14g",
            ),
            (
                "giraffe + pangenome DV, MAPQ>=5",
                "giraffe",
                "HG003_chr20_vg_mq5",
                "HG003_chr20_vg",
                "pangenome",
                PANGENOME_MQ5,
                "14g",
            ),
        ],
    },
}
# HPRC release 2 benchmark graph (held out: HG002, HG005, NA19240), chr20 cut out by fetch_dv_testdata --pangenome r2-chr20.
R2_GRAPH = "hprc-v2.0-mc-grch38-eval.chr20.gbz"
PRESETS["hprc-r2"] = {
    "base": "HG003_chr20",
    "configs": [
        ("BWA + linear DV", "bwa", "HG003_chr20", "HG003_chr20", "deepvariant", {}, "12g"),
        ("BWA + pangenome DV, HPRC v1.1", "bwa", "HG003_chr20", "HG003_chr20", "pangenome", bg.PANGENOME_ENV, "14g"),
        (
            "BWA + pangenome DV, HPRC r2",
            "bwa",
            "HG003_chr20_r2",
            "HG003_chr20",
            "pangenome",
            bg.PANGENOME_ENV,
            "14g",
            R2_GRAPH,
        ),
        (
            "giraffe + linear DV (VG flags)",
            "giraffe",
            "HG003_chr20_vg",
            "HG003_chr20_vg",
            "deepvariant",
            VG_LINEAR,
            "12g",
        ),
        (
            "giraffe + pangenome DV, HPRC v1.1",
            "giraffe",
            "HG003_chr20_vg",
            "HG003_chr20_vg",
            "pangenome",
            bg.PANGENOME_ENV,
            "14g",
        ),
        (
            "giraffe + pangenome DV, HPRC r2",
            "giraffe",
            "HG003_chr20_vg_r2",
            "HG003_chr20_vg",
            "pangenome",
            bg.PANGENOME_ENV,
            "14g",
            R2_GRAPH,
        ),
    ],
}
# A first look at r2 from its VCF (a different construction method; see fetch_dv_testdata "r2-chr20-vcf").
R2_VCF_GRAPH = "hprc-v2.0-mc-grch38-eval.chr20.vcf-construct.gbz"
PRESETS["hprc-r2-vcf"] = {
    "base": "HG003_chr20",
    "configs": [
        c
        for c in PRESETS["hprc-r2"]["configs"]
        if (c[7] if len(c) > 7 else None) is None  # the linear and r1.1 rows, unchanged
    ]
    + [
        (
            "BWA + pangenome DV, HPRC r2 (VCF-built)",
            "bwa",
            "HG003_chr20_r2vcf",
            "HG003_chr20",
            "pangenome",
            bg.PANGENOME_ENV,
            "14g",
            R2_VCF_GRAPH,
        ),
        (
            "giraffe + pangenome DV, HPRC r2 (VCF-built)",
            "giraffe",
            "HG003_chr20_vg_r2vcf",
            "HG003_chr20_vg",
            "pangenome",
            bg.PANGENOME_ENV,
            "14g",
            R2_VCF_GRAPH,
        ),
    ],
}
ERROR_KEYS = ("snp_fn", "snp_fp", "indel_fn", "indel_fp")


def unpack(config):
    """(label, aligner, sample, source, caller, env, memory, graph): graph is None for the default."""
    label, aligner, sid, source, caller, env, memory, *rest = config
    return label, aligner, sid, source, caller, env, memory, (rest[0] if rest else None)


def history_metadata(aligner, source):
    """What produced a configuration's calls, for the benchmark history: its aligner, plus the alignment and coverage
    of the alignment it reuses when that one is a known benchmark run (a configuration's own sample name, e.g. an
    _mq5 one, is only a link to it)."""
    known = benchmark_history.KNOWN_RUNS.get(source, {})
    # No "graph": every preset's pangenome caller uses run_local's default graph, the history's default. A preset
    # with its own graph passes it as "graph" (a GBZ name under pangenome/ or a label; benchmark_happy --graph).
    return {
        "aligner": bg.ALIGNER_NAMES.get(aligner, aligner),
        **{k: known[k] for k in ("alignment", "coverage") if k in known},
    }


def missing_inputs(root, preset, reference, graph=run_local.DEFAULT_PANGENOME):
    """Keys a preset needs that are not under root: alignments, truth, reference, and the graph for pangenome runs."""
    need = [f"truth/{preset['base']}.vcf.gz", f"truth/{preset['base']}.bed", f"reference/{reference}"]
    for _, _, _, _, caller, _, _, config_graph in map(unpack, preset["configs"]):
        if caller == "pangenome" and f"pangenome/{config_graph or graph}" not in need:
            need.append(f"pangenome/{config_graph or graph}")
    missing = [k for k in need if not os.path.exists(ba.path_of(root, k))]
    for source in dict.fromkeys(c[3] for c in preset["configs"]):
        if ba.alignment_path(root, source) is None:
            missing.append(f"alignments/{source}.cram or .bam")
    return missing


def error_total(row):
    """SNP + indel FN + FP, or '' if any of the four counts is missing (a partial sum would look like a real one)."""
    values = [row.get(k) for k in ERROR_KEYS]
    return sum(values) if all(isinstance(v, int) for v in values) else ""


def summary_markdown(preset, rows):
    lines = [
        f"# Caller configurations: {preset}",
        "",
        "| Configuration | SNP FN / FP | SNP F1 | Indel FN / FP | Indel F1 | SNP+indel errors | SNP errors in segdups "
        "| SNP errors, low mappability | Calls |",
        "| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :--- |",
    ]
    for r in rows:
        f1 = {k: f"{r[k]:.6f}" if isinstance(r[k], float) else "" for k in ("snp_f1", "indel_f1")}
        lines.append(
            f"| {r['configuration']} | {r['snp_fn']} / {r['snp_fp']} | {f1['snp_f1']} | {r['indel_fn']} / "
            f"{r['indel_fp']} | {f1['indel_f1']} | {error_total(r)} | {r['segdups_snp_errors']} | "
            f"{r['lowmappabilityall_snp_errors']} | {r.get('calls_provenance', '')} |"
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
    p.add_argument("--pg-memory", help="memory cap for pangenome DV (default: each configuration's own)")
    a = p.parse_args(argv)

    root = os.path.abspath(a.root)
    preset = PRESETS[a.preset]
    missing = missing_inputs(root, preset, a.reference)
    if a.stratification and not os.path.exists(ba.path_of(root, a.stratification)):
        missing.append(a.stratification)
    if missing:
        p.error("missing before any run: " + ", ".join(missing) + " (fetch them first, see --help)")
    out_dir = ba.path_of(root, f"benchmarks/calls_{a.preset}")
    os.makedirs(out_dir, exist_ok=True)
    subsets = ["*"] + (
        benchmark_happy.read_stratification_names(ba.path_of(root, a.stratification)) if a.stratification else []
    )

    rows = []
    for label, aligner, sid, source, caller, env, dv_memory, graph in map(unpack, preset["configs"]):
        if sid != source:
            aln = ba.alignment_path(root, source)
            bg.link_or_copy(aln, ba.path_of(root, f"alignments/{sid}.{aln.rsplit('.', 1)[-1]}"))
        env, dv_memory = bg.caller_settings(caller, env, dv_memory, a.pg_shards, a.pg_memory)
        cpath = os.path.join(out_dir, f"calls_{sid}_{caller}.json")
        scored = bg.score_config(
            root,
            out_dir,
            cpath,
            label,
            sid,
            caller,
            env,
            dv_memory,
            a.reference,
            preset["base"],
            a.region,
            a.stratification,
            subsets,
            history={**history_metadata(aligner, source), **({"graph": graph} if graph else {})},
            pangenome=graph,
        )
        if scored is None:
            return 1
        summary, extended, info = scored
        rows.append(bg.result_row(label, aligner, sid, caller, summary, extended, info))

    csv_path = ba.path_of(root, f"benchmarks/calls_{a.preset}.csv")
    ba.write_csv(csv_path, rows)
    text = summary_markdown(a.preset, rows)
    with open(os.path.join(out_dir, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"\n{text}\nWritten to {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
