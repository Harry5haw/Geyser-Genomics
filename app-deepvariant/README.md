# DeepVariant stage (`app-deepvariant/`)

An alternative to the bcftools `variants` stage: [Google DeepVariant](https://github.com/google/deepvariant)
`1.10.0`, on an NVIDIA GPU through CUDA. It has its own image because the GPU base image alone is
about 10 GB.

| | |
|---|---|
| Reads | `alignments/{id}.cram` + `.crai` (or `alignments/{id}.bam`, for imported data), `reference/{name}` (+ `.fai` if stored) |
| Writes | `variants_deepvariant/{id}.vcf.gz`, beside the bcftools `variants/{id}.vcf.gz`; `stats/{id}/bcftools.stats.deepvariant.txt` (`bcftools stats -f PASS`: RefCall candidates excluded), also as the sample's `stats/{id}/bcftools.stats.txt` when DeepVariant is the run's caller (`PIPELINE_CALLER=deepvariant`) |
| Command | `/opt/geyser-venv/bin/python deepvariant_task.py <id> <reference_name>` |
| Metrics | `DeepVariant` (whole stage) plus `DeepVariant.MakeExamples`, `DeepVariant.CallVariants` and `DeepVariant.PostprocessVariants` |

Storage and metrics come from `app/storage.py`, `app/metrics.py` and the helpers in `app/tasks.py`, which are copied into the image unchanged. Local and S3 storage both work the same way they do for every other stage.

The stage indexes the alignment it downloaded (`samtools index`, seconds) rather than trusting a stored `.crai`: after a re-run that failed between its two uploads, the stored index could belong to the previous file.

## Run it locally

```bash
# Check the GPU is visible to Docker first (Docker Desktop, WSL2 backend)
docker run --rm --gpus all nvidia/cuda:12.3.2-base-ubuntu22.04 nvidia-smi

docker build -f Dockerfile -t geyser-app .
docker build -f app-deepvariant/Dockerfile -t geyser-deepvariant .
python scripts/make_test_data.py
python scripts/run_local.py --sample SYNTH001 --reference synthetic_ref.fa --caller both --check

# Real data: NA12878 chr20:10.0-10.1 Mb with GIAB truth (about 5 MB download)
python scripts/fetch_dv_testdata.py
python scripts/run_local.py --sample NA12878_S1 --reference ucsc.hg19.chr20.unittest.fasta \
    --caller deepvariant --skip-to-variants --check

# Whole chromosome: HG003 chr20 35x with GIAB v4.2.1 truth (~1.34 GB download, ~1.2 GB on disk)
python scripts/fetch_dv_testdata.py --dataset hg003-chr20
python scripts/run_local.py --sample HG003_chr20 --reference GRCh38_chr20.fa \
    --caller deepvariant --skip-to-variants --check --dv-memory 12g
```

**Memory.** For a whole chromosome, pass `--dv-memory` to cap the container.
- Uncapped, CPU `call_variants` took about 11.6 GB of the 16 GB Docker VM and starved Windows.
- 8 GB is too small for 16 `make_examples` shards: they thrash and fail.
- 12 GB works, with `DEEPVARIANT_EXTRA_ARGS=--call_variants_extra_args=batch_size=256` (default batch size: 1024).

Settings you can change. Set these on the host; `run_local.py` passes them into the container.

| Variable | Default | |
|---|---|---|
| `DEEPVARIANT_EXTRA_ARGS` | (none) | extra `run_deepvariant` flags, for example `--disable_small_model=true` |
| `DEEPVARIANT_SHARDS` | CPUs in the container | `make_examples` parallelism |
| `DEEPVARIANT_TIMEOUT_SECONDS` | no limit | stop the wrapper and fail the stage after this long |
| `DEEPVARIANT_IDLE_TIMEOUT_SECONDS` | no limit | stop it after this long with no output at all (it looks hung) |
| `DEEPVARIANT_FATAL_GRACE_SECONDS` | 120 | after a fatal error line (`ResourceExhaustedError` / `RESOURCE_EXHAUSTED`, usually out of GPU memory), stop it once it has been silent this long without exiting |
| `DEEPVARIANT_MODEL_TYPE` | `WGS` | |
| `DEEPVARIANT_CHECKPOINT` | on | `0` turns off keeping `make_examples` output for a retry (below) |

**A hung wrapper no longer blocks the stage.** On 1 Nov 2026, `call_variants` ran out of GPU memory and `run_pangenome_aware_deepvariant` never exited. The stage now runs the wrapper in its own process group and watches its output. When a limit is reached, it stops the wrapper's whole group (including steps that outlive the wrapper) and fails with the reason (`WrapperStopped`).
- **Fatal errors:** a wrapper still printing after a fatal line counts as alive. TensorFlow's `Allocator (GPU_0_bfc) ran out of memory trying to allocate ...` lines are warnings that also appear in successful runs, so they don't count.
- **Defaults:** the overall and idle limits are off by default, because whole-genome steps can be long and silent. The cloud job sets `DEEPVARIANT_TIMEOUT_SECONDS=3300` (below its 3600 s Batch attempt timeout) and `DEEPVARIANT_IDLE_TIMEOUT_SECONDS=900`.
- **Settings:** a value that isn't a non-negative number fails the stage before any download. `run_local.py` passes all three variables through.

**A retry skips `make_examples`.** `make_examples` is most of the run: 936 of 1,241 s for pangenome-aware DeepVariant on HG003 chr20. Neither Google wrapper can skip a step, so the stage keeps what it needs for a retry itself:
- **When:** the wrapper fails or is stopped after `make_examples` has finished, i.e. after the wrapper has started `call_variants`.
- **What is kept:** `make_examples`' output files and the remaining step commands, exactly as the wrapper printed them, under `intermediate/deepvariant-checkpoints/<sample>/<caller>/`. The data lake's lifecycle rule expires that prefix.
- **On the next run:** if the inputs and settings match, the stage downloads those files and runs only `call_variants` and `postprocess_variants` (moved to the new work directory), each under the same stop limits. Matching means the same DeepVariant version, alignment and reference (name and size), graph, shard count, model type and `DEEPVARIANT_EXTRA_ARGS`. The stop limits are not part of the match.
- **After use:** a used checkpoint is marked consumed by overwriting its manifest, because the cloud role cannot delete objects. A checkpoint made with other settings is ignored, and the stage runs in full.
- **Off switch:** `DEEPVARIANT_CHECKPOINT=0`.

There is no GPU? Build the CPU variant (`--build-arg DV_TAG=1.10.0 -t geyser-deepvariant-cpu`) and pass `--dv-image geyser-deepvariant-cpu --no-gpu`.

**Where the GPU actually helps.** Of DeepVariant's three steps, only `call_variants` uses the GPU. `make_examples` and `postprocess_variants` run on the CPU. Since 1.8, a "small model" inside `make_examples` also calls the easy sites on the CPU, so `call_variants` only sees the harder ones. The stage records each step separately.

Measured results (full tables in the main README):

| Input | `call_variants` | Whole stage |
|---|---|---|
| Whole chr20, defaults | 128 s on the GPU vs 427 s on the CPU (3.3×) | 7.8 min vs 12.9 min (1.7×) |
| 100 kb slices | 5–6 s slower on the GPU (CUDA start-up) | |

`make_examples` takes about 300 s either way.

## Pangenome-aware mode (`--pangenome`)

The same stage can run Google's [pangenome-aware DeepVariant](https://github.com/google/deepvariant/blob/r1.9/docs/pangenome-aware-wgs-bwa-case-study.md)
instead (issue #51). It reads the same linear-reference BAM and adds a pangenome graph: at each candidate site it
builds pileups from the pangenome's haplotypes as well as the sample's reads, which helps most where short reads
mismap (segmental duplications). Output stays in GRCh38 coordinates.

| | |
|---|---|
| Image | `app-deepvariant/Dockerfile` built with `--build-arg DV_TAG=pangenome_aware_deepvariant-1.10.0-gpu` (tag it `geyser-deepvariant-pangenome`). For CPU only, use `pangenome_aware_deepvariant-1.10.0` and `run_local.py --no-gpu`. |
| Reads | `alignments/{id}.cram` (or `.bam`), `reference/{name}` and `pangenome/{graph}.gbz` |
| Writes | `variants_pangenome/{id}.vcf.gz` |
| Command | `/opt/geyser-venv/bin/python deepvariant_task.py <id> <reference_name> --pangenome <graph>` |
| Metrics | `PangenomeDeepVariant` plus `.MakeExamples`, `.CallVariants`, `.PostprocessVariants` |
| Hardware | `call_variants` on the GPU, like linear DeepVariant. On a 10 GB card it needs `--call_variants_extra_args=batch_size=256`, because the default 1,024 runs out of GPU memory. (NVIDIA Parabricks' separate GPU version needs 16 GB of VRAM. Google's image does not.) |

```bash
docker build -f app-deepvariant/Dockerfile --build-arg DV_TAG=pangenome_aware_deepvariant-1.10.0-gpu \
    -t geyser-deepvariant-pangenome .
# HG003 chr20, plus HPRC's chr20 graph converted to hprc-v1.1-mc-grch38.chr20.gbz (72 MB) with vg in Docker
python scripts/fetch_dv_testdata.py --dataset hg003-chr20 --pangenome
DEEPVARIANT_SHARDS=6 DEEPVARIANT_EXTRA_ARGS="--call_variants_extra_args=batch_size=256" \
python scripts/run_local.py --sample HG003_chr20 --reference GRCh38_chr20.fa \
    --caller pangenome --skip-to-variants --check --dv-memory 14g
# Score every caller's VCF with hap.py (vcfeval, PASS only, GIAB confident regions)
python scripts/benchmark_happy.py --sample HG003_chr20 --reference GRCh38_chr20.fa --region chr20
```

`--caller all` runs bcftools, linear DeepVariant and pangenome-aware DeepVariant in one go. The pangenome container
gets `--shm-size 12g` by default (`--pg-shm-size`), as in Google's case studies, and the `--dv-memory` cap.

**Which graph.** `--pangenome` (or `--pangenome chr20`) downloads HPRC's per-chromosome `chr20.vg` and converts it with
vg v1.55.0 (`vg convert -f`, then `vg gbwt -G --gbz-format --set-reference GRCh38 --set-reference CHM13`). The result
has the same 46 samples, 90 haplotypes and `GRCh38#0#chr20` path as the whole-genome graph. `--pangenome full` fetches
the whole-genome graph (3.29 GB). Loaded, it takes about 10.7 GB of shared memory plus about 2 GB per `make_examples` worker,
which ran out of memory in a 22 GB container on a 32 GB desktop.

**Measured on HG003 chr20** (RTX 3080, 6 workers): 936 s `make_examples`, 257 s `call_variants` (231,085 examples),
21 s `postprocess_variants`, 20.7 min in all, peak container memory about 8.4 GB. Accuracy and the comparison with linear
DeepVariant are in the main README.

**Known issue.** When `call_variants` ran out of GPU memory, `run_pangenome_aware_deepvariant` hung instead of
exiting. Give the cloud job a timeout.

**The BAM must be duplicate-marked.** Google's case-study BAM (`fetch_dv_testdata.py --dataset hg003-chr20`) already
is, and so are the pipeline's own BAMs: the `align` stage marks duplicates with `samtools markdup`.

## Tests

- `tests/test_deepvariant_task.py` runs on every push. `run_deepvariant` and `samtools` are faked, so it needs no Docker or GPU.
- `tests/test_pangenome.py` runs on every push too: the pangenome-aware stage (faked tool), the `pangenome`/`all` callers in `run_local.py`, the graph download entry and the hap.py benchmark helpers.
- `tests/test_e2e_deepvariant.py` is opt-in:
  - `GEYSER_GPU_E2E=1` uses the GPU image;
  - `GEYSER_DV_E2E=1` uses the CPU image;
  - `GEYSER_DV_TESTDATA=<root>` also runs the real-data slice.
- `tests/test_e2e_pangenome.py` is opt-in (`GEYSER_PG_E2E=1`): the real pangenome-aware image on synthetic data. `make_test_data.py --pangenome` builds a small graph of the synthetic reference (vg in Docker): the reference as the `GRCh38` path, plus six panel haplotypes. Most of them carry each planted SNP, and half of them carry five decoy SNPs the sample does not have. The test requires exactly the 10 planted SNPs, so a decoy called from the graph alone fails it. It takes about a minute on an RTX 3080. The image is 38 GB, so it is not run in CI.
- In CI, the CPU-image E2E runs only on pull requests and when started manually. GitHub runners have no GPU. The CPU image runs the same commands and wiring, so CI still catches integration breakage. The GPU path is tested locally before merging.

## Cloud: what the infrastructure work will need (not done here)

`infrastructure/` is owned by the infra branches. This is the list for them:

1. **Image.** Use a separate ECR repo, `geyser-deepvariant`, with a lifecycle policy. The image is about 10 GB, so `deploy-platform.yml` either needs a second build/push step on a runner with enough disk, or the image is pushed by hand.
2. **Batch job definition.** EC2 platform, because Fargate has no GPUs. Settings:
   - `resourceRequirements`: `GPU=1`, `VCPU=16`, `MEMORY≈60000` on `g5.4xlarge`. Sizing reasons are in [AWS sizing](#aws-sizing-from-local-measurements) below;
   - queue: `gpu_queue` (from `batch_gpu.tf`);
   - command: `/opt/geyser-venv/bin/python deepvariant_task.py <srr_id> <reference_name>`;
   - environment: `BUCKET_NAME` (plus `AWS_REGION`). Optionally also `DEEPVARIANT_SHARDS` set to the vCPU count;
   - set a job timeout from the run times below.
3. **Disk.** The default ECS GPU AMI root volume is far too small. The image is 9.8 GB compressed and 28 GB unpacked, and the stage copies the BAM to local disk. The compute environment needs a launch template with gp3 sized as below: about 100 GB for chromosome-scale inputs, 250 GB or more for a whole genome.
4. **IAM.** The job role needs:
   - `s3:GetObject` on `alignments/*` and `reference/*`;
   - `s3:PutObject` on `variants_deepvariant/*` and `stats/*`;
   - `cloudwatch:PutMetricData`.
5. **Networking.** `gpu_env` currently uses the private subnet. With the NAT gateway off by default (the cost-safe plan), it has no route to ECR or S3. Either move it to the public subnet with public IPs on launch, or add VPC endpoints (S3 gateway; ECR api/dkr interface endpoints, which also cost money).
6. **Step Functions.** After `Align`, add a `Parallel` state with two branches:
   - the existing bcftools `variants` branch;
   - a `Pass` + `batch:submitJob.sync` branch on `gpu_queue`.

   Both branches catch `States.ALL` and route to `Notify_Failure`. When this lands, update `scripts/run_local.py` and `tests/test_consistency.py` in the same change.
7. **Cost.** GPU instances bill by the hour while they run. Keep `min_vcpus = 0`, and set the AWS Budget alert before the first cloud run.

## AWS sizing (from local measurements)

Measured on 1 Nov 2026 with DeepVariant 1.10.0 and the HG003 whole-chr20 35x run (the commands are in the main README).

**Test machine:**
- Docker Desktop on WSL2, 16 vCPUs, 16 GB VM;
- NVIDIA RTX 3080 (10 GB) for the GPU runs;
- `make_examples` ran with 16 shards.

Each figure below is labelled:
- **[measured]**: measured on this machine;
- **[extrapolated]**: scaled from those measurements;
- **[spec]**: taken from published hardware specifications.

None of the AWS figures has been run on AWS yet.

### What a chr20-sized job needs

| Resource | Figure | Basis |
|---|---|---|
| `make_examples` time | ~300 s with 16 shards, about **80 vCPU-minutes** in total. It runs entirely on the CPU, on either image. | [measured] |
| `call_variants` time, GPU | 128 s for 79,759 examples (defaults). About 15 s of that is fixed CUDA/model start-up, so roughly 700 examples/s. | [measured] |
| `call_variants` time, CPU (16 vCPU) | 427 s for the same examples, roughly 190 examples/s. | [measured] |
| `postprocess_variants` | 17–22 s, CPU | [measured] |
| Whole stage | **7.8 min on GPU, 12.9 min CPU-only** (defaults) | [measured] |
| Memory | 16 shards thrashed and failed under an 8 GB cap but ran fine at 12 GB. Uncapped, the CPU-only run peaked at about 11.6 GB (batch 1024). Plan on about **0.6 GB per shard plus 2 GB**. That fits both results: 11.6 GB for 16 shards, and over 8 GB needed. | [measured] |
| GPU memory | TensorFlow reserves all free VRAM. The run fitted in the roughly 6 GB the desktop left free on the 10 GB card. | [measured] |
| Input | 1.1 GB BAM, 65 MB reference | [measured] |
| Intermediate files | **not measured.** Run `du` on the intermediate directory before sizing whole-genome volumes. | — |

### What that means for the compute environment

- **The current instance types are too small for `make_examples`.**
  - `gpu_env` offers `g4dn.xlarge`, `g5.xlarge` and `g5.2xlarge`, which have 4–8 vCPUs.
  - `make_examples` scales with vCPUs and is already the largest step on the GPU. On 4 vCPUs it would take about 20 minutes for chr20 [extrapolated], with the GPU idle the whole time.
  - **Add `g5.4xlarge`** (16 vCPU, 64 GB, A10G 24 GB). It matches the measured setup and should give run times close to the ones above.
  - `g4dn.4xlarge` (16 vCPU, 64 GB, T4 16 GB) is the cheaper option.
- **GPU type** [spec]:
  - A10G (g5): about 31 TFLOPS FP32, roughly the same class as the RTX 3080 (about 30). Expect a similar `call_variants` speed.
  - T4 (g4dn): about 8 TFLOPS FP32. Expect `call_variants` to run about 2–3 times slower than measured here.
  - Either GPU has enough VRAM.
- **Memory:** g4dn and g5 have 4 GB per vCPU, which comfortably covers about 0.6 GB per shard. In the job definition, leave about 4 GB of the instance's memory for the ECS agent and the OS.
- **CPU-only might be cheaper at chromosome scale.** CPU-only took 12.9 min against 7.8 min on the GPU. A 16-vCPU compute-optimised Spot instance may cost less per run than an on-demand GPU instance. Compare current eu-west-2 prices before committing to GPU. The same Dockerfile builds the CPU image (`--build-arg DV_TAG=1.10.0`).
- **Cold starts:** each new instance pulls a 9.8 GB image, which adds several minutes before the first job [not measured]. With `min_vcpus = 0`, every run pays this. A pre-pulled AMI would remove it later.

### Whole genome (later) [extrapolated]

chr20 is about 1/45 of the genome, so these are linear scale-ups. Real genomes are not perfectly uniform, so treat them as order-of-magnitude.

| | 30–35x whole genome |
|---|---|
| `make_examples` | about **60 vCPU-hours** (about 4 h on 16 vCPUs) |
| `call_variants`, A10G / RTX 3080-class | about 3.6 M examples at about 700/s, so about **1.5 h** |
| `call_variants`, T4 | about 3–4 h [spec-scaled] |
| `call_variants`, 16 vCPU CPU-only | about 5 h |
| Input BAM | **about 47 GB** (HG003 35x is 46.8 GB) [measured listing] |
| Disk | 47 GB BAM copy, plus intermediates (unmeasured), plus 28 GB unpacked image and its layers. **250 GB or more of gp3.** |

At this scale a single GPU instance spends most of its time in `make_examples` with the GPU idle. A better shape would be two jobs:
- `make_examples` on a large CPU Spot instance;
- `call_variants` and `postprocess` on the GPU.

That means splitting this stage in two and passing intermediates through S3. Do that only once whole genomes are actually needed.

### The existing bcftools stage

bcftools took **409 s** for chr20, single-threaded [measured]. That is about **5 h for a whole genome** [extrapolated] on the current Fargate job (2 vCPU, 4 GB). Set the Batch and Step Functions timeouts for that, or parallelise `variants` by region, before running whole genomes.
