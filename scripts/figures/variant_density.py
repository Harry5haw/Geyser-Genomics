"""Signature figure 4: variant density heatmap, calls per Mb along a chromosome, one row per caller
(bcftools, DeepVariant, pangenome-aware DeepVariant), grouped by sample.

Input: VCFs from the benchmark runs (geyser-data/variants*, not committed). Only CHROM, POS and FILTER
are read, plus the contig length from the header. A record counts if FILTER is PASS or "." (no filter
applied). DeepVariant sets FILTER, and its RefCall candidates are not variants (see #102's note on
bcftools stats -f PASS), so its rows are PASS calls. The pipeline's ``bcftools call -mv`` sets no FILTER,
so a bcftools row is *every* call, unfiltered: the VCF as the pipeline writes it and as hap.py scored it.
The counts per bin are the only thing written, so the committed CSV holds no genotypes or positions:

    python scripts/figures/variant_density.py --from-vcf "HG003|bcftools=variants/HG003_chr20.vcf.gz" ... \\
        --csv docs/figures/csv/variant-density.csv
    python scripts/figures/variant_density.py --csv docs/figures/csv/variant-density.csv \\
        --out docs/figures/variant-density

A label "SAMPLE|caller" groups rows by sample (a header row per sample, in first-seen sample order); a
plain label is its own group.
"""

import argparse
import gzip
import re
import sys

import figkit

FIELDS = ["label", "chrom", "bin_start", "bin_end", "variants"]
COUNTED = {"PASS", "."}  # "." = no filter applied (bcftools call)


def count_bins(path, chrom, bin_size):
    """({bin index: counted records}, contig length from the header or None) for one contig."""
    opener = gzip.open if path.endswith(".gz") else open
    counts, length = {}, None
    contig = re.compile(r"##contig=<ID=" + re.escape(chrom) + r",.*?length=(\d+)")
    with opener(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("#"):
                found = contig.match(line)
                if found:
                    length = int(found.group(1))
                continue
            fields = line.split("\t", 7)
            if fields[0] != chrom or fields[6] not in COUNTED:
                continue
            b = (int(fields[1]) - 1) // bin_size
            counts[b] = counts.get(b, 0) + 1
    return counts, length


def from_vcf(specs, chrom, bin_size, length=None):
    """Tidy rows for every label and every bin of the contig, zeros included. The contig length is
    `length`, else the VCF headers' ##contig line, else (with a warning) the last occupied bin's end."""
    counted, header_lengths = [], set()
    for spec in specs:
        label, path = figkit.parse_spec(spec, "--from-vcf")
        counts, header_length = count_bins(path, chrom, bin_size)
        if not counts:
            sys.exit(f"{path}: no counted records on {chrom!r} (contig names differ? see --chrom)")
        counted.append((label, counts))
        if header_length:
            header_lengths.add(header_length)
    if not length and len(header_lengths) == 1:
        length = header_lengths.pop()
    if not length:
        length = (max(max(c) for _, c in counted) + 1) * bin_size
        print(f"warning: no single contig length for {chrom} in the headers; bins end at {length:,}", file=sys.stderr)
    rows = []
    for label, counts in counted:
        for b in range((length - 1) // bin_size + 1):
            rows.append(
                {
                    "label": label,
                    "chrom": chrom,
                    "bin_start": b * bin_size,
                    "bin_end": min((b + 1) * bin_size, length),
                    "variants": counts.get(b, 0),
                }
            )
    return rows


def read_csv(path):
    return figkit.read_csv(path)


def group_of(label):
    return label.partition("|")[0]


def matrix(rows):
    """labels (grouped by sample, first-seen order within), bin edges (bp), per-Mb densities [label][bin]."""
    seen = list(dict.fromkeys(r["label"] for r in rows))
    groups = list(dict.fromkeys(group_of(label) for label in seen))
    labels = sorted(seen, key=lambda label: groups.index(group_of(label)))  # stable: callers keep their order
    starts = sorted({int(r["bin_start"]) for r in rows})
    ends = {int(r["bin_start"]): int(r["bin_end"]) for r in rows}
    index = {s: i for i, s in enumerate(starts)}
    grid = [[0.0] * len(starts) for _ in labels]
    for r in rows:
        width_mb = (int(r["bin_end"]) - int(r["bin_start"])) / 1e6
        grid[labels.index(r["label"])][index[int(r["bin_start"])]] = int(r["variants"]) / width_mb
    return labels, starts + [ends[starts[-1]]], grid


def plot(rows, out, title=None, cap=98.0):
    import numpy as np

    plt = figkit.pyplot()
    labels, edges, grid = matrix(rows)
    starts = edges[:-1]
    grouped = any("|" in label for label in labels)
    # With "SAMPLE|caller" labels each sample gets a header row (its name, no cells) above its callers.
    display, names, headers = [], [], []
    for i, label in enumerate(labels):
        group, _, caller = label.partition("|")
        if grouped and (i == 0 or group != group_of(labels[i - 1])):
            headers.append((len(display), group))
            display.append([np.nan] * len(starts))
            names.append("")
        display.append(grid[i])
        names.append(caller or group)
    z = np.array(display, dtype=float)
    bin_mb = (starts[1] - starts[0]) / 1e6 if len(starts) > 1 else 1.0
    # A few bins (bcftools near the centromere) are ~4x the rest; a linear scale up to them would wash
    # out everything else, so the scale stops at the --cap percentile and darker bins are "at least".
    vmax = float(np.nanpercentile(z, cap)) if np.isfinite(z).any() else 1.0
    cmap = figkit.sequential_cmap().with_extremes(bad=figkit.SURFACE)
    fig, ax = plt.subplots(figsize=(11, 0.34 * len(names) + 1.5))
    # Cells on the true bin edges: the last bin ends at the contig's end, not a whole bin later.
    image = ax.pcolormesh(
        [e / 1e6 for e in edges], np.arange(len(names) + 1) - 0.5, z, cmap=cmap, vmin=0, vmax=vmax, shading="flat"
    )
    ax.set_xlim(0, edges[-1] / 1e6)
    ax.set_ylim(len(names) - 0.5, -0.5)
    ax.set_yticks(range(len(names)), names)
    for row, group in headers:
        ax.text(0, row + 0.25, group, ha="left", va="bottom", fontsize=9, fontweight="semibold", color=figkit.INK)
    for side in ax.spines.values():
        side.set_visible(False)
    ax.tick_params(axis="y", length=0)
    over = int((z > vmax).sum())
    chrom = rows[0]["chrom"] if rows else ""
    ax.set_xlabel(f"position on {chrom} (Mb), {bin_mb:g} Mb bins")
    bar = fig.colorbar(image, ax=ax, fraction=0.025, pad=0.01, extend="max")
    bar.set_label("calls per Mb (FILTER PASS, or none set)")
    if over:
        ax.text(
            1.0,
            1.01,
            f"scale capped at the {cap:g}th percentile ({vmax:,.0f}/Mb); {over} bins above it",
            transform=ax.transAxes,
            ha="right",
            va="bottom",
            fontsize=7,
            color=figkit.MUTED,
        )
    bar.outline.set_visible(False)
    ax.set_title(title or f"Variant density along {chrom} by caller")
    written = figkit.save(fig, out)
    plt.close(fig)
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from-vcf", nargs="+", metavar="LABEL=VCF", help="build the CSV first")
    parser.add_argument("--chrom", default="chr20")
    parser.add_argument("--bin-size", type=int, default=1_000_000)
    parser.add_argument("--length", type=int, help="contig length (default: from the VCF headers)")
    parser.add_argument("--csv", required=True, help="tidy CSV: written with --from-vcf, else read")
    parser.add_argument("--out", help="output prefix (PNG + SVG)")
    parser.add_argument("--title")
    parser.add_argument("--cap", type=float, default=98.0, help="colour scale stops at this percentile")
    args = parser.parse_args(argv)
    if args.from_vcf:
        figkit.write_csv(from_vcf(args.from_vcf, args.chrom, args.bin_size, args.length), args.csv, FIELDS)
        print("wrote", args.csv)
    if args.out:
        for path in plot(read_csv(args.csv), args.out, args.title, args.cap):
            print("wrote", path)


if __name__ == "__main__":
    main()
