<!--
Public copy of amendment A1 to the RP-1 pre-registration (docs/prereg/rp1-alphagenome.md). The original was
committed to the author's private dissertation repository as prereg/amendments/rp1-A1-batch-size.md,
commit 5a76e0fd6d06dfc16cf41780f40c481c736183ab, annotated tag prereg-rp1-A1, 2026-11-04T08:54:33Z,
before any confirmatory DeepVariant call existed.
SHA-256 of the original file: 2f983d1a9b8afa677e2af950570d47edea3bd261ea4b8cc3328aa2fb7209b2cd
Everything below this comment is byte-identical to that file.
-->
# RP-1 amendment A1: DeepVariant call_variants batch size 256 on the local GPU

- **Pre-registration:** `prereg/rp1-alphagenome.md`, tag `prereg-rp1-v1` (commit 9ca1bc8, 2026-11-04T01:26:18Z).
- **Amendment date:** 2026-11-04, drafted before any confirmatory DeepVariant call exists.
- **Status:** approved by the author on 2026-11-04 and committed and tagged `prereg-rp1-A1` before the first confirmatory call.

## What changes

**§1 Data, "Calls"**
- **Before:** "DeepVariant 1.10.0 (GPU image built from `app-deepvariant/Dockerfile`, `DV_TAG=1.10.0-gpu`), WGS model, default settings, on each sample × chromosome."
- **After:** the same, except that **call_variants runs with `batch_size=256`** (`--call_variants_extra_args=batch_size=256`) instead of the default 1024. It applies to **every** confirmatory cell, the same setting throughout.

Nothing else changes: the image (sha256:7318378eff7f…), the model (WGS), make_examples, postprocessing, the samples, chromosomes, scores, hypotheses, designs, seeds, endpoints, success criteria, multiple testing and reporting. The runtime-only settings are already logged in `PROCEDURE.md`: `--num_shards=8`, `writer_threads=4`, a 10 GB container memory cap, and a WSL page-cache drop before each cell.

## Why

The confirmatory calls run locally on an NVIDIA RTX 3080 (10 GB VRAM), on a 32 GB host shared with other work. A host-memory guard stops any step when Windows "Available MBytes" falls below 6 GB. That's the threshold set after the 01:53 UTC incident, when memory exhaustion got processes killed.

Twice, the default batch size couldn't run within that limit (`geyser-data/rp1/PROCEDURE.md`, `calls.log`, `calls2.log`):

| Attempt | Cell | Available memory | Outcome |
|---|---|---|---|
| 1, 02:20-02:28 UTC | HG003_chr19 | ~10 GB in make_examples | call_variants started 16 writer processes (GPU autodetect); free memory fell to 5.1 GB; stopped |
| 2, 02:29-02:37 UTC, with `writer_threads=4` | HG003_chr19 | 15.7 GB at the start → 9.9 GB in make_examples | call_variants started ("Total 4 writing processes started"). At batch 1024, TensorFlow logged GPU allocator out-of-memory retries ("ran out of memory trying to allocate 2.68GiB … 7.36GiB"), and host available memory fell to 4.4 GB; stopped |

- **On the 10 GB card, a 1024-example batch doesn't fit the GPU.** TensorFlow's out-of-memory retries spill to host memory.
- **The project already runs DeepVariant's call_variants at batch 256 on this card:** the pangenome-aware runs (README; `DEEPVARIANT_EXTRA_ARGS=--call_variants_extra_args=batch_size=256`) and the CPU timing runs.
- **Alternatives, rejected:**
  - relaxing the memory stop line (that's what led to the 01:53 incident);
  - moving the calls to the cloud GPU (cost and scheduling, for now).

## Evidence that batch size doesn't change the calls beyond GPU nondeterminism

**Source:** PR #216, the local DeepVariant batch × shards grid. Same image family (DeepVariant 1.10.0 GPU), same input (HG003 chr20, Google 35x case-study BAM), the same RTX 3080. Data: `docs/figures/csv/dv-batch-shards.runs.csv` (repo) and `geyser-data/dv-grid/results/dv-grid.csv`.

- **Every run sent the same 79,759 examples to call_variants**, with the same small-model calls (143,219). Batch size only groups examples for the GPU; the examples, model and weights are identical.
- **PASS counts across all 13 runs** (batch 128, 256, 512 and 1024 × 4, 8 and 16 shards, plus a same-settings repeat):
  - SNPs 85,652-85,653;
  - indels 19,883-19,884;
  - PASS records 105,402-105,404.
- **That spread equals the run-to-run variation at fixed settings.** The same-settings repeat, b256-s16 vs b256-s16-r2, gave 105,403 vs 105,402. #216 reports it as "≤ 2 PASS records of 105,403", and no two runs wrote byte-identical records, not even the repeat.
- **Per setting:**

  | | Batch 256 | Batch 1024 |
  |---|---|---|
  | PASS records, by shard count | 105,403 / 105,404 / 105,403 (16 / 8 / 4 shards) | 105,404 / 105,403 / 105,402 |
  | Difference from the other batch size | | within ±2 |

- **Conclusion:** batch size 256 vs 1024 changes PASS calls by no more than DeepVariant's own GPU nondeterminism (±2 of ~105,000 records, ≈ 0.002%).
  - At that scale, a call that differs would move at most a couple of positives or controls per cell. That's negligible against the positives expected per cell (tens to hundreds) and the AUROC CI half-width (~±0.025 for H1).

## No confirmatory data exists yet

- **Calls:** no confirmatory DeepVariant call exists. Both attempts were stopped before any VCF was written (`geyser-data/rp1/calls/` holds only the two attempts' logs).
- **Scores:** no Atlas, CADD, phyloP or AVI value has been looked up for any confirmatory variant. The CADD and AVI files were downloaded or extracted, but nothing was read from them for any variant set.
- **Sets:** no positive or control set has been built.

So this amendment can't have been informed by confirmatory results.

## Effect on the analysis

None on the hypotheses, endpoints or criteria. The deviations table in the article lists A1 with this rationale. PROCEDURE.md records the cell-by-cell run settings.
