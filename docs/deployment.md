# 🚀 Deployment & Usage

### 1. The Deployment Process
Deployment is deliberate, not automatic. Terraform runs locally from `infrastructure/` (`terraform init -backend-config=backend-dev.hcl`, then `plan`, review, `apply`), and the container image is built locally and pushed to ECR under a tag (`main-<sha>`, plus `dev-local` for the one the job definition uses). Claude Code, which helps build the project, can run plans and applies too, but only as a bounded role: a permissions boundary, Batch capacity caps, a local check before each command, and a $20 project budget that cuts the role off automatically. Destroying or replacing stateful resources stays a human decision. See [docs/design/claude-guardrails.md](design/claude-guardrails.md).

**Sizing and resilience settings** (`infrastructure/variables.tf`, all with defaults):

| Variable | Default | What it does |
| :--- | :--- | :--- |
| `stage_resources` | QC 1 vCPU / 2 GiB, align 4 / 8 GiB, variant shard 1 / 2 GiB, region planner 0.25 / 0.5 GiB, merge 0.25 / 0.5 GiB | Each stage's Fargate size, sent by the state machine as `ContainerOverrides.ResourceRequirements` (a change updates the state machine in place). Sized from the real-sample runs' peaks (#173). Checked against Fargate's valid vCPU/memory pairs, and QC (or the planner) + align, which run at the same time, must fit in `fargate_max_vcpus`. |
| `job_disk_gb` | 20 | Fargate ephemeral storage per job. 20 GiB is free; 21-200 is billed. A whole human genome at 30x needs ~100+. |
| `pipeline_timeout_hours` | 12 | Time limit for a whole run, queue waits included. A run past it ends as TimedOut. |
| `log_retention_days` | 30 | How long Batch job logs (`/aws/batch/geyser-<env>`) and trigger Lambda logs (`/aws/lambda/geyser-sfn-trigger-<env>`) are kept. |
| `intermediate_expiry_days` | 7 | How long `intermediate/` (region parts, the planner's regions) and `decompressed/` are kept, and when unfinished multipart uploads are aborted. Nothing reads a part after the merge, so this is only a debugging window. |
| `alignment_expiry_days` | 180 | When `alignments/` (CRAM + `.crai`) expires: the **break-even** of storing a CRAM vs re-aligning its raw reads, `align cost / (CRAM GiB × S3 price per GiB-day)` = 178-248 days on the six real chr20 runs (`scripts/lifecycle_break_even.py --results <dir>`; the same for a whole genome, since both scale together). `0` keeps alignments forever. Raw reads, references, VCFs, reports, stats and run records never expire. |

**Retries.** The Batch job definition retries only infrastructure failures, up to 3 attempts: a host going away, a Spot interruption, a task that failed to start, or a failed image pull or log setup. Anything else exits at once, including every non-zero exit code, so an application error such as a corrupt upload fails fast. Each Batch task in the state machine also retries Batch API errors (throttling, server errors) 3 times with back-off. Both are tested offline in Step Functions Local.

The GitHub Actions workflow `.github/workflows/deploy-platform.yml` ("Terraform Plan") can run a plan for the `dev` or `prod` environment. It authenticates with GitHub OIDC (short-lived credentials, no stored keys) using a read-only role, so it cannot change anything. It is manual and currently disabled.

<details>
<summary><b>Prerequisites for the CI plan workflow</b></summary>

<br/>

*   An AWS Account, with the GitHub OIDC identity provider configured.
*   GitHub environments `dev` and `prod`.
*   The following secrets configured in the GitHub repository settings:
    *   `AWS_REGION`: The target AWS region (e.g., `eu-west-2`).
    *   `ECR_IAM_ROLE_ARN`: The ARN of the IAM role for pushing to ECR.
    *   `TERRAFORM_IAM_ROLE_ARN`: The ARN of the read-only IAM role for `terraform plan`.

</details>

### 2. How to Use the Platform (Event-Driven)
Once deployed, a run is started by uploading a file.

**Verified in the cloud (2-3 November 2026).** Most checkpoints used one small sample: 10,000 single-end reads from GRCh38 chr20:10,000,001-10,050,000 with 10 planted SNPs. The real-sample runs used GIAB HG003 and HG005 chr20. Costs are Fargate (or EC2 GPU) on-demand list prices.

| Run | What it showed |
| :--- | :--- |
| `CLOUD002-20261102T132058Z-43888afd` | The three-stage state machine: QC and alignment overlapped, the run SUCCEEDED in 1 min 52 s (the old four-stage run took 3 min 45 s), all 10 planted SNPs, about $0.006. |
| `CLOUD004-20261102T144542Z-3aa11cd4` | A deliberately truncated upload: the stages fail, `Notify_Failure` publishes to SNS, the run ends Failed, and each job ran once (application failures are not retried). |
| `CLOUD005` / `CLOUD006` / `CLOUD007` | bwa-mem2, fastp, CRAM + indexes, QC statistics and the run record, single- and paired-end; with the bwa-mem2 index stored in S3 the align job dropped from 45.7 s to 29.3 s. |
| `CLOUD012-20261102T185742Z-2f9926cc` | Region-split variant calling: regions planned, 2 shards in parallel, merged. On a sample this small it is slower (3 min 45 s): each extra job adds about a minute of fixed overhead. |
| `CLOUD014-20261102T203644Z-dv` | **DeepVariant on a GPU** (`caller=deepvariant`): g6.xlarge (NVIDIA L4), 9 min 42 s (2.6 min instance start, 4 min image pull, 1.7 min DeepVariant), 10/10 planted SNPs, no false calls, about $0.16. The instance scaled back to zero after the job. |
| `REAL-HG003-35x-chr20-…-5bd4cf33` | **A real sample:** GIAB HG003 chr20 at 35x (19.0 M reads), 22 min 31 s, about $0.071. Quality gate (run on its outputs): **11/11 checks passed**, mean depth 36.8x, Ti/Tv 1.81, the same numbers as the offline calibration. |
| `REAL-HG003-30x-chr20-…-3da626bc` | The same at 30x (16.2 M reads): 21 min 25 s, about $0.072, gate 11/11. Peak memory: alignment 2.3-2.9 GiB of 8, a variant-calling shard 0.13 GiB of 4 (shards now get 2 GiB, [#174](../../../pull/174)). |
| `REAL-HG005-30x-chr20-…-859d7397` | A second genome, HG005 at 30x, paired-end: 22 min 54 s, about $0.079, gate 11/11. |
| `REAL-HG005-30x-SE-…` / `REAL-HG003-35x-SE-…` | Single-end input. HG005 (R1 and R2 as single-end reads, 31.7x): 18 min 36 s, gate 11/11. HG003 R1 only (19.1x): 12 min 36 s, gate **FAIL**, mean depth 19.07 < 20, correctly. |
| `REAL-HG003-15x-NEG-chr20-…-1a09ce99` | The negative control, HG003 downsampled to 16.1x: 15 min 10 s, gate **FAIL** on mean depth (16.14 < 20), as intended. Over all six runs: no false fails, no gate errors ([#157](../../../issues/157)). |
| `CLOUD015-20261103T022401Z-6e8ba766` | The first run through the quality gate's state-machine step ([#162](../../../pull/162)): the gate job takes about 45 s and costs about $0.0009. |

1.  **Locate the S3 Data Lake:** Find the S3 bucket created by Terraform. Its name has the format `<project_name>-data-lake-<random-suffix>`.
2.  **Upload Raw Data:** Upload your gzipped FASTQ file to the `raw_reads/` prefix, e.g. `raw_reads/SRR062634.fastq.gz`. The sample ID is the part of the filename before the first dot.
    *   **Paired-end:** upload both mates as `raw_reads/<id>_R1.fastq.gz` and `raw_reads/<id>_R2.fastq.gz`, in either order. The run starts once both are there, and only once. Only `_R1`/`_R2` mark mates: rename SRA-style `_1`/`_2` files before uploading. The stages detect the layout from these keys.
    *   The trigger Lambda checks for the other mate with `s3:GetObject` on `raw_reads/*` and `s3:ListBucket` (added in [#69](https://github.com/Harry5haw/Geyser-Genomics/pull/69)).
    *   **Reference genome:** `chr20.fa` unless the upload names another with S3 user metadata `reference` (also `reference-name` / `reference_name`), on either mate, e.g. `aws s3 cp reads.fastq.gz s3://<data lake>/raw_reads/S1.fastq.gz --metadata reference=GRCh38.fa`. The FASTA must already be at `reference/<name>` (indexes are optional: align builds missing ones). The state machine checks this first (`Check_Reference_Name`, then `Check_Reference`, a HeadObject), before any job: a name that isn't a plain file name (empty, with a `/`, mates that disagree, a misspelt key such as `refernce`, unreadable metadata) fails the run with `UnknownReference`, and a FASTA that isn't there with `ReferenceNotFound`, both closed and notified like any failed run. It never falls back to `chr20.fa` silently. Manual starts: `"reference_name"` in the input. Locally: `run_local.py --reference <name>` runs the same check against `<root>/reference/`.
3.  **Monitor the Execution:** The upload automatically starts the Step Functions state machine. You can follow its progress in the AWS Step Functions console, where each run is named after its sample and upload time, e.g. `SRR062634-20261101T123456Z-1a2b3c4d`. The same name appears in the Batch job names. If S3 delivers the same upload event twice, it still starts only one run. A paired-end run is named after the later of its two uploads.
4.  **Retrieve Results:** When the run succeeds, the outputs are in the same bucket:

| Stage | Output |
| :--- | :--- |
| Quality control | `qc_reports/<id>.fastp.html` and `qc_reports/<id>.fastp.json` from [fastp](https://github.com/OpenGene/fastp) in report-only mode (nothing trimmed or filtered); one report per sample, both mates for paired-end |
| Alignment | `alignments/<id>.cram` + `.cram.crai`, one per sample (coordinate-sorted, duplicates marked with samtools markdup; paired-end runs `samtools fixmate -m` first). CRAM stores the reads against the reference, so it is far smaller than a BAM, and it needs that same reference to read it: `samtools view -T reference/<name> alignments/<id>.cram` |
| Variant calling | `variants/<id>.vcf.gz` + `.vcf.gz.tbi` (tabix index, for region queries), from bcftools; with `caller=deepvariant`, `variants_deepvariant/<id>.vcf.gz` instead, or bcftools' VCF if DeepVariant fails |
| Consequence annotation (optional) | `variants/<id>.csq.vcf.gz` + `.tbi`: the same VCF with [bcftools csq](https://samtools.github.io/bcftools/howtos/csq-calling.html) consequences in `INFO/BCSQ` (e.g. `missense\|DEFB125\|ENST00000382410\|protein_coding\|+\|10I>10T`). Written only when the reference has gene models at `reference/<name>.gff3.gz` (Ensembl-style GFF3); `ANNOTATE=off` skips it. The cloud reference has none yet, so cloud runs don't annotate. |
| QC statistics | `stats/<id>/`: `flagstat.txt` (samtools flagstat: mapped, duplicates, properly paired) and `mosdepth.summary.txt` / `mosdepth.global.dist.txt` (mean depth, and the fraction of bases covered at each depth; duplicates excluded) from the align stage; `bcftools.stats.txt` (SNP and indel counts, Ti/Tv) from variant calling. DeepVariant and pangenome-aware DeepVariant write `bcftools.stats.<caller>.txt` over their **PASS calls only** (their VCFs also keep rejected RefCall candidates, which would halve Ti/Tv), and the sample's `bcftools.stats.txt` when they are the run's caller. The job log prints the headline numbers. |
| Quality gate | `qc_reports/<id>.gate.json`: the verdict of [`gate/`](../gate/README.md) on the QC statistics, the fastp report and the run record (pass or fail, each failed check named). Written by every run, locally and in the cloud; in the cloud a failed verdict fails the run (`gate_mode = "fail"`, #157). The thresholds are per run: `gate_profile` (S3 metadata `gate-profile` on the upload: `default`, `synthetic` or, for the cloud smoke tests, `smoke`, [gate/README.md](../gate/README.md#threshold-profile-per-run-gate_profile)) |
| Run record | `runs/<id>.provenance.json`: what produced the outputs. It lists the input keys and sizes, the reference (name, MD5, size), the tool versions the image reports, the image (`APP_VERSION`), the aligner and layout, each stage's status, timings and any error (a DeepVariant-only run is complete when DeepVariant, its caller, succeeds: it has no bcftools stage), the outputs with sizes, and the headline QC stats. Each stage writes its own part (`runs/<id>.provenance/<stage>.json`) and rebuilds the record, so a failed stage is recorded too. Best effort: it never fails a stage. `scripts/reconcile.py` shows it in its R column. Every job gets the Step Functions execution name as `RUN_NAME`, so each part says which run wrote it. The record also names the stage whose calls are the sample's (`variant_stage`, `vcf`): DeepVariant's when it was the run's caller. |

### 3. Monitoring
For the day-to-day commands (watching, stopping a run, rollback, common failures), see the [operations runbook](operations.md).

> [!NOTE]
> The dashboard (`geyser-dashboard-dev`) and the trigger alarm (`geyser-dev-trigger-lambda-errors`) are deployed, with the scripts below, after a `--dry-run` check. Both are within the CloudWatch free tier. The alarm has no actions: it is a signal on the dashboard, not an email.

Each stage records `Duration` (and `FailureCount` when it fails) in the CloudWatch namespace `GeyserGenomics`. The dashboard and alarm definitions are built in `scripts/monitoring.py` and deployed by small Python scripts rather than Terraform, because the dashboard JSON was hard to manage there.

| Script | What it creates |
| :--- | :--- |
| `scripts/deploy_dashboard.py` | Dashboard `geyser-dashboard-dev`: links to the console pages for a run (Step Functions runs, Batch jobs, logs, Logs Insights, alarms, S3, budgets), alarm status, pipeline runs per day, end-to-end time (p50/p95), failures by stage, result quality per sample (from the job logs), data lake size and object count, and stage durations for the core pipeline, the region shards, DeepVariant and pangenome DeepVariant |
| `scripts/deploy_alarms.py` | Alarm `geyser-dev-trigger-lambda-errors`: the trigger Lambda failing, the one failure that leaves no trace in the pipeline's own metrics because no run starts. Its state shows on the dashboard |
| `scripts/runtime_deploy_pipeline_dashboard.py` | Optional dashboard with only the Step Functions widgets |
| `scripts/query_pipeline_duration.py` | Prints a table of recent Step Functions run times (read-only). With `--stages`: for each stage, how long the Batch job queued vs ran, its vCPU/memory, attempts, why it failed (e.g. out of memory), and an estimated Fargate cost per stage and per run, and the CPU time and peak memory each job used against what it was given (`--stages-csv` to export) |
| `scripts/reconcile.py` | Compares uploads, runs and outputs to find what the pipeline silently missed: uploads the trigger ignores (not `.gz`), refuses (a mate not named `<id>_R1.fastq.gz`/`_R2.fastq.gz`) or that start a run which will fail (anything but `raw_reads/<id>.fastq.gz` or `<id>_R1`/`_R2`), paired-end samples with only one mate or uploaded both ways, samples with no VCF, orphan outputs, and (with `--state-machine-arn`) two runs of one sample at once. Works offline: `--local-dir local_bucket` |
| `scripts/stop_run.py` | Stops a run: its Step Functions execution **and** the Batch jobs it started (stopping an execution alone leaves its running `.sync` jobs running and billing). Finds the jobs by name (every job name ends with the execution name) in every job queue, queued or running only. `--dry-run` only reads. `python scripts/stop_run.py <execution-name> --dry-run`, with `SFN_ARN` set |

**Preview without AWS:** `--dry-run` prints the JSON and makes no AWS calls. No ARNs are needed.
```bash
python scripts/deploy_dashboard.py --dry-run
python scripts/deploy_alarms.py --dry-run
```

**Deploy** (uses your AWS credentials; region defaults to `eu-west-2`). The state machine ARN is passed in, and the account ID for the alarm widget is looked up at deploy time, so neither is stored in the repo:
```bash
export SFN_ARN=$(terraform -chdir=infrastructure output -raw geyser_pipeline_state_machine_arn)
export BUCKET_NAME=$(terraform -chdir=infrastructure output -raw data_lake_bucket_name)   # for the S3 link
python scripts/deploy_alarms.py
python scripts/deploy_dashboard.py
```

Stage durations show p50 and p95 per stage: the metrics are published per stage (dimensions `TaskName` + `Status`), not per sample. Images built before Nov 2026 published a `SampleId` dimension too, which made every sample a separate metric and ruled out percentiles; `--schema per-sample` still reads that older data (mean and max across samples). Per-sample detail is in the job logs and the run record. To add a stage or a new variant caller, add it to `STAGE_GROUPS` in `scripts/monitoring.py`.

Email is optional and off by default: `deploy_alarms.py --sns-topic-arn <arn>` also sends the alarm to an SNS topic. Failed runs already publish to the pipeline's SNS topic from the state machine's `Notify_Failure` step.

---

---

[← Back to the README](../README.md)
