#!/usr/bin/env python3
"""Benchmark the align stage's two aligners, bwa and bwa-mem2 (#88), on real reads: time, memory and calls.

The align stage runs bwa-mem2 by default and bwa with ALIGNER=bwa (app/tasks.py). This script runs the real stage
(`python tasks.py align` in the geyser-app image) once per aligner on the same reads, measures it, then calls
variants on both BAMs and scores them with hap.py, to show what bwa-mem2 buys and that the calls are equivalent.

    python scripts/fetch_dv_testdata.py --dataset hg003-chr20
    python scripts/benchmark_aligners.py --root <root> --from-bam alignments/HG003_chr20.bam \\
        --base HG003_chr20 --reference GRCh38_chr20.fa --region chr20

Steps (each is skipped when its output already exists, so an interrupted run resumes):
  1. reads    paired FASTQs from an existing BAM (--from-bam; samtools collate | fastq, primary records only,
              pairs whose mate is not in the BAM dropped) to raw_reads/<base>_<aligner>_R1/_R2.fastq.gz.
  2. index    each aligner's index built next to reference/<reference> (timed), so the align runs below fetch it
              instead of building it. bwa and bwa-mem2 write identical .amb/.ann/.pac files, so both coexist.
  3. align    the align stage per aligner, as sample <base>_bwa / <base>_bwamem2 (the source BAM is untouched).
              Measured: wall time of the container (host clock: includes copying inputs and the BAM), the
              aligner's own "[main] Real time", peak process memory (cgroup v2 'anon', sampled every second)
              and the kernel's memory.peak (also counts page cache, so it is an upper bound).
  4. calls    run_local.py --skip-to-variants with --caller (default both: bcftools + linear DeepVariant).
  5. hap.py   benchmark_happy.py per aligner sample, against truth/<base>.* (copied to each sample's name),
              plus a direct comparison of the two aligners' PASS calls per caller.
Outputs: benchmarks/aligners_<base>.csv (one row per aligner), benchmarks/aligners_<base>/summary.md, and the raw
measurements as benchmarks/aligners_<base>/<step>.json.
"""

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time

import benchmark_happy
import run_local

ALIGNERS = ("bwa", "bwa-mem2")
SAMPLE_SUFFIX = {"bwa": "bwa", "bwa-mem2": "bwamem2"}
# Index files each aligner builds next to the FASTA (same as ALIGNER_INDEX_EXTS in app/tasks.py).
INDEX_EXTS = {
    "bwa-mem2": [".0123", ".amb", ".ann", ".bwt.2bit.64", ".pac"],
    "bwa": [".amb", ".ann", ".bwt", ".pac", ".sa"],
}
CALLER_CHOICES = {"bcftools": ["bcftools"], "deepvariant": ["deepvariant"], "both": ["bcftools", "deepvariant"]}
MIB = 1024 * 1024

# Runs "$@" in the background and samples the container's cgroup v2 memory once a second. 'anon' is memory the
# processes hold (not page cache); memory.peak is the kernel's high-water mark and does include page cache.
MEASURE_SCRIPT = r"""
"$@" &
pid=$!
peak=0
while kill -0 "$pid" 2>/dev/null; do
  a=$(awk '/^anon /{print $2}' /sys/fs/cgroup/memory.stat 2>/dev/null)
  if [ -n "$a" ] && [ "$a" -gt "$peak" ]; then peak=$a; fi
  sleep 1
done
wait "$pid"
rc=$?
echo "GEYSER_PEAK_ANON_BYTES=$peak"
echo "GEYSER_CGROUP_PEAK_BYTES=$(cat /sys/fs/cgroup/memory.peak 2>/dev/null || echo 0)"
exit $rc
"""


def sample_id(base, aligner):
    return f"{base}_{SAMPLE_SUFFIX[aligner]}"


def raw_keys(sid):
    return [f"raw_reads/{sid}_R1.fastq.gz", f"raw_reads/{sid}_R2.fastq.gz"]


def path_of(root, key):
    return os.path.join(root, *key.split("/"))


def measured(cmd):
    """Wrap a container command so it prints its peak memory when it ends (see MEASURE_SCRIPT)."""
    return ["bash", "-c", MEASURE_SCRIPT, "measure"] + list(cmd)


def fastq_from_bam_command(bam_key, r1_key, r2_key, threads=4):
    """Container command: paired FASTQs from a BAM, written under temporary names and renamed when complete.

    collate groups mates; fastq -F 0x900 keeps primary records only (duplicate-flagged reads stay: they are real
    reads), -n keeps read names without /1 /2, and singletons (mate not in the BAM) go to /dev/null.
    """
    r1, r2 = f"/data/{r1_key}", f"/data/{r2_key}"
    t1, t2 = (os.path.dirname(p) + "/.partial-" + os.path.basename(p) for p in (r1, r2))
    script = (f"set -e -o pipefail; trap 'rm -f {t1} {t2}' ERR; cd /tmp; "
              f"samtools collate -u -O -@ {threads} /data/{bam_key} /tmp/collate | "
              f"samtools fastq -n -F 0x900 -@ {threads} -1 {t1} -2 {t2} -0 /dev/null -s /dev/null -; "
              f"mv {t1} {r1}; mv {t2} {r2}")
    return ["bash", "-c", script]


def index_command(aligner, reference_name):
    return ["bash", "-c", f"cd /data/reference && {aligner} index {reference_name}"]


def align_command(sid, reference_name):
    return ["python", "tasks.py", "align", sid, reference_name]


def parse_measurements(text):
    """Numbers from a measured run's output: peak memory, the aligner's own timer, markdup's read counts."""
    out = {}
    patterns = {
        "peak_anon_bytes": r"GEYSER_PEAK_ANON_BYTES=(\d+)",
        "cgroup_peak_bytes": r"GEYSER_CGROUP_PEAK_BYTES=(\d+)",
        "markdup_reads": r"^READ: (\d+)",
        "markdup_duplicates": r"^DUPLICATE TOTAL: (\d+)",
    }
    for name, pattern in patterns.items():
        m = re.search(pattern, text, re.M)
        if m:
            out[name] = int(m.group(1))
    # bwa (and bwa index) end with "[main] Real time: X sec; CPU: Y sec" (the last one is mem's). bwa-mem2 prints
    # no CPU total; its "Time taken for main_mem function" is the same span (index load to last record written).
    m = None
    for m in re.finditer(r"\[main\] Real time: ([\d.]+) sec; CPU: ([\d.]+) sec", text):
        pass
    if m:
        out["aligner_real_s"], out["aligner_cpu_s"] = float(m.group(1)), float(m.group(2))
    m = re.search(r"Time taken for main_mem function: ([\d.]+) sec", text)
    if m:
        out["aligner_real_s"] = float(m.group(1))
    m = re.search(r"Writing IO time \(SAM\) avg: ([\d.]+)", text)
    if m:
        out["aligner_write_s"] = float(m.group(1))
    return out


def load_measured(json_path, log_path):
    """A saved measurement, refreshed with anything parse_measurements finds in its log (so parser fixes apply to
    earlier runs without re-running them). None when there is no saved measurement."""
    data = load_json(json_path)
    if data is not None and os.path.exists(log_path):
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            data.update(parse_measurements(fh.read()))
    return data


def run_measured(cmd, root, image, log_path, extra_env=None):
    """Run a container command, teeing its output to log_path; returns (exit code, wall seconds, measurements)."""
    full = run_local.docker_command(measured(cmd), root, image, extra_env=extra_env)
    started = time.monotonic()
    lines = []
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace")
        for line in proc.stdout:
            log.write(line)
            lines.append(line)
        rc = proc.wait()
    wall = time.monotonic() - started
    return rc, wall, parse_measurements("".join(lines))


def tool_versions(image):
    """{'bwa': ..., 'bwa-mem2': ...} as the tools report them (bwa-mem2 2.3 reports 2.2.1, see the Dockerfile)."""
    script = "bwa 2>&1 | awk '/^Version/{print $2}'; bwa-mem2 version 2>/dev/null | tail -1"
    out = subprocess.run(["docker", "run", "--rm", image, "bash", "-c", script], capture_output=True, text=True)
    lines = out.stdout.split()
    return {"bwa": lines[0] if lines else "", "bwa-mem2": lines[1] if len(lines) > 1 else ""}


def concordance(root, caller, first, second):
    """(shared, only first, only second) PASS calls between two samples' VCFs for one caller."""
    a = run_local.read_vcf_calls(run_local.vcf_path(root, caller, first))
    b = run_local.read_vcf_calls(run_local.vcf_path(root, caller, second))
    return len(a & b), len(a - b), len(b - a)


def csv_rows(aligners, versions, align, index, happy, concord):
    """One row per aligner. align/index: {aligner: measurement dict}; happy: {aligner: {caller: read_summary()}};
    concord: {caller: (shared, only bwa, only bwa-mem2)}."""
    rows = []
    for aligner in aligners:
        a, i = align.get(aligner, {}), index.get(aligner, {})
        row = {
            "aligner": aligner,
            "version": versions.get(aligner, ""),
            "sample": a.get("sample", ""),
            "threads": a.get("threads", ""),
            "align_stage_wall_s": round(a["wall_s"], 1) if "wall_s" in a else "",
            "aligner_real_s": a.get("aligner_real_s", ""),
            "aligner_cpu_s": a.get("aligner_cpu_s", ""),
            "aligner_write_s": a.get("aligner_write_s", ""),
            "align_peak_anon_mib": round(a["peak_anon_bytes"] / MIB) if "peak_anon_bytes" in a else "",
            "align_cgroup_peak_mib": round(a["cgroup_peak_bytes"] / MIB) if "cgroup_peak_bytes" in a else "",
            "reads": a.get("markdup_reads", ""),
            "duplicates": a.get("markdup_duplicates", ""),
            "bam_bytes": a.get("bam_bytes", ""),
            "index_build_wall_s": round(i["wall_s"], 1) if "wall_s" in i else "",
            "index_peak_anon_mib": round(i["peak_anon_bytes"] / MIB) if "peak_anon_bytes" in i else "",
        }
        for caller, kinds in sorted(happy.get(aligner, {}).items()):
            for kind in ("SNP", "INDEL"):
                r = kinds.get(kind)
                if r:
                    prefix = f"{caller}_{kind.lower()}"
                    row.update({f"{prefix}_fn": r["fn"], f"{prefix}_fp": r["fp"], f"{prefix}_f1": r["f1"]})
        for caller, (shared, only_first, only_second) in sorted(concord.items()):
            row[f"{caller}_calls_shared"] = shared
            row[f"{caller}_calls_only_this"] = only_first if aligner == aligners[0] else only_second
        rows.append(row)
    return rows


def write_csv(path, rows):
    fields = []
    for row in rows:
        fields += [k for k in row if k not in fields]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def summary_markdown(base, rows, concord):
    lines = [f"# Aligner benchmark: {base}", "",
             "| Aligner | Version | Align stage (s) | Aligner real (s) | Aligner CPU (s) | Peak memory (MiB) | "
             "cgroup peak incl. cache (MiB) | Index build (s) |",
             "| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for r in rows:
        lines.append(f"| {r['aligner']} | {r['version']} | {r['align_stage_wall_s']} | {r['aligner_real_s']} | "
                     f"{r['aligner_cpu_s']} | {r['align_peak_anon_mib']} | {r['align_cgroup_peak_mib']} | "
                     f"{r['index_build_wall_s']} |")
    callers = sorted({k.split("_snp_f1")[0] for r in rows for k in r if k.endswith("_snp_f1")})
    if callers:
        lines += ["", "| Aligner | Caller | SNP FN / FP | SNP F1 | Indel FN / FP | Indel F1 |",
                  "| :--- | :--- | ---: | ---: | ---: | ---: |"]
        for r in rows:
            for c in callers:
                if f"{c}_snp_f1" in r:
                    lines.append(f"| {r['aligner']} | {c} | {r[f'{c}_snp_fn']} / {r[f'{c}_snp_fp']} | "
                                 f"{r[f'{c}_snp_f1']:.6f} | {r[f'{c}_indel_fn']} / {r[f'{c}_indel_fp']} | "
                                 f"{r[f'{c}_indel_f1']:.6f} |")
    if concord:
        lines += ["", "PASS calls compared directly (position, REF, ALT), whole contig:", "",
                  "| Caller | Shared | Only bwa | Only bwa-mem2 |", "| :--- | ---: | ---: | ---: |"]
        for c, (shared, a, b) in sorted(concord.items()):
            lines.append(f"| {c} | {shared:,} | {a:,} | {b:,} |")
    return "\n".join(lines) + "\n"


def load_json(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def ensure_truth(root, base, sid):
    """Copy truth/<base>.vcf.gz and .bed to the aligner sample's name (benchmark_happy looks them up by sample)."""
    for ext in (".vcf.gz", ".bed"):
        src, dest = path_of(root, f"truth/{base}{ext}"), path_of(root, f"truth/{sid}{ext}")
        if not os.path.exists(dest):
            shutil.copyfile(src, dest)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default=os.path.join(run_local.REPO_ROOT, "local_bucket"), help="local storage root")
    p.add_argument("--base", required=True, help="truth/<base>.* to score against; aligner samples are <base>_bwa...")
    p.add_argument("--reference", required=True, help="reference name under reference/")
    p.add_argument("--from-bam", metavar="KEY", help="make the paired FASTQs from this BAM (e.g. alignments/X.bam)")
    p.add_argument("--region", help="hap.py -l, e.g. chr20")
    p.add_argument("--caller", choices=sorted(CALLER_CHOICES), default="both")
    p.add_argument("--image", default="geyser-app")
    p.add_argument("--dv-image", default="geyser-deepvariant")
    p.add_argument("--dv-memory", default="12g")
    p.add_argument("--no-gpu", action="store_true")
    a = p.parse_args(argv)

    root = os.path.abspath(a.root)
    out_dir = path_of(root, f"benchmarks/aligners_{a.base}")
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(path_of(root, "raw_reads"), exist_ok=True)
    samples = {al: sample_id(a.base, al) for al in ALIGNERS}

    # 1. reads: once, then a copy (hard link where possible) per aligner sample
    first = raw_keys(samples[ALIGNERS[0]])
    if not all(os.path.exists(path_of(root, k)) for k in first):
        if not a.from_bam:
            p.error(f"no {first[0]}: pass --from-bam to make the reads")
        print(f">>> reads: {a.from_bam} -> {first[0]}, {first[1]}", flush=True)
        cmd = run_local.docker_command(fastq_from_bam_command(a.from_bam, *first), root, a.image)
        if subprocess.run(cmd).returncode != 0:
            return 1
    for al in ALIGNERS[1:]:
        for src, dest in zip(first, raw_keys(samples[al])):
            if not os.path.exists(path_of(root, dest)):
                try:
                    os.link(path_of(root, src), path_of(root, dest))
                except OSError:
                    shutil.copyfile(path_of(root, src), path_of(root, dest))

    # 2. index, 3. align: measured, results kept as JSON so a rerun only does what is missing
    index, align = {}, {}
    for al in ALIGNERS:
        ref = path_of(root, f"reference/{a.reference}")
        jpath = os.path.join(out_dir, f"index_{SAMPLE_SUFFIX[al]}.json")
        if all(os.path.exists(ref + ext) for ext in INDEX_EXTS[al]):
            index[al] = load_measured(jpath, jpath[:-5] + ".log") or {}
        else:
            print(f">>> index: {al}", flush=True)
            rc, wall, m = run_measured(index_command(al, a.reference), root, a.image,
                                       os.path.join(out_dir, f"index_{SAMPLE_SUFFIX[al]}.log"))
            if rc != 0:
                print(f"{al} index failed")
                return 1
            index[al] = {"wall_s": wall, **m}
            save_json(jpath, index[al])

    for al in ALIGNERS:
        sid = samples[al]
        jpath = os.path.join(out_dir, f"align_{SAMPLE_SUFFIX[al]}.json")
        bam = path_of(root, f"alignments/{sid}.bam")
        if load_json(jpath) and os.path.exists(bam):
            align[al] = load_measured(jpath, jpath[:-5] + ".log")
            continue
        print(f">>> align: {sid} with ALIGNER={al}", flush=True)
        rc, wall, m = run_measured(align_command(sid, a.reference), root, a.image,
                                   os.path.join(out_dir, f"align_{SAMPLE_SUFFIX[al]}.log"), {"ALIGNER": al})
        if rc != 0:
            print(f"align failed with {al}")
            return 1
        threads = subprocess.run(["docker", "run", "--rm", a.image, "nproc"], capture_output=True,
                                 text=True).stdout.strip()
        align[al] = {"sample": sid, "wall_s": wall, "threads": threads, "bam_bytes": os.path.getsize(bam), **m}
        save_json(jpath, align[al])
        print(f"    {wall:.0f} s, peak {m.get('peak_anon_bytes', 0) / MIB:,.0f} MiB", flush=True)

    # 4. calls, 5. hap.py
    callers = CALLER_CHOICES[a.caller]
    happy = {}
    for al in ALIGNERS:
        sid = samples[al]
        missing = [c for c in callers if not os.path.exists(run_local.vcf_path(root, c, sid))]
        if missing:
            caller_arg = "both" if len(missing) == 2 else missing[0]
            args = ["--root", root, "--sample", sid, "--reference", a.reference, "--caller", caller_arg,
                    "--skip-to-variants", "--image", a.image, "--dv-image", a.dv_image, "--dv-memory", a.dv_memory]
            if a.no_gpu:
                args.append("--no-gpu")
            if run_local.main(args) != 0:
                return 1
        ensure_truth(root, a.base, sid)
        summaries = {c: path_of(root, f"benchmarks/{sid}/{c}.summary.csv") for c in callers}
        if not all(os.path.exists(s) for s in summaries.values()):
            args = ["--root", root, "--sample", sid, "--reference", a.reference, "--callers", *callers]
            if a.region:
                args += ["--region", a.region]
            if benchmark_happy.main(args) != 0:
                return 1
        happy[al] = {c: benchmark_happy.read_summary(s) for c, s in summaries.items()}

    concord = {c: concordance(root, c, samples[ALIGNERS[0]], samples[ALIGNERS[1]]) for c in callers}
    rows = csv_rows(list(ALIGNERS), tool_versions(a.image), align, index, happy, concord)
    csv_path = path_of(root, f"benchmarks/aligners_{a.base}.csv")
    write_csv(csv_path, rows)
    text = summary_markdown(a.base, rows, concord)
    with open(os.path.join(out_dir, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"\n{text}\nWritten to {csv_path} and {out_dir}/summary.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
