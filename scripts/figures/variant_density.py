"""Signature figure 4: variant density heatmap, PASS variants per Mb along a chromosome, one row per
caller (bcftools, DeepVariant, pangenome-aware DeepVariant), grouped by sample.

Input: VCFs from the benchmark runs (geyser-data/variants*, not committed). Only CHROM, POS and FILTER
are read: a record counts if FILTER is PASS or "." (DeepVariant writes RefCall candidates into its VCF;
they are not variants, see #102's note on bcftools stats -f PASS). The counts per bin are the only thing
written, so the committed CSV holds no genotypes or positions:

    python scripts/figures/variant_density.py --from-vcf "HG003|bcftools=variants/HG003_chr20.vcf.gz" ... \\
        --csv docs/figures/csv/variant-density.csv
    python scripts/figures/variant_density.py --csv docs/figures/csv/variant-density.csv \\
        --out docs/figures/variant-density

A label "SAMPLE|caller" groups rows by sample (a gap between groups); a plain label is its own group.
"""

import argparse
import csv
import gzip
import sys

import style

FIELDS = ["label", "chrom", "bin_start", "bin_end", "variants"]
PASSING = {"PASS", "."}


def count_bins(path, chrom, bin_size):
    """{bin index: PASS variant count} for one contig of a VCF(.gz)."""
    opener = gzip.open if path.endswith(".gz") else open
    counts = {}
    with opener(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            fields = line.split("\t", 7)
            if fields[0] != chrom or fields[6] not in PASSING:
                continue
            b = (int(fields[1]) - 1) // bin_size
            counts[b] = counts.get(b, 0) + 1
    return counts


def from_vcf(specs, chrom, bin_size, length=None):
    """Tidy rows for every label, every bin up to the last occupied one (or `length`), zeros included."""
    counted = []
    for spec in specs:
        label, sep, path = spec.partition("=")
        if not sep:
            sys.exit(f"--from-vcf wants label=path, got {spec!r}")
        counted.append((label, count_bins(path, chrom, bin_size)))
    last = (length - 1) // bin_size if length else max((max(c, default=0) for _, c in counted), default=0)
    rows = []
    for label, counts in counted:
        for b in range(last + 1):
            end = (b + 1) * bin_size if not length else min((b + 1) * bin_size, length)
            rows.append(
                {
                    "label": label,
                    "chrom": chrom,
                    "bin_start": b * bin_size,
                    "bin_end": end,
                    "variants": counts.get(b, 0),
                }
            )
    return rows


def write_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def matrix(rows):
    """labels (first-seen order), bin starts (Mb), per-Mb densities [label][bin]."""
    labels = list(dict.fromkeys(r["label"] for r in rows))
    starts = sorted({int(r["bin_start"]) for r in rows})
    index = {s: i for i, s in enumerate(starts)}
    grid = [[0.0] * len(starts) for _ in labels]
    for r in rows:
        width_mb = (int(r["bin_end"]) - int(r["bin_start"])) / 1e6
        grid[labels.index(r["label"])][index[int(r["bin_start"])]] = int(r["variants"]) / width_mb
    return labels, starts, grid


def plot(rows, out, title=None, cap=98.0):
    import numpy as np

    plt = style.pyplot()
    labels, starts, grid = matrix(rows)
    grouped = any("|" in label for label in labels)
    # With "SAMPLE|caller" labels each sample gets a header row (its name, no cells) above its callers.
    display, names, headers = [], [], []
    for i, label in enumerate(labels):
        group, _, caller = label.partition("|")
        if grouped and (i == 0 or group != labels[i - 1].partition("|")[0]):
            headers.append((len(display), group))
            display.append([np.nan] * len(starts))
            names.append("")
        display.append(grid[i])
        names.append(caller or group)
    z = np.array(display, dtype=float)
    bin_mb = (starts[1] - starts[0]) / 1e6 if len(starts) > 1 else 1.0
    # A few bins (bcftools near the centromere) are 5-10x the rest; a linear scale up to them would wash
    # out everything else, so the scale stops at the --cap percentile and darker bins are "at least".
    vmax = float(np.nanpercentile(z, cap)) if np.isfinite(z).any() else 1.0
    cmap = style.sequential_cmap().with_extremes(bad=style.SURFACE)
    fig, ax = plt.subplots(figsize=(11, 0.34 * len(names) + 1.5))
    image = ax.imshow(
        z,
        cmap=cmap,
        aspect="auto",
        interpolation="nearest",
        vmin=0,
        vmax=vmax,
        extent=(0, len(starts) * bin_mb, len(names) - 0.5, -0.5),
    )
    ax.set_yticks(range(len(names)), names)
    for row, group in headers:
        ax.text(0, row + 0.25, group, ha="left", va="bottom", fontsize=9, fontweight="semibold", color=style.INK)
    for side in ax.spines.values():
        side.set_visible(False)
    ax.tick_params(axis="y", length=0)
    over = int((z > vmax).sum())
    chrom = rows[0]["chrom"] if rows else ""
    ax.set_xlabel(f"position on {chrom} (Mb), {bin_mb:g} Mb bins")
    bar = fig.colorbar(image, ax=ax, fraction=0.025, pad=0.01, extend="max")
    bar.set_label("PASS variants per Mb")
    if over:
        ax.text(
            1.0,
            1.01,
            f"scale capped at the {cap:g}th percentile ({vmax:,.0f}/Mb); {over} bins above it",
            transform=ax.transAxes,
            ha="right",
            va="bottom",
            fontsize=7,
            color=style.MUTED,
        )
    bar.outline.set_visible(False)
    ax.set_title(title or f"Variant density along {chrom} by caller")
    written = style.save(fig, out)
    plt.close(fig)
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from-vcf", nargs="+", metavar="LABEL=VCF", help="build the CSV first")
    parser.add_argument("--chrom", default="chr20")
    parser.add_argument("--bin-size", type=int, default=1_000_000)
    parser.add_argument("--length", type=int, default=64_444_167, help="contig length (default GRCh38 chr20)")
    parser.add_argument("--csv", required=True, help="tidy CSV: written with --from-vcf, else read")
    parser.add_argument("--out", help="output prefix (PNG + SVG)")
    parser.add_argument("--title")
    parser.add_argument("--cap", type=float, default=98.0, help="colour scale stops at this percentile")
    args = parser.parse_args(argv)
    if args.from_vcf:
        write_csv(from_vcf(args.from_vcf, args.chrom, args.bin_size, args.length), args.csv)
        print("wrote", args.csv)
    if args.out:
        for path in plot(read_csv(args.csv), args.out, args.title, args.cap):
            print("wrote", path)


if __name__ == "__main__":
    main()
