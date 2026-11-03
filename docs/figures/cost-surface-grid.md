# Design note: the grid of cloud runs behind the cost x turnaround figure

Status: **design only, no runs made.** Whether and when to run the grid is the user's decision (it costs
money and needs one infra apply). `scripts/figures/cost_surface.py` is written and tested against the
output this grid would produce (`query_pipeline_duration.py --stages --stages-csv`).

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

**Recommended: one small infra PR first** (`infra/run-overrides`): a first `Pass` state merges optional
execution-input fields over Terraform's defaults,
`States.JsonMerge(States.StringToJson('{"align_vcpu":"4","align_memory_mb":"8192","variant_region_window":"50000000"}'), $, false)`,
and the align and region states read `$.align_vcpu` / `$.variant_region_window` instead of the
template values. Uploads via the trigger Lambda send no overrides, so behaviour is unchanged; the grid
starts executions directly (`aws stepfunctions start-execution --input '{"srr_id": ..., "reference_name":
"chr20.fa", "align_vcpu": "2", ...}'`) on reads already in `raw_reads/`, with no re-upload.
Expected plan: **0 add / 1 change / 0 destroy** (the state machine, in place). An invalid vCPU/memory pair
then fails at SubmitJob (Notify_Failure) instead of at plan time; the tests in
`tests/test_state_machine_local.py` would cover the merge.

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
