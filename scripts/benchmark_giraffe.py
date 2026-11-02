#!/usr/bin/env python3
"""Benchmark graph alignment (vg giraffe) against linear alignment (bwa-mem2) for pangenome-aware calling (#51).

Google's pangenome-aware VG case study aligns reads to the HPRC graph with vg giraffe (haplotype sampling: a
personalised pangenome from the sample's own k-mers), surjects them to GRCh38 and calls with pangenome-aware
DeepVariant. This script does the same on our data and compares it with the bwa-mem2 BAM made by
benchmark_aligners.py, on the same reads, with each caller scored by hap.py (with GIAB stratifications).

    python scripts/benchmark_aligners.py ...            # makes <base>_bwa reads and the <base>_bwamem2 BAM
    python scripts/benchmark_giraffe.py --root <root> --base HG003_chr20 --reference GRCh38_chr20.fa \\
        --graph hprc-v1.1-mc-grch38.chr20.gbz --region chr20 --stratification stratifications/GRCh38_chr20.tsv

Steps (each skipped when its output exists, so an interrupted run resumes; timings kept as JSON):
  1. graph indexes   distance index, r-index and haplotype information (.hapl) for the graph, plus the list of
                     GRCh38 paths for surjection (vg 1.55.0, as in Google's case study). Built once per graph.
  2. kmers           k-mer counts of the reads (kmc 3.2, k=29, KFF), for haplotype sampling.
  3. giraffe         vg giraffe -o BAM --ref-paths ... -P -L 3000 with --haplotype-name/--kff-name (Google's flags),
                     unsorted, then in the geyser-app image: contig names without the 'GRCh38#0#' prefix, then
                     samtools fixmate | sort | markdup, the same post-processing as the align stage, so the two
                     BAMs differ only in the aligner. Writes alignments/<base>_giraffe.bam.
  4. calls           run_local.py --skip-to-variants for each configuration in CONFIGS (pangenome-aware DV on both
                     BAMs; linear DV on the giraffe BAM with Google's VG flags; the bwa-mem2 linear DV call is reused).
  5. hap.py          benchmark_happy.py per sample, then benchmarks/giraffe_<base>.csv and a summary.
"""

import argparse
import os
import shutil
import sys
import time

import benchmark_aligners as ba
import benchmark_happy
import run_local

VG_IMAGE = "quay.io/vgteam/vg:v1.55.0"  # the vg release Google's VG case study uses
KMC_IMAGE = "quay.io/biocontainers/kmc:3.2.4--h5814d7d_6"
KMER_LENGTH = 29  # vg haplotypes' default minimizer k; the KFF counts must use the same k
# The align stage's samtools sort settings (tasks.SORT_MAX_THREADS / SORT_MEMORY_PER_THREAD; a test keeps them equal).
SORT_THREADS = 4
SORT_MEMORY = "512M"
# Google's VG case study flags for linear DeepVariant on giraffe BAMs.
VG_LINEAR_DV_ARGS = (
    "--make_examples_extra_args=min_mapping_quality=0,keep_legacy_allele_counter_behavior=true,normalize_reads=true"
)
# Pangenome-aware DV settings used for every benchmark so far (10 GB GPU: batch 256; 6 make_examples workers).
PANGENOME_ENV = {"DEEPVARIANT_SHARDS": "6", "DEEPVARIANT_EXTRA_ARGS": "--call_variants_extra_args=batch_size=256"}
MAPQ5_ARGS = " --make_examples_extra_args=min_mapping_quality=5"

# (label, aligner, sample suffix, caller, extra env, DV memory). Sample suffixes ending _mq5 are hard links of
# the aligner's BAM, so each configuration has its own VCF key.
CONFIGS = [
    ("bwa-mem2 + linear DV", "bwa-mem2", "bwamem2", "deepvariant", {}, "12g"),
    ("bwa-mem2 + pangenome DV", "bwa-mem2", "bwamem2", "pangenome", PANGENOME_ENV, "14g"),
    ("giraffe + linear DV", "giraffe", "giraffe", "deepvariant", {"DEEPVARIANT_EXTRA_ARGS": VG_LINEAR_DV_ARGS}, "12g"),
    ("giraffe + pangenome DV", "giraffe", "giraffe", "pangenome", PANGENOME_ENV, "14g"),
]
MAPQ5_CONFIGS = [
    (
        "bwa-mem2 + pangenome DV, MAPQ>=5",
        "bwa-mem2",
        "bwamem2_mq5",
        "pangenome",
        {**PANGENOME_ENV, "DEEPVARIANT_EXTRA_ARGS": PANGENOME_ENV["DEEPVARIANT_EXTRA_ARGS"] + MAPQ5_ARGS},
        "14g",
    ),
    (
        "giraffe + pangenome DV, MAPQ>=5",
        "giraffe",
        "giraffe_mq5",
        "pangenome",
        {**PANGENOME_ENV, "DEEPVARIANT_EXTRA_ARGS": PANGENOME_ENV["DEEPVARIANT_EXTRA_ARGS"] + MAPQ5_ARGS},
        "14g",
    ),
]
# Region types reported per configuration (GIAB stratification names; '*' = all confident regions).
REGIONS = ("*", "segdups", "lowmappabilityall", "alldifficultregions", "notinalldifficultregions")


def graph_keys(graph):
    """Keys of the per-graph index files, next to pangenome/<graph>."""
    stem = graph[:-4] if graph.endswith(".gbz") else graph
    base = f"pangenome/{stem}"
    return {
        "gbz": f"pangenome/{graph}",
        "dist": f"{base}.dist",
        "ri": f"{base}.ri",
        "hapl": f"{base}.hapl",
        "paths": f"{base}.GRCh38.paths.txt",
    }


def vg_index_commands(keys, threads):
    """{step: command} building each graph index under a temporary name, renamed when complete."""
    d = {k: f"/data/{v}" for k, v in keys.items()}
    return {
        "dist": f"vg index -t {threads} -j {d['dist']}.part {d['gbz']} && mv {d['dist']}.part {d['dist']}",
        "ri": f"vg gbwt -p --num-threads {threads} -r {d['ri']}.part -Z {d['gbz']} && mv {d['ri']}.part {d['ri']}",
        "hapl": (
            f"vg haplotypes -v 1 -t {threads} -d {d['dist']} -r {d['ri']} -H {d['hapl']}.part {d['gbz']} "
            f"&& mv {d['hapl']}.part {d['hapl']}"
        ),
        "paths": f"vg paths -x {d['gbz']} -L -Q GRCh38 > {d['paths']}.part && mv {d['paths']}.part {d['paths']}",
    }


def kmc_command(read_keys, kff_key, threads, memory_gb=8):
    """kmc counting both read files into <kff_key> (KFF), in a scratch folder inside the container."""
    prefix = f"/data/{kff_key[:-4]}"  # kmc adds .kff
    listing = "\\n".join(f"/data/{k}" for k in read_keys)
    script = (
        f"set -e; mkdir -p /tmp/kmc; printf '{listing}\\n' > /tmp/reads.txt; "
        f"kmc -k{KMER_LENGTH} -m{memory_gb} -okff -t{threads} @/tmp/reads.txt {prefix}.part /tmp/kmc; "
        f"mv {prefix}.part.kff {prefix}.kff"
    )
    return ["bash", "-c", script]


def giraffe_command(sid, read_keys, keys, kff_key, unsorted_key, threads):
    """vg giraffe as in Google's case study: haplotype sampling, surjection to the GRCh38 paths, unsorted BAM."""
    d = {k: f"/data/{v}" for k, v in keys.items()}
    work = f"/data/pangenome/giraffe_{sid}"
    script = (
        f"set -e -o pipefail; mkdir -p {work}; cd {work}; "
        f"vg giraffe --progress --read-group 'ID:{sid} LB:lib1 SM:{sid} PL:illumina PU:unit1' --sample {sid} "
        f"-o BAM --ref-paths {d['paths']} -P -L 3000 "
        f"-f /data/{read_keys[0]} -f /data/{read_keys[1]} -Z {d['gbz']} "
        f"--kff-name /data/{kff_key} --haplotype-name {d['hapl']} --index-basename {work}/sampled "
        f"-t {threads} > /data/{unsorted_key}.part; mv /data/{unsorted_key}.part /data/{unsorted_key}"
    )
    return ["bash", "-c", script]


def postprocess_command(unsorted_key, bam_key, threads):
    """GRCh38#0#chr20 -> chr20, then the align stage's fixmate | sort | markdup (its sort settings, see SORT_*).

    The rename is Google's documented step (samtools view -h | sed over the whole SAM). Only the header holds contig
    names, so `samtools reheader` would be faster; it is kept as published so the timing matches the case study.
    """
    tmp = "/tmp/pp"
    script = (
        f"set -e -o pipefail; mkdir -p {tmp}; samtools view -h /data/{unsorted_key} | sed -e 's/GRCh38#0#//g' | "
        f"samtools fixmate -m -u - - | "
        f"samtools sort -u -@ {SORT_THREADS} -m {SORT_MEMORY} -T {tmp}/sort - | "
        f"samtools markdup -s -@ {threads} -T {tmp}/markdup - /data/{bam_key}.part; "
        f"mv /data/{bam_key}.part /data/{bam_key}"
    )
    return ["bash", "-c", script]


def giraffe_mapping_seconds(text):
    """Seconds giraffe reports for mapping alone ('Mapped N reads across T threads in S seconds'), if printed."""
    import re

    m = re.search(r"Mapped \d+ reads across \d+ threads in ([\d.]+) seconds", text)
    return float(m.group(1)) if m else None


def run_step(name, cmd, root, image, out_dir, results, outputs=(), extra_env=None, force=False):
    """Run a measured container step once; its measurement is saved as <out_dir>/<name>.json.

    A saved step is reused only while all its outputs (keys under root) still exist; force reruns it anyway.
    """
    jpath, log = os.path.join(out_dir, f"{name}.json"), os.path.join(out_dir, f"{name}.log")
    saved = ba.load_measured(jpath, log)
    if saved is not None and not force and all(os.path.exists(ba.path_of(root, k)) for k in outputs):
        results[name] = saved
        return True
    print(f">>> {name}", flush=True)
    rc, wall, m = ba.run_measured(cmd, root, image, log, extra_env)
    if rc != 0:
        print(f"{name} failed (exit {rc}); see {log}")
        return False
    with open(log, encoding="utf-8", errors="replace") as fh:
        mapping = giraffe_mapping_seconds(fh.read())
    results[name] = {"wall_s": wall, **m, **({"mapping_s": mapping} if mapping else {})}
    ba.save_json(jpath, results[name])
    print(f"    {wall:.0f} s, peak {m.get('peak_anon_bytes', 0) / ba.MIB:,.0f} MiB", flush=True)
    return True


def giraffe_cost(costs):
    """Per-sample alignment cost of the giraffe path, and the one-off graph index cost, as CSV columns.

    align_*: giraffe (incl. haplotype sampling and the sampled graph's indexes) + post-processing, which is the
    like-for-like counterpart of the align stage. kmc_*: the per-sample k-mer count giraffe needs first.
    graph_index_wall_s: dist + r-index + hapl + path list, built once per graph and reused for every sample.
    """
    step = {name: costs.get(name) or {} for name in ("giraffe", "postprocess", "kmc")}
    peak = max(step["giraffe"].get("peak_anon_bytes", 0), step["postprocess"].get("peak_anon_bytes", 0))
    return {
        "align_wall_s": round(step["giraffe"].get("wall_s", 0) + step["postprocess"].get("wall_s", 0), 1),
        "align_peak_mib": round(peak / ba.MIB) if peak else "",
        "giraffe_mapping_s": step["giraffe"].get("mapping_s", ""),
        "kmc_wall_s": round(step["kmc"]["wall_s"], 1) if "wall_s" in step["kmc"] else "",
        "kmc_peak_mib": round(step["kmc"]["peak_anon_bytes"] / ba.MIB) if step["kmc"].get("peak_anon_bytes") else "",
        "graph_index_wall_s": graph_index_seconds(costs),
    }


def graph_index_seconds(costs):
    """Total measured graph index time, or '' when no index step was measured (built elsewhere)."""
    walls = [(costs.get(f"index_{n}") or {}).get("wall_s") for n in ("dist", "ri", "hapl", "paths")]
    walls = [w for w in walls if w is not None]
    return round(sum(walls), 1) if walls else ""


def bwa_mem2_cost(measurement):
    """The bwa-mem2 align stage's cost (benchmark_aligners.py), in the same columns."""
    if not measurement:
        return {"align_wall_s": "", "align_peak_mib": ""}
    peak = measurement.get("peak_anon_bytes")
    return {
        "align_wall_s": round(measurement.get("wall_s", 0), 1),
        "align_peak_mib": round(peak / ba.MIB) if peak else "",
    }


def caller_settings(caller, env, dv_memory, pg_shards, pg_memory):
    """(env, memory) for one configuration: pangenome runs get the chosen make_examples worker count and memory cap.

    Shard count changes speed and memory only, not the calls (DeepVariant shards by region). The chr20 graph is
    small (72 MB), so more workers than the whole-genome graph allows (~10.7 GB shared memory + ~2 GB each) fit.
    """
    if caller != "pangenome":
        return env, dv_memory
    return {**env, "DEEPVARIANT_SHARDS": str(pg_shards)}, pg_memory or dv_memory


def file_identity(path):
    """(size, mtime) of a file, to tell when an input was replaced since an output was made from it."""
    st = os.stat(path)
    return [st.st_size, st.st_mtime_ns]


def call_settings(caller, env):
    """What decides a caller's output: the caller and its DeepVariant flags (shard count only changes speed)."""
    return {"caller": caller, **{k: v for k, v in sorted(env.items()) if k != "DEEPVARIANT_SHARDS"}}


def remove_caller_outputs(root, sid, caller):
    """Delete one caller's VCF and hap.py files for a sample (other callers' results stay)."""
    vcf = run_local.vcf_path(root, caller, sid)
    if os.path.exists(vcf):
        os.remove(vcf)
    happy_dir = ba.path_of(root, f"benchmarks/{sid}")
    if os.path.isdir(happy_dir):
        for name in os.listdir(happy_dir):
            if name.startswith(f"{caller}."):
                os.remove(os.path.join(happy_dir, name))


def score_config(
    root,
    out_dir,
    cpath,
    label,
    sid,
    caller,
    env,
    dv_memory,
    reference,
    base,
    region=None,
    stratification=None,
    subsets=("*",),
):
    """Calls and hap.py for one configuration, each redone only when missing or stale. Returns
    (summary, extended, calls info) or None if a step failed.

    Calls are redone when the alignment file or the caller settings differ from those recorded in cpath. A VCF with
    no record is reused and reported as calls_provenance 'unrecorded' ('legacy' for records from before settings were
    kept). hap.py is redone when the VCF, region or stratification differ from its own record.
    """
    aln = ba.alignment_path(root, sid)
    vcf = run_local.vcf_path(root, caller, sid)
    saved = ba.load_json(cpath)
    want = {"settings": call_settings(caller, env), "alignment": file_identity(aln)}
    stale = saved is not None and "settings" in saved and {k: saved.get(k) for k in want} != want
    if not os.path.exists(vcf) or stale:
        print(f">>> calls: {label}" + (" (alignment or settings changed: calling again)" if stale else ""), flush=True)
        remove_caller_outputs(root, sid, caller)
        started = time.monotonic()
        if run_caller(root, sid, reference, caller, env, dv_memory) != 0:
            return None
        saved = {
            "calls_wall_s": round(time.monotonic() - started, 1),
            "calls_shards": env.get("DEEPVARIANT_SHARDS", "default"),
            **want,
        }
        ba.save_json(cpath, saved)
    provenance = "unrecorded" if saved is None else ("recorded" if "settings" in saved else "legacy")
    if sid != base:
        ba.ensure_truth(root, base, sid)
    prefix = ba.path_of(root, f"benchmarks/{sid}/{caller}")
    hpath = os.path.join(out_dir, f"happy_{sid}_{caller}.json")
    hwant = {"vcf": file_identity(vcf), "region": region, "stratification": stratification}
    extended_csv = prefix + ".extended.csv"
    if (
        not os.path.exists(extended_csv)
        or ba.load_json(hpath) != hwant
        or (stratification and not benchmark_happy.has_subsets(extended_csv, subsets))
    ):
        args = ["--root", root, "--sample", sid, "--reference", reference, "--callers", caller]
        args += ["--region", region] if region else []
        args += ["--stratification", stratification] if stratification else []
        if benchmark_happy.main(args) != 0:
            return None
        ba.save_json(hpath, hwant)
    info = {"calls_provenance": provenance}
    info.update({k: saved[k] for k in ("calls_wall_s", "calls_shards") if saved and k in saved})
    return (
        benchmark_happy.read_summary(prefix + ".summary.csv"),
        benchmark_happy.read_extended(extended_csv, subsets),
        info,
    )


def link_or_copy(src, dest):
    """Hard-link (or copy) src to dest, replacing dest if it no longer matches src (src was regenerated)."""
    if os.path.exists(dest):
        s, d = os.stat(src), os.stat(dest)
        if (s.st_size, s.st_mtime_ns) == (d.st_size, d.st_mtime_ns):
            return
        os.remove(dest)
    try:
        os.link(src, dest)
    except OSError:
        shutil.copy2(src, dest)


def remove_downstream(root, sid, callers=("deepvariant", "pangenome")):
    """Delete a sample's VCFs and hap.py folder: they are stale once its alignment is regenerated."""
    for caller in callers:
        path = run_local.vcf_path(root, caller, sid)
        if os.path.exists(path):
            os.remove(path)
    shutil.rmtree(ba.path_of(root, f"benchmarks/{sid}"), ignore_errors=True)


def run_caller(root, sid, reference, caller, env, dv_memory):
    """run_local.py --skip-to-variants for one caller, with DeepVariant settings passed through the environment."""
    saved = {k: os.environ.get(k) for k in run_local.DEEPVARIANT_ENV_VARS}
    try:
        for k in run_local.DEEPVARIANT_ENV_VARS:
            os.environ.pop(k, None)
        os.environ.update(env)
        return run_local.main(
            [
                "--root",
                root,
                "--sample",
                sid,
                "--reference",
                reference,
                "--caller",
                caller,
                "--skip-to-variants",
                "--dv-memory",
                dv_memory,
            ]
        )
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def result_row(label, aligner, sid, caller, summary, extended, align_cost):
    """One CSV row: configuration, alignment cost, hap.py totals and per-region SNP/indel errors."""
    row = {"configuration": label, "aligner": aligner, "caller": caller, "sample": sid, **align_cost}
    for kind in ("SNP", "INDEL"):
        r = summary.get(kind, {})
        k = kind.lower()
        row.update({f"{k}_fn": r.get("fn", ""), f"{k}_fp": r.get("fp", ""), f"{k}_f1": r.get("f1", "")})
    for region in REGIONS[1:]:
        for kind in ("SNP", "INDEL"):
            r = extended.get((region, kind), {})
            row[f"{region}_{kind.lower()}_errors"] = r["fn"] + r["fp"] if r else ""
    return row


def summary_markdown(base, rows, costs):
    lines = [
        f"# Graph vs linear alignment: {base}",
        "",
        "| Configuration | SNP FN / FP | SNP F1 | Indel FN / FP | Indel F1 | SNP errors in segdups | "
        "SNP errors, low mappability |",
        "| :--- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in rows:
        f1 = {k: f"{r[k]:.6f}" if isinstance(r[k], float) else "" for k in ("snp_f1", "indel_f1")}
        lines.append(
            f"| {r['configuration']} | {r['snp_fn']} / {r['snp_fp']} | {f1['snp_f1']} | "
            f"{r['indel_fn']} / {r['indel_fp']} | {f1['indel_f1']} | {r['segdups_snp_errors']} | "
            f"{r['lowmappabilityall_snp_errors']} |"
        )
    if costs:
        lines += ["", "| Step | Wall (s) | Peak memory (MiB) |", "| :--- | ---: | ---: |"]
        for name, m in costs.items():
            peak = round(m["peak_anon_bytes"] / ba.MIB) if m.get("peak_anon_bytes") else ""
            lines.append(f"| {name} | {m.get('wall_s', 0):.0f} | {peak} |")
    return "\n".join(lines) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default=os.path.join(run_local.REPO_ROOT, "local_bucket"))
    p.add_argument("--base", required=True, help="truth/<base>.*; samples are <base>_giraffe, <base>_bwamem2")
    p.add_argument("--reference", required=True)
    p.add_argument("--graph", default=run_local.DEFAULT_PANGENOME, help="GBZ under pangenome/")
    p.add_argument("--reads", help="paired reads sample under raw_reads/ (default <base>_bwa)")
    p.add_argument("--region", help="hap.py -l, e.g. chr20")
    p.add_argument("--stratification", help="hap.py stratification TSV key (benchmark_happy --stratification)")
    p.add_argument("--with-mapq5", action="store_true", help="also pangenome DV with min_mapping_quality=5 on both")
    p.add_argument("--threads", type=int, default=os.cpu_count() or 16)
    p.add_argument(
        "--pg-shards",
        type=int,
        default=6,
        help="make_examples workers for pangenome DV (6 = earlier benchmarks; the chr20 graph fits more)",
    )
    p.add_argument("--pg-memory", help="memory cap for pangenome DV (default: each configuration's own, 14g)")
    p.add_argument("--image", default="geyser-app")
    a = p.parse_args(argv)

    root = os.path.abspath(a.root)
    out_dir = ba.path_of(root, f"benchmarks/giraffe_{a.base}")
    os.makedirs(out_dir, exist_ok=True)
    reads_sample = a.reads or f"{a.base}_bwa"
    reads = ba.raw_keys(reads_sample)
    for key in reads:
        if not os.path.exists(ba.path_of(root, key)):
            p.error(f"no {key}: run benchmark_aligners.py first (or pass --reads)")
    gsid, msid = f"{a.base}_giraffe", f"{a.base}_bwamem2"
    msid_alignment = ba.alignment_path(root, msid)
    if msid_alignment is None:
        p.error(f"no alignments/{msid}.cram or .bam: run benchmark_aligners.py first")
    keys = graph_keys(a.graph)
    costs = {}

    # 1. graph indexes (an index built elsewhere, with no saved measurement, is used as is: its cost stays blank)
    for name, script in vg_index_commands(keys, a.threads).items():
        built_elsewhere = os.path.exists(ba.path_of(root, keys[name])) and not os.path.exists(
            os.path.join(out_dir, f"index_{name}.json")
        )
        if not built_elsewhere and not run_step(
            f"index_{name}", ["bash", "-c", script], root, VG_IMAGE, out_dir, costs, outputs=[keys[name]]
        ):
            return 1
    # 2. kmers, 3. giraffe + post-processing (each reruns if its output is gone, whatever its saved timing says)
    kff = f"pangenome/kmc/{reads_sample}.kff"
    os.makedirs(os.path.dirname(ba.path_of(root, kff)), exist_ok=True)
    if not run_step("kmc", kmc_command(reads, kff, a.threads), root, KMC_IMAGE, out_dir, costs, outputs=[kff]):
        return 1
    unsorted = f"alignments/{gsid}.unsorted.bam"
    bam = f"alignments/{gsid}.bam"
    if os.path.exists(ba.path_of(root, bam)):
        for name in ("giraffe", "postprocess"):
            jpath = os.path.join(out_dir, f"{name}.json")
            costs[name] = ba.load_measured(jpath, jpath[:-5] + ".log") or {}
    else:
        if not os.path.exists(ba.path_of(root, unsorted)) and not run_step(
            "giraffe",
            giraffe_command(gsid, reads, keys, kff, unsorted, a.threads),
            root,
            VG_IMAGE,
            out_dir,
            costs,
            force=True,
        ):
            return 1
        costs.setdefault("giraffe", ba.load_measured(os.path.join(out_dir, "giraffe.json"), "") or {})
        # A new giraffe BAM makes every call and score made from the old one stale, including _mq5 links.
        for _, aligner, suffix, _, _, _ in CONFIGS + MAPQ5_CONFIGS:
            if aligner == "giraffe":
                remove_downstream(root, f"{a.base}_{suffix}")
                if suffix != "giraffe" and os.path.exists(ba.path_of(root, f"alignments/{a.base}_{suffix}.bam")):
                    os.remove(ba.path_of(root, f"alignments/{a.base}_{suffix}.bam"))
        if not run_step(
            "postprocess",
            postprocess_command(unsorted, bam, a.threads),
            root,
            a.image,
            out_dir,
            costs,
            force=True,
        ):
            return 1
        os.remove(ba.path_of(root, unsorted))
    bwamem2 = ba.load_json(ba.path_of(root, f"benchmarks/aligners_{a.base}/align_bwamem2.json")) or {}

    # 4. calls, 5. hap.py
    configs = CONFIGS + (MAPQ5_CONFIGS if a.with_mapq5 else [])
    subsets = ["*"] + (
        benchmark_happy.read_stratification_names(ba.path_of(root, a.stratification)) if a.stratification else []
    )
    rows = []
    for label, aligner, suffix, caller, env, dv_memory in configs:
        sid = f"{a.base}_{suffix}"
        source_sid, source = (gsid, ba.path_of(root, bam)) if aligner == "giraffe" else (msid, msid_alignment)
        if sid != source_sid:  # an _mq5 configuration: the same alignment under its own sample name
            link_or_copy(source, ba.path_of(root, f"alignments/{sid}.{source.rsplit('.', 1)[-1]}"))
        env, dv_memory = caller_settings(caller, env, dv_memory, a.pg_shards, a.pg_memory)
        cpath = os.path.join(out_dir, f"calls_{suffix}_{caller}.json")
        scored = score_config(
            root,
            out_dir,
            cpath,
            label,
            sid,
            caller,
            env,
            dv_memory,
            a.reference,
            a.base,
            a.region,
            a.stratification,
            subsets,
        )
        if scored is None:
            return 1
        summary, extended, info = scored
        cost = giraffe_cost(costs) if aligner == "giraffe" else bwa_mem2_cost(bwamem2)
        cost.update(info)
        rows.append(result_row(label, aligner, sid, caller, summary, extended, cost))

    csv_path = ba.path_of(root, f"benchmarks/giraffe_{a.base}.csv")
    ba.write_csv(csv_path, rows)
    text = summary_markdown(a.base, rows, costs)
    with open(os.path.join(out_dir, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"\n{text}\nWritten to {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
