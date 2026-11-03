"""Signature figure 3: per-base quality surface, mean base quality (Phred) by sequencing cycle, a
heightmap per read (R1 and R2 side by side), one row per sample.

Input: the fastp JSON reports the QC stage writes (qc_reports/{id}.fastp.json). fastp keeps per-cycle
*mean* quality (overall and per base A/C/G/T), not a per-cycle distribution, so the surface is
cycle x sample x mean Phred. Build the tidy CSV, then plot:

    python scripts/figures/quality_surface.py --from-fastp "HG003 35x=REAL-HG003-35x-chr20.fastp.json" ... \\
        --csv docs/figures/csv/quality-surface.csv
    python scripts/figures/quality_surface.py --csv docs/figures/csv/quality-surface.csv \\
        --out docs/figures/quality-surface

Single-end samples have no R2 row; their R2 cells stay empty. Quality is read before filtering (what the
sequencer delivered).
"""

import argparse
import csv
import json
import sys

import style

FIELDS = ["label", "read", "cycle", "mean", "A", "C", "G", "T"]
READS = {"R1": "read1_before_filtering", "R2": "read2_before_filtering"}


def from_fastp(specs):
    rows = []
    for spec in specs:
        label, sep, path = spec.partition("=")
        if not sep:
            sys.exit(f"--from-fastp wants label=path, got {spec!r}")
        with open(path, encoding="utf-8") as fh:
            report = json.load(fh)
        for read, key in READS.items():
            curves = report.get(key, {}).get("quality_curves")
            if not curves:
                continue
            for cycle, mean in enumerate(curves["mean"], start=1):
                row = {"label": label, "read": read, "cycle": cycle, "mean": round(mean, 3)}
                row.update({base: round(curves[base][cycle - 1], 3) for base in "ACGT"})
                rows.append(row)
    return rows


def write_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def matrices(rows):
    """labels, cycles, {read: [[mean or None per cycle] per label]}."""
    labels = list(dict.fromkeys(r["label"] for r in rows))
    cycles = max(int(r["cycle"]) for r in rows)
    out = {}
    for read in READS:
        grid = [[None] * cycles for _ in labels]
        for r in rows:
            if r["read"] == read:
                grid[labels.index(r["label"])][int(r["cycle"]) - 1] = float(r["mean"])
        out[read] = grid
    return labels, cycles, out


def plot(rows, out, title=None):
    import numpy as np

    plt = style.pyplot()
    labels, cycles, grids = matrices(rows)
    values = [v for g in grids.values() for row in g for v in row if v is not None]
    lo, hi = min(values), max(values)
    cmap = style.sequential_cmap().with_extremes(bad=style.SURFACE)
    fig, axes = plt.subplots(1, 2, figsize=(11, 0.42 * len(labels) + 1.6), sharey=True)
    for ax, (read, grid) in zip(axes, grids.items(), strict=True):
        z = np.array([[np.nan if v is None else v for v in row] for row in grid])
        image = ax.imshow(
            z,
            cmap=cmap,
            vmin=lo,
            vmax=hi,
            aspect="auto",
            interpolation="nearest",
            extent=(0.5, cycles + 0.5, len(labels) - 0.5, -0.5),
        )
        for i, row in enumerate(grid):
            if all(v is None for v in row):
                ax.text(cycles / 2, i, "single-end: no R2", ha="center", va="center", fontsize=7.5, color=style.MUTED)
        ax.set_title(read)
        ax.set_xlabel("sequencing cycle (base position in the read)")
        ax.set_yticks(range(len(labels)), labels)
        for side in ax.spines.values():
            side.set_visible(False)
        ax.tick_params(axis="y", length=0)
    bar = fig.colorbar(image, ax=axes, fraction=0.025, pad=0.015)
    bar.set_label("mean base quality (Phred)")
    bar.outline.set_visible(False)
    fig.suptitle(
        title or "Per-base quality by cycle (fastp, before filtering)",
        x=0.01,
        ha="left",
        fontsize=11,
        fontweight="semibold",
    )
    written = style.save(fig, out)
    plt.close(fig)
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from-fastp", nargs="+", metavar="LABEL=FASTP_JSON", help="build the CSV first")
    parser.add_argument("--csv", required=True, help="tidy CSV: written with --from-fastp, else read")
    parser.add_argument("--out", help="output prefix (PNG + SVG)")
    parser.add_argument("--title")
    args = parser.parse_args(argv)
    if args.from_fastp:
        write_csv(from_fastp(args.from_fastp), args.csv)
        print("wrote", args.csv)
    if args.out:
        for path in plot(read_csv(args.csv), args.out, args.title):
            print("wrote", path)


if __name__ == "__main__":
    main()
