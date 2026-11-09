# Design note: the grid of cloud runs behind the cost x turnaround figure

Status: **run on 4 Nov 2026: 25 of 25 runs succeeded.** The figure is [`cost-surface.png`](cost-surface.png),
and the reading notes are in [README.md](README.md). How it actually ran, which differs from the plan, is in
[As run](#as-run) at the end. The rest of this note is the design as written before the runs.

## What the figure needs

Height = cost per run (and a second panel for turnaround) over **align vCPUs x calling shards**, one
cell per configuration, at least 2 runs per cell (the script takes the median and shows n).

## The two knobs, and why they can't vary per run today

| Knob | Terraform variable | Values in the grid | Shards / note |
|---|---|---|---|
| Align vCPUs (+ memory) | `stage_resources["align"]` | 1 vCPU / 4 GiB, 2 / 4 GiB, 4 / 8 GiB | 8 vCPU is ruled out by the `qc + align <= fargate_max_vcpus` (8) check; raising the cap is a spend-rate decision |
| Calling shards | `variant_region_window` | 65 Mb, 33 Mb, 17 Mb, 9 Mb | 1, 2, 4, 8 shards on GRCh38 chr20 (64.4 Mb). 8 shards run in two waves of `variants_max_concurrency` = 4 |

Both are rendered into the state machine by `templatefile()` (`${align_vcpu}`, `${variant_region_window}`),
so every grid cell would be a separate plan + in-place apply of the state machine: 12 applies.

**Recommended: one small infra PR first.** This is draft #192 (`infra/run-overrides`, planned 0 add /
1 change / 0 destroy, the state machine in place; not applied, decided together with this grid). A
manual start passes the settings in a nested `overrides` object; uploads via the trigger Lambda send none,
so triggered runs are unchanged:

```bash
aws stepfunctions start-execution --state-machine-arn "$SFN_ARN" --input '{"srr_id": "REAL-HG003-30x-chr20",
  "reference_name": "chr20.fa",
  "overrides": {"align_vcpu": "2", "align_memory_mb": "4096", "variant_region_window": 17000000}}'
```

- Nested rather than top-level fields because Step Functions can't list an object's keys: a misspelt
  top-level `align_cpu` would silently run the defaults, while inside `overrides` an unknown key fails
  with `InvalidRunOverrides` before any job.
- Sizes are strings as Terraform writes them (`"4"`, not `"4.0"`); the pair must be a valid Fargate pair
  with `max(qc, plan) + align <= fargate_max_vcpus` (8).
- The window is a JSON whole number from 5 Mb to 250 Mb.
- Each run's effective settings are in its execution output under `run_settings`.
  `cost_surface.py` reads the align size from the align job itself and the shard count from the
  `Call_Shard` rows, so it needs neither.
- The grid starts executions on reads already in `raw_reads/`, with no re-upload. The grid's windows
  (65, 33, 17, 9 Mb) are inside the bounds.

## The grid

- Sample: **REAL-HG003-30x-chr20** (paired, already in `raw_reads/`, gate PASS overnight). Synthetic
  runs are no use here: every stage is ~1 min of fixed overhead, so the surface would be flat.
- Full grid: 3 align sizes x 4 shard counts = **12 cells x 2 runs = 24 runs**.
- Cheaper alternative (axes are nearly independent: align time doesn't depend on shards and vice versa):
  vary one axis at a time from the default (4 vCPU, 2 shards): 3 + 4 - 1 = 6 cells, plus 2 corner cells
  to check that the costs really add up = **8 cells x 2 = 16 runs**. The script draws a partial grid as
  heatmaps (empty cells blank); the 3D view needs the full grid.

## Cost and time estimate

From the overnight runs (HG003 35x chr20: align 814 s at 4 vCPU using 96% of it, ~3,130 CPU-seconds;
one bcftools shard ~365 s at 2 vCPU), at the on-demand list prices in `stage_breakdown.py`:

| Align | Align time (est.) | Align cost | Calling (1 to 8 shards) | Run total |
|---|---|---|---|---|
| 1 vCPU / 4 GiB | ~53 min | ~$0.060 | ~$0.012 to $0.020 (fixed overhead + Fargate's 1-min minimum per shard) | ~$0.075-0.085 |
| 2 vCPU / 4 GiB | ~27 min | ~$0.050 | same | ~$0.065-0.075 |
| 4 vCPU / 8 GiB | ~14 min | ~$0.051 | same | ~$0.065-0.075 |

- Full grid: **~$1.7-2.0** (24 runs), ~10-12 h if run strictly one at a time (the AWS guard allows one
  run at a time). The 16-run alternative: **~$1.1-1.3**, ~7 h.
- Fargate Spot is the queue's first choice, so the real bill is likely lower; `stage_breakdown.py`
  prices every job on-demand (an upper bound) and doesn't yet record whether a job ran on Spot.
  `cost_surface.py --cost-factor` rescales a whole figure, not individual jobs; recording the capacity
  provider per job is a small follow-up if the Spot valley is to be drawn from data.
- Check before starting: month-to-date spend well under the guard's $6 new-run limit (the grid adds
  ~$2) and no other run in flight.

## Collecting and drawing

```bash
python scripts/query_pipeline_duration.py --stages --stages-csv grid.stages.csv --csv grid.csv   # needs SFN_ARN
python scripts/figures/cost_surface.py grid.stages.csv --durations grid.csv \
    --out docs/figures/cost-surface --surface --table docs/figures/csv/cost-surface.csv
```

The stage rows must come from a `stage_breakdown.py` that counts each Map shard as its own row (fixed
in the same PR as this note; before it, a run's shards after the first were missing from its cost).

## As run

- **The full grid:** all 12 cells x 2 runs, plus a third run of 4 vCPU x 1 shard because one of its runs
  waited 548 s in the queue. 25 runs in all, on dev-local = main-6431940 (with #204), with the quality gate
  in fail mode.
- **Windows:** 65, 33, 17 and 9 Mb gave 1, 2, 4 and 8 shards, checked on every run.
- **Six runs at a time, not one:** the user raised both Fargate caps to 48 vCPU for the grid.
  - Concurrent runs of one sample share every per-sample key (stats, run-record parts, the CRAM), and the
    gate reads the latest run record. So each of the 6 slots got its own sample ID, `REAL-HG003-30x-chr20-p1`
    to `-p6`: server-side S3 copies of the reads.
  - The copies carried an invalid `gate-profile`, so the run each copy triggered stopped at
    `Check_Gate_Profile` before any job.
  - An ID never had two runs at once, and a cell's second run started only after its first had succeeded.
- **Checks on every run:**
  - `run_settings`, the align job's real size and the number of shard jobs;
  - the gate verdict, and the SNP count within 0.1% of the sample's earlier run;
  - a time under 2x the estimate;
  - a watcher that paused new starts if any job sat in RUNNABLE for over 2 min.
- **What stopped it:**
  - The first solo run failed in the gate on a double-encoded run-record part. #204 fixed it, and the run
    was repeated.
  - The 548 s Spot placement wait paused new starts until the coordinator chose to continue.
- **Times:** align took about 35% longer than the estimate above (about 1,150 s at 4 vCPU, against 848 s
  the night before). Every run still finished within 2x its estimate.
- **Cost:** about $2.18 for the 25 runs at on-demand list prices (`stage_breakdown.py`). The real bill is
  lower where jobs ran on Fargate Spot.
