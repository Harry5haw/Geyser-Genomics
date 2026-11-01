# DeepVariant stage (`app-deepvariant/`)

An alternative to the bcftools `variants` stage: [Google DeepVariant](https://github.com/google/deepvariant)
`1.10.0`, on an NVIDIA GPU through CUDA. It has its own image because the GPU base image alone is
about 10 GB.

| | |
|---|---|
| Reads | `alignments/{id}.bam`, `reference/{name}` (+ `.fai` if stored) |
| Writes | `variants_deepvariant/{id}.vcf.gz`, beside the bcftools `variants/{id}.vcf.gz` |
| Command | `/opt/geyser-venv/bin/python deepvariant_task.py <id> <reference_name>` |
| Metrics | `DeepVariant` (whole stage) plus `DeepVariant.MakeExamples`, `DeepVariant.CallVariants` and `DeepVariant.PostprocessVariants` |

Storage and metrics come from `app/storage.py`, `app/metrics.py` and the helpers in `app/tasks.py`, which are copied into the image unchanged. Local and S3 storage both work the same way they do for every other stage.

The stage indexes the BAM itself (`samtools index`), because the bcftools path never needed an index.

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
| `DEEPVARIANT_MODEL_TYPE` | `WGS` | |

There is no GPU? Build the CPU variant (`--build-arg DV_TAG=1.10.0 -t geyser-deepvariant-cpu`) and pass `--dv-image geyser-deepvariant-cpu --no-gpu`.

**Where the GPU actually helps.** Of DeepVariant's three steps, only `call_variants` uses the GPU. `make_examples` and `postprocess_variants` run on the CPU. Since 1.8, a "small model" inside `make_examples` also calls the easy sites on the CPU, so `call_variants` only sees the harder ones. The stage records each step separately.

Measured results (full tables in the main README):

| Input | `call_variants` | Whole stage |
|---|---|---|
| Whole chr20, defaults | 128 s on the GPU vs 427 s on the CPU (3.3×) | 7.8 min vs 12.9 min (1.7×) |
| 100 kb slices | 5–6 s slower on the GPU (CUDA start-up) | |

`make_examples` takes about 300 s either way.

## Tests

- `tests/test_deepvariant_task.py` runs on every push. `run_deepvariant` and `samtools` are faked, so it needs no Docker or GPU.
- `tests/test_e2e_deepvariant.py` is opt-in:
  - `GEYSER_GPU_E2E=1` uses the GPU image;
  - `GEYSER_DV_E2E=1` uses the CPU image;
  - `GEYSER_DV_TESTDATA=<root>` also runs the real-data slice.
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
   - `s3:PutObject` on `variants_deepvariant/*`;
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
