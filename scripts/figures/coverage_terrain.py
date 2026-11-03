"""Signature figure 2: coverage terrain, mean read depth along chr20 as a ridgeline, one ridge per
sample or aligner.

Input: a tidy CSV ``label,chrom,start,end,mean_depth`` (one row per window). Build it from mosdepth's
windowed output (``mosdepth -n --fast-mode --by 500000 <prefix> <bam>`` writes <prefix>.regions.bed.gz):

    python scripts/figures/coverage_terrain.py --from-mosdepth "HG003 35x=HG003_chr20.regions.bed.gz" ... \\
        --csv docs/figures/csv/coverage-terrain.csv
    python scripts/figures/coverage_terrain.py --csv docs/figures/csv/coverage-terrain.csv \\
        --out docs/figures/coverage-terrain

The pipeline's own stats/{id}/ keep only mosdepth's global distribution and summary (default mode, no
--by), so windowed depth comes from a separate mosdepth run on the BAM/CRAM; the windows are small
derived numbers (no reads), fine to commit.

Each ridge is drawn on the same depth scale (shared across ridges, so a 30x and a 35x sample compare
honestly); its baseline is the ridge's label row. Depth above --clip x the overall median is clipped
(collapsed duplications and satellite arrays reach hundreds of x) and a tick marks where.
"""

import argparse
import csv
import gzip
import statistics
import sys

import style

FIELDS = ["label", "chrom", "start", "end", "mean_depth"]


def read_mosdepth_regions(path, chrom=None):
    """[(chrom, start, end, depth)] from a mosdepth regions.bed(.gz)."""
    opener = gzip.open if path.endswith(".gz") else open
    rows = []
    with opener(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            parts = line.split("\t")
            if len(parts) < 4 or (chrom and parts[0] != chrom):
                continue
            rows.append((parts[0], int(parts[1]), int(parts[2]), float(parts[-1])))
    return rows


def from_mosdepth(specs, chrom=None):
    """specs: ["label=path", ...] -> tidy rows (dicts with FIELDS)."""
    out = []
    for spec in specs:
        label, sep, path = spec.partition("=")
        if not sep:
            sys.exit(f"--from-mosdepth wants label=path, got {spec!r}")
        for c, start, end, depth in read_mosdepth_regions(path, chrom):
            out.append({"label": label, "chrom": c, "start": start, "end": end, "mean_depth": round(depth, 2)})
    return out


def write_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return [
            {**r, "start": int(r["start"]), "end": int(r["end"]), "mean_depth": float(r["mean_depth"])}
            for r in csv.DictReader(fh)
        ]


def ridges(rows):
    """{label: (mids_mb, depths)} in first-seen label order, windows sorted by position."""
    by_label = {}
    for r in rows:
        by_label.setdefault(r["label"], []).append(r)
    out = {}
    for label, windows in by_label.items():
        windows.sort(key=lambda r: r["start"])
        out[label] = ([(r["start"] + r["end"]) / 2e6 for r in windows], [r["mean_depth"] for r in windows])
    return out


def clip_level(series, factor):
    """factor x the median of every window with any coverage (gaps like the centromere excluded)."""
    covered = [d for _, depths in series.values() for d in depths if d > 0]
    return factor * statistics.median(covered) if covered else 1.0


def plot(rows, out, title=None, clip=2.5, overlap=1.6):
    plt = style.pyplot()
    series = ridges(rows)
    cap = clip_level(series, clip)
    n = len(series)
    fig, ax = plt.subplots(figsize=(10, 0.62 * n + 1.3))
    height = overlap / cap  # one ridge spacing = cap / overlap of depth
    labels = list(series)
    for i, label in enumerate(labels):
        base = n - 1 - i  # first label on top
        xs, depths = series[label]
        ys = [base + min(d, cap) * height for d in depths]
        # Later (lower) ridges are drawn over earlier ones; the surface-coloured fill hides what's behind.
        z = 10 + 2 * i
        ax.fill_between(xs, base, ys, color=style.SURFACE, linewidth=0, zorder=z)
        ax.fill_between(xs, base, ys, color=style.BLUES[2], alpha=0.85, linewidth=0, zorder=z)
        ax.plot(xs, ys, color=style.BLUES[9], linewidth=1.0, zorder=z + 1)
        ax.axhline(base, color=style.AXIS, linewidth=0.5, zorder=z + 1)
        clipped = [x for x, d in zip(xs, depths, strict=True) if d > cap]
        if clipped:
            ax.plot(
                clipped,
                [base + cap * height] * len(clipped),
                linestyle="none",
                marker="|",
                markersize=4,
                color=style.INK_2,
                zorder=z + 1,
            )
        covered = [d for d in depths if d > 0]
        median = statistics.median(covered) if covered else 0
        ax.text(-0.6, base + 0.08, label, ha="right", va="bottom", fontsize=8.5, color=style.INK)
        ax.text(-0.6, base + 0.04, f"median {median:.0f}x", ha="right", va="top", fontsize=7, color=style.MUTED)
    xmax = max(max(xs) for xs, _ in series.values())
    ax.set_xlim(0, xmax)
    ax.set_ylim(-0.2, n - 1 + overlap + 0.2)
    ax.set_yticks([])
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.grid(True, axis="x")
    ax.set_axisbelow(True)
    chrom = rows[0]["chrom"] if rows else ""
    width_kb = (rows[0]["end"] - rows[0]["start"]) / 1000 if rows else 0
    ax.set_xlabel(f"position on {chrom} (Mb); mean depth per {width_kb:.0f} kb window")
    note = f"one ridge spacing = {cap / overlap:.0f}x; | = clipped above {cap:.0f}x ({clip:g} x the median)"
    ax.set_title(title or f"Coverage terrain along {chrom}", loc="left")
    ax.text(1.0, 1.01, note, transform=ax.transAxes, ha="right", va="bottom", fontsize=7, color=style.MUTED)
    fig.tight_layout()
    written = style.save(fig, out)
    plt.close(fig)
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from-mosdepth", nargs="+", metavar="LABEL=REGIONS_BED", help="build the CSV first")
    parser.add_argument("--chrom", default="chr20", help="keep this contig only (default chr20)")
    parser.add_argument("--csv", required=True, help="tidy CSV: written with --from-mosdepth, else read")
    parser.add_argument("--out", help="output prefix (PNG + SVG)")
    parser.add_argument("--title")
    parser.add_argument("--clip", type=float, default=2.5, help="clip depth above this x the median")
    args = parser.parse_args(argv)
    if args.from_mosdepth:
        write_csv(from_mosdepth(args.from_mosdepth, args.chrom), args.csv)
        print("wrote", args.csv)
    if args.out:
        for path in plot(read_csv(args.csv), args.out, args.title, args.clip):
            print("wrote", path)


if __name__ == "__main__":
    main()
