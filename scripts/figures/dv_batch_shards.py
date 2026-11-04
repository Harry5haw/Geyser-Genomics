"""DeepVariant batch x shards: the stage's speed over call_variants batch size x make_examples shards on one
GPU (#139), as three heatmaps on the same grid: the whole stage, call_variants alone, and the GPU's mean
utilisation during call_variants.

Input: the table scripts/benchmark_dv_grid.py writes (dv-grid.csv, one row per run). Build the summary CSV
(median per cell over its runs), then plot:

    python scripts/figures/dv_batch_shards.py --from-grid <results>/dv-grid.csv \\
        --csv docs/figures/csv/dv-batch-shards.csv
    python scripts/figures/dv_batch_shards.py --csv docs/figures/csv/dv-batch-shards.csv \\
        --out docs/figures/dv-batch-shards

A cell whose runs all failed (e.g. a batch too big for the card's memory) is drawn empty and labelled
"failed"; the summary keeps it, with its error.
"""

import argparse
import statistics
import sys

import figkit

FIELDS = [
    "batch_size",
    "shards",
    "runs",
    "succeeded",
    "stage_s",
    "make_examples_s",
    "call_variants_s",
    "postprocess_variants_s",
    "examples",
    "call_variants_gpu_util_mean_pct",
    "gpu_memory_peak_mib",
    "pass_snps",
    "pass_indels",
    "same_calls",
    "error",
]
MEDIANS = (
    "stage_s",
    "make_examples_s",
    "call_variants_s",
    "postprocess_variants_s",
    "call_variants_gpu_util_mean_pct",
    "pass_snps",
    "pass_indels",
)


def _num(value):
    return None if value in (None, "") else float(value)


def summarise(rows):
    """One row per (batch, shards): medians over its successful runs. same_calls says whether every successful
    run in the whole grid produced byte-identical records: batch size and shards should change speed only, but
    GPU inference isn't bit-reproducible, so compare the PASS counts too (a repeat of one cell shows the noise)."""
    ok = [r for r in rows if r["status"] == "Success"]
    digests = {r["records_md5"] for r in ok if r.get("records_md5")}
    same = "yes" if len(digests) == 1 else ("no" if digests else "")
    cells = {}
    for r in rows:
        cells.setdefault((int(r["batch_size"]), int(r["shards"])), []).append(r)
    out = []
    for (batch, shards), runs in sorted(cells.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        good = [r for r in runs if r["status"] == "Success"]
        row = {"batch_size": batch, "shards": shards, "runs": len(runs), "succeeded": len(good)}
        for key in MEDIANS:
            values = [_num(r[key]) for r in good if _num(r[key]) is not None]
            median = round(statistics.median(values), 1) if values else ""
            row[key] = int(median) if values and median == int(median) and key.startswith("pass_") else median
        examples = {r["examples"] for r in good if r.get("examples")}
        row["examples"] = examples.pop() if len(examples) == 1 else ""
        peaks = [_num(r["gpu_memory_peak_mib"]) for r in runs if _num(r["gpu_memory_peak_mib"]) is not None]
        row["gpu_memory_peak_mib"] = int(max(peaks)) if peaks else ""
        row["same_calls"] = same if good else ""
        row["error"] = "" if good else (runs[-1].get("error") or "failed")[:120]
        out.append(row)
    return out


def _heatmap(ax, cells, xs, ys, key, fmt, title, cmap, scale=1.0):
    """scale converts the CSV's unit to the display unit (s -> min), so cells and colour bar agree."""
    import numpy as np

    z = np.full((len(ys), len(xs)), np.nan)
    for (x, y), c in cells.items():
        if c[key] != "":
            z[ys.index(y), xs.index(x)] = float(c[key]) * scale
    image = ax.imshow(z, cmap=cmap, origin="lower", aspect="auto")
    lo, hi = np.nanmin(z), np.nanmax(z)
    for (x, y), c in cells.items():
        i, j = ys.index(y), xs.index(x)
        if c[key] == "":
            ax.text(j, i, "failed", ha="center", va="center", fontsize=7.5, color=figkit.MUTED)
            continue
        value = float(c[key]) * scale
        dark = hi > lo and (value - lo) / (hi - lo) > 0.55
        label = fmt(value) + (f"\nn={c['succeeded']}" if int(c["succeeded"]) > 1 else "")
        ax.text(j, i, label, ha="center", va="center", fontsize=7.5, color="#ffffff" if dark else figkit.INK)
    ax.set_xticks(range(len(xs)), [str(x) for x in xs])
    ax.set_yticks(range(len(ys)), [str(y) for y in ys])
    ax.set_xlabel("call_variants batch size")
    ax.set_ylabel("make_examples shards")
    ax.set_title(title)
    for side in ax.spines.values():
        side.set_visible(False)
    ax.tick_params(length=0)
    return image


def plot(rows, out, title=None):
    plt = figkit.pyplot()
    cells = {(int(r["batch_size"]), int(r["shards"])): r for r in rows}
    xs = sorted({k[0] for k in cells})
    ys = sorted({k[1] for k in cells})
    fig, axes = plt.subplots(1, 3, figsize=(11, 3.4))
    cmap = figkit.sequential_cmap()
    panels = [
        ("stage_s", lambda v: f"{v:.1f} min", "Whole stage (minutes)", 1 / 60),
        ("call_variants_s", lambda v: f"{v:.0f} s", "call_variants (seconds)", 1.0),
        ("call_variants_gpu_util_mean_pct", lambda v: f"{v:.0f}%", "GPU busy during call_variants (%)", 1.0),
    ]
    for ax, (key, fmt, panel_title, scale) in zip(axes, panels, strict=True):
        image = _heatmap(ax, cells, xs, ys, key, fmt, panel_title, cmap, scale)
        fig.colorbar(image, ax=ax, fraction=0.05, pad=0.03).outline.set_visible(False)
    if title:
        fig.suptitle(title, x=0.01, ha="left", fontsize=11, fontweight="semibold")
    fig.tight_layout()
    written = figkit.save(fig, out)
    plt.close(fig)
    return written


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--from-grid", metavar="CSV", help="benchmark_dv_grid.py's dv-grid.csv (one row per run)")
    p.add_argument("--csv", required=True, help="the summary CSV: written with --from-grid, else read")
    p.add_argument("--out", help="output prefix for <out>.png and <out>.svg")
    p.add_argument("--title")
    a = p.parse_args(argv)
    if a.from_grid:
        figkit.write_csv(summarise(figkit.read_csv(a.from_grid)), a.csv, FIELDS)
    if a.out:
        for path in plot(figkit.read_csv(a.csv), a.out, a.title):
            print(path)
    elif not a.from_grid:
        sys.exit("nothing to do: give --from-grid and/or --out")
    return 0


if __name__ == "__main__":
    sys.exit(main())
