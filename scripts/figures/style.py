"""One look for every signature figure: palette, ink, fonts and the save helper.

The colours are the dataviz skill's validated reference palette (light mode: these figures are static
PNG/SVG for the README, the release notes and the printed dissertation). Rules the scripts follow:
categorical hues in fixed slot order and never cycled; magnitude in one hue, light to dark; text in ink
colours, never in a series colour; hairline grids; no dual axes.
"""

import os

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


def sequential_cmap(name="geyser-blues"):
    from matplotlib.colors import LinearSegmentedColormap

    return LinearSegmentedColormap.from_list(name, BLUES)


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
