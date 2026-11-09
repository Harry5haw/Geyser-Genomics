"""One look for every signature figure: palette, ink, fonts and the save helper.

The colours are the dataviz skill's validated reference palette (light mode: these figures are static
PNG/SVG for the README, the release notes and the printed dissertation). Rules the scripts follow:
categorical hues in fixed slot order and never cycled; magnitude on the heat ramp below (low dark, high light,
the same mapping as the 3D figures on the web page); text in ink
colours, never in a series colour; hairline grids; no dual axes.
"""

import os
import sys

# Categorical slots, in this order (the order is what keeps adjacent pairs colour-blind safe).
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]

# Sequential blue, step 100 (near zero) to 700 (most).
BLUES = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6"]
BLUES += ["#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b"]

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
FONT = ["Segoe UI", "Helvetica Neue", "Arial", "DejaVu Sans"]


def pyplot():
    """matplotlib's pyplot on the non-interactive backend, styled. Imported lazily: tests that only
    check the data helpers don't need matplotlib."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "figure.facecolor": SURFACE,
            "axes.facecolor": SURFACE,
            "savefig.facecolor": SURFACE,
            "font.family": "sans-serif",
            "font.sans-serif": FONT,
            "font.size": 9,
            "text.color": INK,
            "axes.labelcolor": INK_2,
            "axes.titlecolor": INK,
            "axes.titlesize": 10,
            "axes.titleweight": "semibold",
            "axes.titlelocation": "left",
            "axes.edgecolor": AXIS,
            "axes.linewidth": 0.6,
            "axes.grid": False,
            "grid.color": GRID,
            "grid.linewidth": 0.5,
            "xtick.color": MUTED,
            "ytick.color": MUTED,
            "xtick.labelcolor": INK_2,
            "ytick.labelcolor": INK_2,
            "xtick.major.size": 3,
            "ytick.major.size": 3,
            "legend.frameon": False,
            "svg.fonttype": "none",  # text stays text in the SVG
            "svg.hashsalt": "geyser",  # deterministic SVG ids
            "path.simplify": True,
        }
    )
    return plt


# The heat ramps for magnitude (heatmaps, 3D surfaces, colour bars). The web page uses the same stops
# (web/samples/figures.py HEAT_TURBO / HEAT_CIVIDIS; tests/test_figures.py checks they agree).
# HEAT_TURBO, the default "rainbow heatcam": Google's Turbo colormap (Anton Mikhailov, 2019, "Turbo, An Improved
# Rainbow Colormap for Visualization", Apache License 2.0), 15 stops sampled from 0.10 to 0.90 of its lookup table
# (matplotlib's "turbo"), trimmed so both ends clear 2:1 against the light (#f3efe6) and dark (#262626) stage.
HEAT_TURBO = [
    "#4559cb", "#4680f6", "#3aa3fc", "#20c7df", "#19e3b9", "#38f491", "#71fe5f", "#a4fc3c",
    "#c8ef34", "#e9d539", "#fbb838", "#fe9029", "#f36315", "#e14109", "#c32503",
]  # fmt: skip
# HEAT_CIVIDIS, the high-contrast alternative: cividis from 0 to 0.742, low = dark navy, high = khaki. Perceptually
# uniform, lightness rises monotonically (reads in greyscale and with any colour-vision deficiency), and the light
# end is trimmed so it still clears 2:1 against the beige stage.
HEAT_CIVIDIS = ["#00224e", "#18376f", "#424e6c", "#5f636f", "#7b7a78", "#999277", "#b9ab6d"]
HEAT = HEAT_TURBO  # the default ramp


def sequential_cmap(name="geyser-heat", stops=None):
    from matplotlib.colors import LinearSegmentedColormap

    return LinearSegmentedColormap.from_list(name, stops or HEAT)


def _luminance(rgb):
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def ink_on(fraction, stops=None):
    """Text colour on a heat-ramp cell at this fraction of the scale: whichever of white and ink contrasts more
    with the cell's own colour (Turbo is dark at both ends and bright in the middle)."""
    rgb = sequential_cmap(stops=stops)(min(1.0, max(0.0, fraction)))[:3]
    lum = _luminance(rgb)
    return "#ffffff" if (1.05 / (lum + 0.05)) > ((lum + 0.05) / (_luminance([0.043] * 3) + 0.05)) else INK


def tidy(ax, grid_axis="y"):
    """Recessive chrome: no top/right spines, hairline grid on one axis behind the marks."""
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    if grid_axis:
        ax.grid(True, axis=grid_axis)
        ax.set_axisbelow(True)


def save(fig, out_prefix, formats=("png", "svg")):
    """Write <out_prefix>.png (200 dpi) and .svg without timestamps, so reruns give identical files."""
    folder = os.path.dirname(out_prefix)
    if folder:
        os.makedirs(folder, exist_ok=True)
    written = []
    for fmt in formats:
        path = f"{out_prefix}.{fmt}"
        meta = {"Software": None} if fmt == "png" else {"Date": None, "Creator": None}
        fig.savefig(path, dpi=200, bbox_inches="tight", metadata=meta)
        written.append(path)
    return written


# ---- shared input/output for the figure scripts ----


def parse_spec(spec, option):
    """ "label=path" -> (label, path). The label may not contain "=", the path may."""
    label, sep, path = spec.partition("=")
    if not sep or not label or not path:
        sys.exit(f"{option} wants label=path, got {spec!r}")
    return label, path


def write_csv(rows, path, fields):
    """Write tidy rows; refuse to write an empty table (a wrong --chrom or label usually causes it)."""
    import csv

    if not rows:
        sys.exit(f"nothing to write to {path}: no rows matched (check --chrom and the inputs)")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path):
    import csv

    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        sys.exit(f"{path} has no data rows")
    return rows
