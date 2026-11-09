#!/usr/bin/env python3
"""DeepVariant's speed over call_variants batch size x make_examples shards, on a local GPU (#139).

Runs the real DeepVariant stage (run_local.py --caller deepvariant --skip-to-variants) once per cell of the grid
on the same alignment, strictly one container at a time, and collects per cell: the stage's wall time, each
step's time, the examples sent to call_variants, GPU utilisation and memory (the stage's own nvidia-smi sampling,
#186), and the calls (PASS counts from the stage's bcftools stats, plus a digest of the VCF's records, so a
setting that changed the calls would show). Batch size and shards should change speed only.

    python scripts/fetch_dv_testdata.py --dataset hg003-chr20
    python scripts/benchmark_dv_grid.py --root <bucket> --sample HG003_chr20 --reference GRCh38_chr20.fa \\
        --batch-sizes 128,256,512,1024 --shards 16,8,4 --dv-image geyser-deepvariant --dv-memory 12g \\
        --out <results>

Each cell's files go to <out>/b<batch>-s<shards>[-r<n>]/ (run.log, the stage's run-record part, its bcftools
stats, cell.json); a cell whose cell.json exists is skipped, so an interrupted grid resumes. The table of all
cells is <out>/dv-grid.csv (scripts/figures/dv_batch_shards.py draws it).

Before each cell the host must have --min-free-gb free and no other container running except light ones
(--allow-container, default: Step Functions Local); otherwise the grid stops with exit code 3 and resumes when
rerun. A cell that fails (e.g. a batch too big for the GPU's memory: DeepVariant's fatal-error watch stops it)
is recorded as failed and the grid goes on.
"""

import argparse
import csv
import gzip
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_LOCAL = os.path.join(REPO_ROOT, "scripts", "run_local.py")
STEPS = ("make_examples", "call_variants", "postprocess_variants")
STEP_LINE = re.compile(r"^\s+(make_examples|call_variants|postprocess_variants)\s+([0-9.]+)s\s*$")
EXAMPLES_LINE = re.compile(
    r"examples sent to call_variants: (\d+|None); called by the small model in make_examples \(CPU\): (\d+|None)"
)
STATS_SN = re.compile(r"^SN\t0\tnumber of (SNPs|indels|records):\t(\d+)$")
LIGHT_CONTAINERS = ("stepfunctions-local", "sfn-local")
CSV_FIELDS = [
    "cell",
    "batch_size",
    "shards",
    "repeat",
    "status",
    "error",
    "stage_s",
    "make_examples_s",
    "call_variants_s",
    "postprocess_variants_s",
    "examples",
    "small_model_calls",
    "gpu_model",
    "gpu_util_mean_pct",
    "gpu_util_peak_pct",
    "gpu_memory_peak_mib",
    "call_variants_gpu_util_mean_pct",
    "call_variants_gpu_memory_peak_mib",
    "cpu_seconds",
    "pass_snps",
    "pass_indels",
    "pass_records",
    "records_md5",
]


def int_list(text):
    values = [int(v) for v in text.split(",") if v.strip()]
    if not values or any(v < 1 for v in values):
        raise argparse.ArgumentTypeError(f"expected positive integers separated by commas, got {text!r}")
    return values


def cell_name(batch, shards, repeat=1):
    return f"b{batch}-s{shards}" + (f"-r{repeat}" if repeat > 1 else "")


def plan_cells(batch_sizes, shards, repeats=()):
    """The grid in run order: shards outer (make_examples dominates), batch sizes inner, then the repeats.

    repeats: (batch, shards) cells to run a second time, to show the run-to-run noise.
    """
    cells = [(b, s, 1) for s in shards for b in batch_sizes]
    seen = {}
    for b, s in repeats:
        seen[(b, s)] = seen.get((b, s), 1) + 1
        cells.append((b, s, seen[(b, s)]))
    return cells


def parse_repeat(text):
    batch, _, shards = text.partition("x")
    return int(batch), int(shards)


def cell_env(batch, shards, sample_seconds, timeout):
    """The DeepVariant settings for one cell (run_local.py passes DEEPVARIANT_* into the container)."""
    return {
        "DEEPVARIANT_SHARDS": str(shards),
        "DEEPVARIANT_EXTRA_ARGS": f"--call_variants_extra_args=batch_size={batch}",
        "DEEPVARIANT_CHECKPOINT": "0",  # every cell runs every step; no resume from a failed cell
        "DEEPVARIANT_GPU_SAMPLE_SECONDS": str(sample_seconds),
        "DEEPVARIANT_TIMEOUT_SECONDS": str(timeout),
    }


def parse_log(text):
    """Step times and example counts from the stage's output (deepvariant_task.py prints both)."""
    out = {f"{step}_s": None for step in STEPS}
    out.update(examples=None, small_model_calls=None)
    for line in text.splitlines():
        m = STEP_LINE.match(line)
        if m:
            out[f"{m.group(1)}_s"] = float(m.group(2))
        m = EXAMPLES_LINE.search(line)
        if m:
            out["examples"] = None if m.group(1) == "None" else int(m.group(1))
            out["small_model_calls"] = None if m.group(2) == "None" else int(m.group(2))
    return out


def parse_part(part):
    """Status, stage time, CPU time and GPU figures from the stage's run-record part (DeepVariant.json)."""
    gpu = part.get("gpu") or {}
    cv = (gpu.get("steps") or {}).get("call_variants") or {}
    resources = part.get("resources") or {}
    return {
        "status": part.get("status"),
        "error": part.get("error"),
        "stage_s": part.get("duration_seconds"),
        "gpu_model": gpu.get("model"),
        "gpu_util_mean_pct": gpu.get("utilization_mean_pct"),
        "gpu_util_peak_pct": gpu.get("utilization_peak_pct"),
        "gpu_memory_peak_mib": gpu.get("memory_peak_mib"),
        "call_variants_gpu_util_mean_pct": cv.get("utilization_mean_pct"),
        "call_variants_gpu_memory_peak_mib": cv.get("memory_peak_mib"),
        "cpu_seconds": resources.get("cpu_seconds"),
    }


def parse_stats(text):
    """PASS counts from `bcftools stats -f PASS` (the stage's bcftools.stats.deepvariant.txt)."""
    names = {"SNPs": "pass_snps", "indels": "pass_indels", "records": "pass_records"}
    out = dict.fromkeys(names.values())
    for line in text.splitlines():
        m = STATS_SN.match(line)
        if m:
            out[names[m.group(1)]] = int(m.group(2))
    return out


def records_md5(vcf_path):
    """Digest of a VCF's records (header excluded: it can carry dates and command lines)."""
    digest = hashlib.md5(usedforsecurity=False)
    with gzip.open(vcf_path, "rb") as fh:
        for line in fh:
            if not line.startswith(b"#"):
                digest.update(line)
    return digest.hexdigest()


def free_memory_gb():
    """Free physical memory of the host, in GB (Windows: what Docker Desktop's VM can still get)."""
    if platform.system() == "Windows":
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return int(out.strip()) / (1024 * 1024)
    with open("/proc/meminfo") as fh:
        info = dict(line.split(":", 1) for line in fh)
    return int(info["MemAvailable"].split()[0]) / (1024 * 1024)


def other_containers(allowed=LIGHT_CONTAINERS):
    """Running containers other than the light ones allowed alongside the grid (name or image matches)."""
    out = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}\t{{.Image}}"], capture_output=True, text=True, check=True
    ).stdout
    busy = []
    for line in out.splitlines():
        if line.strip() and not any(a in line for a in allowed):
            busy.append(line.replace("\t", " "))
    return busy


def blocked(min_free_gb, allowed):
    """Why the next cell can't start now, or None."""
    busy = other_containers(allowed)
    if busy:
        return f"other containers running: {', '.join(busy)}"
    free = free_memory_gb()
    if free < min_free_gb:
        return f"only {free:.1f} GB free (need {min_free_gb} GB)"
    return None


def outputs(root, sample):
    """The stage's outputs a cell reads (and clears first, so a failed cell can't pick up the previous one's)."""
    return {
        "vcf": os.path.join(root, "variants_deepvariant", f"{sample}.vcf.gz"),
        "stats": os.path.join(root, "stats", sample, "bcftools.stats.deepvariant.txt"),
        "part": os.path.join(root, "runs", f"{sample}.provenance", "DeepVariant.json"),
    }


def run_cell(a, batch, shards, repeat):
    name = cell_name(batch, shards, repeat)
    cell_dir = os.path.join(a.out, name)
    os.makedirs(cell_dir, exist_ok=True)
    files = outputs(a.root, a.sample)
    for path in files.values():
        if os.path.exists(path):
            os.remove(path)
    cmd = [
        sys.executable,
        RUN_LOCAL,
        "--root",
        a.root,
        "--sample",
        a.sample,
        "--reference",
        a.reference,
        "--caller",
        "deepvariant",
        "--skip-to-variants",
        "--no-gate",
        "--dv-image",
        a.dv_image,
        "--dv-memory",
        a.dv_memory,
    ]
    env = {**os.environ, **cell_env(batch, shards, a.gpu_sample_seconds, a.timeout)}
    print(f"[{time.strftime('%H:%M:%S')}] {name}: batch_size={batch}, shards={shards}", flush=True)
    start = time.time()
    with open(os.path.join(cell_dir, "run.log"), "w", encoding="utf-8") as log:
        code = subprocess.run(cmd, env=env, stdout=log, stderr=subprocess.STDOUT, check=False).returncode
    wall = time.time() - start
    with open(os.path.join(cell_dir, "run.log"), encoding="utf-8", errors="replace") as fh:
        row = dict.fromkeys(CSV_FIELDS)  # every field present, even for a cell that failed early
        row.update(cell=name, batch_size=batch, shards=shards, repeat=repeat, **parse_log(fh.read()))
    part = {}
    if os.path.exists(files["part"]):
        shutil.copy(files["part"], os.path.join(cell_dir, "DeepVariant.json"))
        with open(files["part"], encoding="utf-8") as fh:
            part = json.load(fh)
    row.update(parse_part(part))
    if os.path.exists(files["stats"]):
        shutil.copy(files["stats"], os.path.join(cell_dir, "bcftools.stats.deepvariant.txt"))
        with open(files["stats"], encoding="utf-8") as fh:
            row.update(parse_stats(fh.read()))
    if code == 0 and os.path.exists(files["vcf"]):
        row["records_md5"] = records_md5(files["vcf"])
    if code != 0 or row.get("status") != "Success":
        row["status"] = "Failure"
        row["error"] = row.get("error") or f"run_local.py exited with code {code}"
    row["host_wall_s"] = round(wall, 1)
    with open(os.path.join(cell_dir, "cell.json"), "w", encoding="utf-8") as fh:
        json.dump(row, fh, indent=2)
    print(
        f"[{time.strftime('%H:%M:%S')}] {name}: {row['status']} in {wall:.0f} s "
        f"(make_examples {row['make_examples_s']}, call_variants {row['call_variants_s']})",
        flush=True,
    )
    return row


def write_table(out, cells):
    rows = []
    for batch, shards, repeat in cells:
        path = os.path.join(out, cell_name(batch, shards, repeat), "cell.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                rows.append(json.load(fh))
    with open(os.path.join(out, "dv-grid.csv"), "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return rows


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--root", required=True, help="local storage root with alignments/<sample> and reference/")
    p.add_argument("--sample", required=True)
    p.add_argument("--reference", required=True)
    p.add_argument("--batch-sizes", type=int_list, default=[128, 256, 512, 1024])
    p.add_argument("--shards", type=int_list, default=[16, 8, 4])
    p.add_argument(
        "--repeat",
        type=parse_repeat,
        action="append",
        default=[],
        metavar="BATCHxSHARDS",
        help="run this cell once more (run-to-run noise), e.g. 256x16; may be given more than once",
    )
    p.add_argument("--dv-image", default="geyser-deepvariant")
    p.add_argument("--dv-memory", default="12g")
    p.add_argument("--gpu-sample-seconds", type=float, default=1)
    p.add_argument("--timeout", type=int, default=3600, help="DEEPVARIANT_TIMEOUT_SECONDS per cell")
    p.add_argument("--min-free-gb", type=float, default=8)
    p.add_argument(
        "--allow-container",
        action="append",
        default=list(LIGHT_CONTAINERS),
        help="a container name/image fragment that may run alongside (light test containers)",
    )
    p.add_argument("--out", required=True)
    a = p.parse_args(argv)
    a.root, a.out = os.path.abspath(a.root), os.path.abspath(a.out)
    os.makedirs(a.out, exist_ok=True)
    cells = plan_cells(a.batch_sizes, a.shards, a.repeat)
    for batch, shards, repeat in cells:
        if os.path.exists(os.path.join(a.out, cell_name(batch, shards, repeat), "cell.json")):
            continue
        reason = blocked(a.min_free_gb, a.allow_container)
        if reason:
            print(f"Stopping before {cell_name(batch, shards, repeat)}: {reason}. Rerun to resume.", flush=True)
            write_table(a.out, cells)
            return 3
        run_cell(a, batch, shards, repeat)
        write_table(a.out, cells)
    rows = write_table(a.out, cells)
    digests = {r["records_md5"] for r in rows if r.get("records_md5")}
    print(
        f"{len(rows)} cells; {sum(r['status'] == 'Success' for r in rows)} succeeded; "
        f"{len(digests)} distinct call set(s)",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
