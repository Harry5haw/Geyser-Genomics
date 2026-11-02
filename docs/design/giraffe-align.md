# A vg giraffe align stage for the whole genome (design, #146)

**Status:** design only; nothing here is built. It sizes an `ALIGNER=giraffe` align stage for a whole human genome, so the cloud decision (instance class, budget) can be made from numbers. The headline measurements in #146 are on HG003 chr20. Every whole-genome number below is labelled **measured**, **published** or **estimated**, and the estimates say what they assume.

**Why giraffe at all:** on Google's whole-genome alignments of HG003 35x, scored on chr20 (#145), vg giraffe + linear DeepVariant has **half the errors** of BWA + DeepVariant: 186 against 378 SNP+indel errors in hap.py. In low-mappability regions, SNP errors fall from 257 to 46. Graph alignment is where the pangenome pays off (#51).

## 1. What a giraffe align job does

The same steps as the local benchmark (`scripts/benchmark_giraffe.py`), as one align job per sample:

1. **k-mer counts** of the reads: `kmc -k29`. Haplotype sampling needs them.
2. **Haplotype sampling:** `vg haplotypes` builds a per-sample graph from the pangenome GBZ, its `.hapl` index and the k-mer counts. It keeps the haplotypes closest to the sample, so mapping runs against a small, sample-specific graph.
3. **Mapping:** `vg giraffe` against the sampled graph. It builds that graph's distance and minimizer indexes in memory, then surjects the alignments onto GRCh38 paths (`--ref-paths`, BAM out).
4. **Post-processing, unchanged from today's align stage, in our image:** contig rename (`GRCh38#0#chr20` → `chr20`), `samtools fixmate -m | sort | markdup`, then CRAM + `.crai`. vg bundles samtools 1.10, which is too old for our CRAM/markdup flags, so this stays outside vg.
   - The rename only touches `@SQ` names, so it can be `samtools reheader` on the header alone. `benchmark_giraffe.py` keeps Google's `samtools view -h | sed` over the whole SAM, on purpose, so its timing matches the case study.

**Once per graph, not per sample:** the `.hapl` (built from the GBZ, its distance index and r-index) and the GRCh38 path list. HPRC publishes a `.hapl` for v1.1, and for **release 2** too: `hprc-v2.0-mc-grch38.hapl` (21.4 GB) and the benchmark graph's `hprc-v2.0-mc-grch38-eval.hapl` (21.1 GB). So neither needs a build here. For a graph without one, it's a one-off job.

The align stage writes the **same keys as today**: `alignments/{id}.cram` + `.crai`, the stats under `stats/{id}/`, and the run-record part with `aligner: "vg giraffe"`. Nothing downstream changes, except how the variants stage picks its flags (§5).

## 2. Measured: HG003 chr20, 35x, 19.0 M reads, 16 threads (local)

From `geyser-data/benchmarks/giraffe_HG003_chr20/*.json` (wall time and cgroup peak):

| Step | Wall | Peak memory | Per |
| :--- | ---: | ---: | :--- |
| kmc k-mer count (`-m8`) | 37 s | 7.4 GiB (set by `-m`) | sample |
| haplotype sampling + giraffe mapping | 546 s (mapping 462 s) | 1.5 GiB | sample |
| rename + fixmate \| sort \| markdup | 158 s | 2.3 GiB | sample |
| distance index | 39 s | 1.45 GiB | graph |
| r-index | 6 s | 0.5 GiB | graph |
| `.hapl` | 297 s | 0.9 GiB | graph |
| *for comparison:* bwa-mem2 align stage, same reads | 521 s | 7.4 GiB | sample |

**Graph files on disk (measured):**

| Graph | GBZ | Notes |
| :--- | ---: | :--- |
| HPRC v1.1 MC GRCh38, chr20 cut | 71.6 MB | `.dist` 84.5 MB, `.ri` 82.4 MB, `.hapl` 124.5 MB |
| HPRC v1.1 MC GRCh38, whole genome | 3.29 GB | |
| HPRC v2.0 MC GRCh38 eval, whole genome | 5.39 GB | held out: HG002, HG005, NA19240 |

**Reported by the benchmark terminal:**
- the v1.1 whole-genome GBZ takes **~10.7 GB of shared memory** in pangenome-aware DeepVariant (its `load_gbz_into_shared_memory` layout); it ran out of memory locally. vg giraffe or `vg haplotypes` holding the same GBZ may use a different amount;
- cutting chromosomes out of the r2 eval graph (`vg chunk`) reached **at least 12.9 GB and was still loading** when it was stopped, twice. The real peak is unknown; about 18 GB was an estimate.
- **Published file sizes:** the whole-genome `.hapl` is 21.4 GB (r2) and 21.1 GB (r2 eval). That's far above the ~5.7 GB scaled from chr20 below (r2 has about 5× v1.1's haplotypes), and it is the upper anchor for the sampling memory.

## 3. Estimated: whole genome, one 30-35x sample

**How the estimates scale:**
- chr20 is 64.4 Mb of GRCh38's 3.09 Gb, so 2.09%.
- The whole genome has ~48× chr20's reads (≈ 910 M reads for HG003 35x) and ~46× its graph (GBZ 71.6 MB → 3.29 GB).
- **Time scales with reads.** Per-read mapping cost is roughly the same genome-wide. Sampling and index building scale with the graph.
- **Memory is set by the graph, not the reads.** That's the useful asymmetry: a small read subset measures the whole-genome peak (§6).

| Step | Estimated wall at 16 vCPU | Estimated peak memory | Basis |
| :--- | ---: | ---: | :--- |
| kmc | ~30 min | 8-16 GiB (`-m`, configurable) | 37 s × 48 reads |
| haplotype sampling (v1.1) | ~1 h | **~20-30 GiB** | full GBZ in memory (~10.7 GB in DeepVariant's layout) + `.hapl` (~5.7 GB est.) + k-mer table |
| haplotype sampling (r2 eval) | ~1.5 h | **~35-45 GiB** | GBZ 1.6× v1.1; cutting it reached ≥ 12.9 GB (measured, still loading); published `.hapl` 21.1 GB |
| giraffe mapping | ~6 h | ~15-30 GiB | 462 s × 48 reads; the sampled graph's indexes in memory |
| rename + fixmate \| sort \| markdup | ~2 h | 8-16 GiB (sort cap) | 158 s × 48; I/O-bound |
| **align stage total** | **~9.5-10 h** | **peak ~30 GiB (v1.1), ~45 GiB (r2)** | vs bwa-mem2 ≈ 7 h (521 s × 48) |

Whole-genome indexes, once per graph:
- `.hapl` ~5.7 GB and `.dist` ~3.9 GB, about 48× the chr20 files;
- building them takes ~4-5 h at 16 threads, unless HPRC's published `.hapl` is used.

The memory ranges are the least certain numbers here, and the first thing §6 measures.

**Disk per job:**

| Item | Size |
| :--- | ---: |
| Reads (gzipped FASTQ, 35x) | ~70-80 GB, or **streamed** from S3 instead of downloaded |
| Graph files (v1.1 GBZ + `.hapl`) | ~9 GB |
| kmc database | ~10-20 GB |
| Sort temporaries (`-l 1`) | ~60 GB |
| CRAM out | ~15-20 GB |
| **Total** | **~100 GB streaming the reads, ~180 GB downloading them** |

## 4. Where it runs: Fargate or EC2

| Option | Fits? | On-demand, London | Spot | Notes |
| :--- | :--- | ---: | ---: | :--- |
| **Fargate** 16 vCPU / 64 GB | memory yes if the peak is ≤ ~60 GB; disk: 200 GiB ephemeral maximum | $1.07/h | FARGATE_SPOT, ~70% off | no new compute environment: the existing Fargate and Fargate Spot environments (the queue tries Spot first) and their guardrails apply; `fargate_spot_max_vcpus` / `fargate_max_vcpus` (8 each today) must allow a 16-vCPU job |
| Fargate 16 vCPU / 120 GB (the Fargate maximum) | memory headroom for r2 | $1.36/h | as above | the same disk limit |
| **EC2** `r6i.4xlarge` (16 vCPU, 128 GiB) | yes, any disk (EBS) | $1.184/h | ~$0.36/h (2 Nov 2026 snapshot) | a **new CPU Batch EC2 environment**: infra the user plans and applies; the boundary forbids Claude compute-env changes |
| EC2 `r7i.4xlarge` (16 vCPU, 128 GiB) | yes | $1.243/h | ~$0.13/h (snapshot) | spot prices move; Batch's `SPOT_PRICE_CAPACITY_OPTIMIZED` picks |
| EC2 `r6i.8xlarge` (32 vCPU, 256 GiB) | yes, with room to spare | $2.368/h | ~$0.74/h | halves the mapping time; only if the memory measurement says so |

Prices: on-demand from the AWS Pricing API (EU London, Linux, shared tenancy). Spot from `describe-spot-price-history` on 2 Nov 2026, cheapest AZ. Fargate from the rates in `scripts/query_pipeline_duration.py`.

**Recommendation:**
- **Fargate first, if the measured peak fits.** If §6 shows ≤ ~60 GB for the graph in use (likely for v1.1), the align job runs on the existing Fargate environment at 16 vCPU / 64 GB, with streamed reads and a 200 GiB ephemeral disk. The infra change is then a job-definition size (the `stage_resources` pattern) and `fargate_max_vcpus` ≥ 16, not a new compute environment.
- **Otherwise EC2:** an `r6i/r7i.4xlarge`-class spot Batch environment. It's also the right home for a future 32-vCPU job, and it's where the GPU environment already lives (EC2, `batch_gpu.tf`).
- The Fargate claim in the brief ("tops out at 120 GB, so likely EC2") holds only if the peak is above ~120 GB. Nothing measured so far points there. The binding Fargate limit is more likely the **200 GiB disk**, hence streaming the reads.

**Faster, same cost: scatter the mapping.** Sampling runs once per sample and writes the sampled GBZ to S3. The reads are then split into N chunks, a Map state maps each on its own job, and a merge step runs `samtools merge` + markdup: the same shape as the variant-calling shards (#140). Mapping drops from ~6 h to ~6/N h of wall time at roughly the same vCPU-hours. Each chunk job needs only the sampled graph's memory. This is a second step, after a single-job version works.

## 5. Cost per sample, and the budget

**Per sample (align stage only, ~10 h at 16 vCPU):**

| Option | Cost per sample |
| :--- | ---: |
| EC2 `r6i.4xlarge`, on-demand | ≈ **$12** |
| EC2 `r6i.4xlarge`, spot (snapshot) | ≈ **$3.5** |
| Fargate 16 vCPU / 64 GB, on-demand | ≈ **$11** |
| Fargate 16 vCPU / 64 GB, FARGATE_SPOT | ≈ **$3-3.5** |

On top of that:
- S3 storage for the graph files: ~9-15 GB, ≈ $0.25-0.35/month.
- Variant calling: today's bcftools variants stage (scattered by region since #140), or DeepVariant on GPU. `app-deepvariant/README.md` extrapolates a whole genome to about 4 h of `make_examples` on 16 vCPUs plus about 1.5 h of `call_variants` on an A10G, with no cost figure yet.

**This doesn't fit the guardrails as they stand:**
- the project budget is $20, with the kill switch at $8 actual spend, and the local hook starts new runs only below $6;
- a single on-demand whole-genome giraffe run would trip the kill switch on its own.

A whole-genome cloud run is therefore **a budget decision for the user**: spot only, one sample, after the local measurements. It would also need the guard thresholds raised by the user for that run.

**Variant calling on giraffe reads (decided in #145/#146):**
- Linear DeepVariant needs Google's VG flags (`min_mapping_quality=0,keep_legacy_allele_counter_behavior=true,normalize_reads=true`, `VG_LINEAR_DV_ARGS` in `scripts/benchmark_giraffe.py`).
- MAPQ ≥ 5 helps pangenome DeepVariant on BWA reads but hurts it on giraffe reads.
- So the variants stage must pick flags **per alignment**. It reads `aligner` from the Align stage's run-record part (already recorded, #105); the `@PG` line in the CRAM header is the fallback. The rule belongs in `tasks.py`, next to `ALIGNER_INDEX_EXTS`.
- **bcftools on giraffe reads has not been measured.** giraffe's MAPQ calibration differs from bwa's, so measure it before making giraffe the default for the bcftools path.

## 6. Measure before building (local, ~free)

1. **Peak memory, whole genome, v1.1:**
   - Run the full giraffe job on a **1-2% random subset** of HG003's whole-genome reads (sampling + mapping + post-processing), under `/usr/bin/time -v` / cgroup peak as `benchmark_giraffe.py` does.
   - Memory is graph-bound, so this measures the whole-genome peak. Time × (all reads / subset reads) gives the wall estimate.
   - The host has 32 GB with WSL capped at 24 GB (`.wslconfig`). Above that, raise the WSL cap for the run, or stop and go to the cloud measurement in step 4.
2. **The same for the r2 eval graph:** expected to be the larger one.
3. **Disk:** watch the work directory's high-water mark during step 1, and scale the reads-proportional parts.
4. **Only then, if a local run can't fit:** one cloud measurement run on an `r6i.4xlarge`-class spot instance (≈ $0.5-1 for a subset). That's the user's budget call.

## 7. Image, files, code

- **Image:** a separate `app-giraffe/` image (vg 1.55.0 static binary + kmc 3.2.4 + our samtools, sharing `app/tasks.py`), like `app-deepvariant/`, rather than adding vg to `geyser-app`.
  - `geyser-app` was just slimmed to 87 MB compressed (#142); every bwa-mem2 and QC job would pay vg's size in start-up time.
  - The giraffe job's start-up is negligible next to ~10 h.
- **Reference files:** under `reference/pangenome/`: `<graph>.gbz`, `<graph>.hapl`, `<graph>.GRCh38.paths.txt`.
  - Uploaded once, from HPRC's published files where they exist, like the bwa-mem2 index (#115).
  - The align job downloads them; at ~9 GB that's under a minute in-region.
- **Code** (#146's remaining tasks):
  - `ALIGNER=giraffe` in `prepare_aligner` / `align_command`: kmc → `vg haplotypes` → `vg giraffe` → rename → the existing fixmate | sort | markdup;
  - its index files in `ALIGNER_INDEX_EXTS`;
  - the variants-stage flag rule;
  - tests with a tiny graph for the Docker E2E. `make_test_data.py --pangenome` (#167) already builds one: the synthetic reference as the `GRCh38` walk plus six panel haplotypes. The giraffe test additionally needs its `.dist`/`.min` (and a `.hapl` for sampling), which take seconds on that graph.
- **Infra** (a separate PR, reviewed plan): a giraffe job definition, its `stage_resources` entry, and either `fargate_spot_max_vcpus` / `fargate_max_vcpus` ≥ 16 or the EC2 environment from §4. Raising those caps raises the worst-case spend rate the guardrails were sized for (`docs/design/claude-guardrails.md`), so it's the user's call too. The state machine is unchanged unless the scattered mapping (§4) is built.

## Open questions

- The measured whole-genome peak for v1.1 and r2 (§6) decides Fargate against EC2.
- Is HPRC's published v1.1 `.hapl` compatible with vg 1.55.0, or must it be rebuilt? The `.hapl` format changed across vg releases.
- bcftools accuracy on giraffe reads.
- Paired-end handling in `vg giraffe` (`-f R1 -f R2`) is already exercised: every run in #129 was paired (9.5 M HG003 pairs, `-L 3000`, Google's flags). A synthetic `--paired` E2E is still worth adding.
