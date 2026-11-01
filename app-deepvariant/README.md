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
```

Settings you can change. Set these on the host; `run_local.py` passes them into the container.

| Variable | Default | |
|---|---|---|
| `DEEPVARIANT_EXTRA_ARGS` | (none) | extra `run_deepvariant` flags, for example `--disable_small_model=true` |
| `DEEPVARIANT_SHARDS` | CPUs in the container | `make_examples` parallelism |
| `DEEPVARIANT_MODEL_TYPE` | `WGS` | |

There is no GPU? Build the CPU variant (`--build-arg DV_TAG=1.10.0 -t geyser-deepvariant-cpu`) and pass `--dv-image geyser-deepvariant-cpu --no-gpu`.

**Where the GPU actually helps.** Of DeepVariant's three steps, only `call_variants` uses the GPU. `make_examples` and `postprocess_variants` run on the CPU. Since 1.8, a "small model" inside `make_examples` also calls the easy sites on the CPU, so `call_variants` only sees the harder ones. The stage records each step separately, so you can see this in the timings.

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
   - `resourceRequirements`: `GPU=1`, `VCPU=4`, `MEMORY≈15000` (fits `g4dn.xlarge`);
   - queue: `gpu_queue` (from `batch_gpu.tf`);
   - command: `/opt/geyser-venv/bin/python deepvariant_task.py <srr_id> <reference_name>`;
   - environment: `BUCKET_NAME` (plus `AWS_REGION`).
3. **Disk.** The default ECS GPU AMI root volume is too small for a 10 GB compressed image. The compute environment needs a launch template with about 60 GB or more of gp3.
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
