"""Signature figure 1: cost x turnaround over align vCPUs x calling shards (the hero figure).

Input: per-stage rows from ``query_pipeline_duration.py --stages --stages-csv`` (one or more files), for a
grid of runs that vary the align stage's vCPUs (stage_resources) and the number of calling shards
(variant_region_window). Each run's grid cell is read from its own rows, so no naming convention is
needed: align vCPUs = the Align_Genome row's vCPU, shards = the number of Call_Shard rows (one per Map
iteration since stage_breakdown counts them separately).

Per run:
  cost        sum of EstCostUSD over its Batch jobs (Fargate estimate; GPU rows have none and are
              skipped with a warning), times --cost-factor (e.g. 0.3 for Fargate Spot: stage_breakdown
              prices are on-demand)
  turnaround  DurationSeconds from ``query_pipeline_duration.py --csv`` when given (--durations),
              otherwise estimated from the stage rows along the critical path:
              max(QC, align) + Plan_Regions + shard phase + Call_Variants + Quality_Gate, each wait + run, the shard
              phase scheduled onto --max-concurrency slots (the Map's MaxConcurrency).
Runs in the same cell are summarised by their median; n is shown.

Output (<out>.png/.svg): cost per run and turnaround as two heatmaps on the same grid, and the
trade-off between them with its Pareto front (no configuration both cheaper and faster). The cheapest
and the fastest cell are ringed. With --surface it also writes <out>-surface.png/.svg, a 3D terrain of
the cost (height) coloured by turnaround, for the README/release banner; the heatmaps are the version
to read numbers from.

    python scripts/figures/cost_surface.py grid.stages.csv [more.stages.csv] \\
        [--durations grid.csv] --out docs/figures/cost-surface [--surface]

See docs/figures/cost-surface-grid.md for the grid of runs this needs.
"""

import argparse
import csv
import heapq
import statistics
import sys
from collections import defaultdict

import figkit

# The Batch stages one after another, as in infrastructure/state_machine.asl.json: (kind, stages).
# "parallel": the stages run side by side (QC and align), the slowest counts. "map": one row per Map
# iteration, scheduled onto the Map's MaxConcurrency slots.
CRITICAL_PATH = (
    ("parallel", ("Quality_Control", "Align_Genome")),
    ("parallel", ("Plan_Regions",)),
    ("map", ("Call_Shard",)),
    ("parallel", ("Call_Variants",)),
    ("parallel", ("Quality_Gate",)),
)
REQUIRED_STAGES = ("Align_Genome", "Call_Shard", "Call_Variants")


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_stage_rows(paths):
    rows = []
    for path in paths:
        with open(path, newline="", encoding="utf-8") as fh:
            rows.extend(csv.DictReader(fh))
    return rows


def read_durations(path):
    """{execution name: (duration seconds, execution status)} from query_pipeline_duration.py --csv."""
    if not path:
        return {}
    with open(path, newline="", encoding="utf-8") as fh:
        return {r["ExecutionName"]: (_float(r["DurationSeconds"]), r.get("Status")) for r in csv.DictReader(fh)}


def makespan(durations, slots):
    """Finish time of jobs started in order on `slots` parallel slots (each takes the first free one)."""
    if not durations:
        return 0.0
    free = [0.0] * max(1, slots)
    for d in durations:
        start = heapq.heappop(free)
        heapq.heappush(free, start + d)
    return max(free)


def estimated_turnaround(stages, max_concurrency):
    """Critical-path estimate from one run's rows: {stage: [wait + run, ...]}."""
    total = 0.0
    for kind, group in CRITICAL_PATH:
        if kind == "map":
            total += makespan([d for s in group for d in stages.get(s, [])], max_concurrency)
        else:
            total += max(max(stages.get(s, [0.0])) for s in group)
    return total


def summarise_runs(rows, durations=None, cost_factor=1.0, max_concurrency=4):
    """One dict per succeeded run: execution, align_vcpus, shards, cost_usd, turnaround_s, measured.

    Succeeded = every Batch job succeeded and the align, shard and merge jobs are all there; with
    `durations` (which carry each execution's own status) the execution must have SUCCEEDED too, which
    also drops runs that failed after their jobs (e.g. at the quality gate in fail mode). Without them
    the execution status is unknown, so a warning says so."""
    durations = durations or {}
    by_run = defaultdict(list)
    for r in rows:
        by_run[r["ExecutionName"]].append(r)
    runs = []
    for name, stage_rows in by_run.items():
        if any(r["Status"] != "SUCCEEDED" for r in stage_rows):
            continue
        if any(not any(r["Stage"] == s for r in stage_rows) for s in REQUIRED_STAGES):
            continue
        measured, status = durations.get(name, (None, None))
        if durations and status != "SUCCEEDED":
            continue
        align = [r for r in stage_rows if r["Stage"] == "Align_Genome"]
        shards = sum(1 for r in stage_rows if r["Stage"] == "Call_Shard")
        costs = [_float(r["EstCostUSD"]) for r in stage_rows]
        if any(c is None for c in costs):
            print(f"warning: {name} has a job without a cost estimate (GPU?); its cost leaves it out", file=sys.stderr)
        spans = defaultdict(list)
        for r in stage_rows:
            spans[r["Stage"]].append((_float(r["WaitSeconds"]) or 0.0) + (_float(r["RunSeconds"]) or 0.0))
        runs.append(
            {
                "execution": name,
                "align_vcpus": _float(align[0]["vCPU"]),
                "shards": shards,
                "cost_usd": sum(c for c in costs if c is not None) * cost_factor,
                "turnaround_s": measured if measured is not None else estimated_turnaround(spans, max_concurrency),
                "measured": measured is not None,
            }
        )
    return runs


def grid(runs):
    """{(align_vcpus, shards): {"cost_usd", "turnaround_s", "n"}} with medians per cell."""
    cells = defaultdict(list)
    for run in runs:
        cells[(run["align_vcpus"], run["shards"])].append(run)
    return {
        key: {
            "cost_usd": statistics.median(r["cost_usd"] for r in group),
            "turnaround_s": statistics.median(r["turnaround_s"] for r in group),
            "n": len(group),
        }
        for key, group in sorted(cells.items())
    }


def pareto(cells):
    """Cells no other cell beats on both cost and turnaround, cheapest first."""
    front = []
    for key, c in sorted(cells.items(), key=lambda kv: (kv[1]["cost_usd"], kv[1]["turnaround_s"])):
        if not front or c["turnaround_s"] < cells[front[-1]]["turnaround_s"]:
            front.append(key)
    return front


def _vcpu_label(v):
    return f"{v:g}"


def _heatmap(ax, cells, xs, ys, value, fmt, title, cmap, ring):
    """value(cell) gives the number in display units (cents, minutes), so cells and colour bar agree."""
    import numpy as np

    z = np.full((len(ys), len(xs)), np.nan)
    for (x, y), c in cells.items():
        z[ys.index(y), xs.index(x)] = value(c)
    image = ax.imshow(z, cmap=cmap, origin="lower", aspect="auto")
    lo, hi = np.nanmin(z), np.nanmax(z)
    marks = dict(ring)
    for (x, y), c in cells.items():
        i, j = ys.index(y), xs.index(x)
        dark = hi > lo and (value(c) - lo) / (hi - lo) > 0.55
        lines = [fmt(value(c))]
        lines += [f"n={c['n']}"] if c["n"] > 1 else []
        lines += [marks[(x, y)]] if (x, y) in marks else []
        ax.text(j, i, "\n".join(lines), ha="center", va="center", fontsize=7.5, color="#ffffff" if dark else figkit.INK)
        if (x, y) in marks:
            ax.add_patch(_ring(j, i, "#ffffff" if dark else figkit.INK))
    ax.set_xticks(range(len(xs)), [_vcpu_label(x) for x in xs])
    ax.set_yticks(range(len(ys)), [str(y) for y in ys])
    ax.set_xlabel("align vCPUs")
    ax.set_ylabel("calling shards")
    ax.set_title(title)
    for side in ax.spines.values():
        side.set_visible(False)
    ax.tick_params(length=0)
    return image


def _ring(j, i, color):
    from matplotlib.patches import Rectangle

    return Rectangle((j - 0.44, i - 0.44), 0.88, 0.88, fill=False, edgecolor=color, linewidth=1.4)


def plot(cells, out, title=None, surface=False):
    plt = figkit.pyplot()
    xs = sorted({k[0] for k in cells})
    ys = sorted({k[1] for k in cells})
    cheapest = min(cells, key=lambda k: cells[k]["cost_usd"])
    fastest = min(cells, key=lambda k: cells[k]["turnaround_s"])
    ring = [(cheapest, "cheapest")] + ([(fastest, "fastest")] if fastest != cheapest else [])

    fig, axes = plt.subplots(1, 3, figsize=(11, 3.6), gridspec_kw={"width_ratios": [1, 1, 1.15]})
    cmap = figkit.sequential_cmap()
    im = _heatmap(
        axes[0],
        cells,
        xs,
        ys,
        lambda c: c["cost_usd"] * 100,
        lambda v: f"{v:.2f}¢",
        "Cost per run (US cents)",
        cmap,
        ring,
    )
    fig.colorbar(im, ax=axes[0], fraction=0.05, pad=0.03).outline.set_visible(False)
    im = _heatmap(
        axes[1],
        cells,
        xs,
        ys,
        lambda c: c["turnaround_s"] / 60,
        lambda v: f"{v:.1f} min",
        "Turnaround (minutes)",
        cmap,
        ring,
    )
    fig.colorbar(im, ax=axes[1], fraction=0.05, pad=0.03).outline.set_visible(False)

    ax = axes[2]
    figkit.tidy(ax, grid_axis="both")
    front = pareto(cells)
    others = [k for k in cells if k not in front]
    ax.scatter(
        [cells[k]["turnaround_s"] / 60 for k in others],
        [cells[k]["cost_usd"] * 100 for k in others],
        s=36,
        color=figkit.BLUES[3],
        edgecolor=figkit.SURFACE,
        linewidth=1.5,
        zorder=3,
        label="other configurations",
    )
    fx = [cells[k]["turnaround_s"] / 60 for k in front]
    fy = [cells[k]["cost_usd"] * 100 for k in front]
    ax.plot(fx, fy, color=figkit.SERIES[0], linewidth=2, zorder=2)
    ax.scatter(
        fx, fy, s=48, color=figkit.SERIES[0], edgecolor=figkit.SURFACE, linewidth=1.5, zorder=4, label="Pareto front"
    )
    for k in front:
        ax.annotate(
            f"{_vcpu_label(k[0])} vCPU, {k[1]} shard{'s' if k[1] != 1 else ''}",
            (cells[k]["turnaround_s"] / 60, cells[k]["cost_usd"] * 100),
            xytext=(6, 4),
            textcoords="offset points",
            fontsize=7,
            color=figkit.INK_2,
        )
    ax.set_xlabel("turnaround (min)")
    ax.set_ylabel("cost per run (US cents)")
    ax.set_title("Trade-off")
    ax.legend(loc="upper right", fontsize=7)

    if title:
        fig.suptitle(title, x=0.01, ha="left", fontsize=11, fontweight="semibold")
    fig.tight_layout()
    written = figkit.save(fig, out)
    plt.close(fig)
    if surface:
        written += plot_surface(cells, xs, ys, out + "-surface", title)
    return written


def plot_surface(cells, xs, ys, out, title=None):
    """3D terrain: height = cost, colour = turnaround. Needs a full grid (every cell measured)."""
    import numpy as np

    plt = figkit.pyplot()
    if len(cells) != len(xs) * len(ys) or len(xs) < 2 or len(ys) < 2:
        print("note: the 3D surface needs a full grid of at least 2 x 2 cells; skipped", file=sys.stderr)
        return []
    gx, gy = np.meshgrid(range(len(xs)), range(len(ys)))
    cost = np.array([[cells[(xs[j], ys[i])]["cost_usd"] * 100 for j in range(len(xs))] for i in range(len(ys))])
    turn = np.array([[cells[(xs[j], ys[i])]["turnaround_s"] / 60 for j in range(len(xs))] for i in range(len(ys))])
    norm = (turn - turn.min()) / ((turn.max() - turn.min()) or 1)
    fig = plt.figure(figsize=(6.4, 4.8))
    ax = fig.add_subplot(projection="3d")
    ax.plot_surface(
        gx,
        gy,
        cost,
        facecolors=figkit.sequential_cmap()(norm),
        edgecolor=figkit.SURFACE,
        linewidth=0.6,
        antialiased=True,
        shade=False,
    )
    ax.set_xticks(range(len(xs)), [_vcpu_label(x) for x in xs])
    ax.set_yticks(range(len(ys)), [str(y) for y in ys])
    ax.set_xlabel("align vCPUs")
    ax.set_ylabel("calling shards")
    ax.set_zlabel("cost per run (¢)")
    ax.view_init(elev=28, azim=-128)
    for axis in (ax.xaxis, ax.yaxis, ax.zaxis):
        axis.set_pane_color((1, 1, 1, 0))
        axis._axinfo["grid"]["color"] = figkit.GRID
    ax.set_title((title + ": " if title else "") + "cost (height), turnaround (colour, darker = slower)", fontsize=9)
    written = figkit.save(fig, out)
    plt.close(fig)
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("stages_csv", nargs="+", help="query_pipeline_duration.py --stages-csv output(s)")
    parser.add_argument("--durations", help="query_pipeline_duration.py --csv output (measured run times)")
    parser.add_argument("--out", required=True, help="output prefix, e.g. docs/figures/cost-surface")
    parser.add_argument("--cost-factor", type=float, default=1.0, help="scale costs, e.g. 0.3 for Fargate Spot")
    parser.add_argument("--max-concurrency", type=int, default=4, help="the Map's MaxConcurrency (estimates)")
    parser.add_argument("--title", default=None)
    parser.add_argument("--surface", action="store_true", help="also write the 3D terrain view")
    parser.add_argument("--table", help="also write the per-cell medians to this CSV")
    args = parser.parse_args(argv)

    runs = summarise_runs(
        read_stage_rows(args.stages_csv), read_durations(args.durations), args.cost_factor, args.max_concurrency
    )
    if not runs:
        sys.exit("no succeeded runs with an align job and at least one Call_Shard job in the input")
    cells = grid(runs)
    if args.table:
        with open(args.table, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["align_vcpus", "shards", "runs", "median_cost_usd", "median_turnaround_s"])
            for (x, y), c in cells.items():
                writer.writerow([f"{x:g}", y, c["n"], f"{c['cost_usd']:.6f}", f"{c['turnaround_s']:.1f}"])
    if not args.durations:
        print(
            "note: no --durations: turnaround is estimated from the stage rows and the executions' own status"
            " is unknown (a run that failed after its jobs, e.g. at the gate in fail mode, is not excluded)",
            file=sys.stderr,
        )
    for path in plot(cells, args.out, args.title, args.surface):
        print("wrote", path)


if __name__ == "__main__":
    main()
