# Operations runbook

How to run, watch, stop and troubleshoot the cloud pipeline once it's deployed. Every command here was checked
against the code on `main`. Names in angle brackets are yours to fill in:

| Placeholder | What it is | Where to find it (from `infrastructure/`) |
| :--- | :--- | :--- |
| `<data-lake>` | the S3 data lake bucket | `terraform output -raw data_lake_bucket_name` |
| `<state-machine-arn>` | the pipeline's Step Functions state machine | `terraform output -raw geyser_pipeline_state_machine_arn` |
| `<ecr-repo>` | the app image repository URL | `terraform output -raw ecr_repository_url` |
| `<id>` | a sample ID | the upload's file name, see below |
| `<run>` | a run's execution name | e.g. `SRR062634-20261101T123456Z-1a2b3c4d` |

The scripts read `SFN_ARN` for the state machine, and all default to region `eu-west-2`.

```bash
export SFN_ARN=<state-machine-arn>
```

## Start a run

Upload a gzipped FASTQ to `raw_reads/`. The S3 notification starts the trigger Lambda, and the Lambda starts the
state machine.

**Single-end:** `raw_reads/<id>.fastq.gz`. The sample ID is everything before the first dot of the file name.

```bash
aws s3 cp SRR062634.fastq.gz s3://<data-lake>/raw_reads/SRR062634.fastq.gz
```

**Paired-end:** `raw_reads/<id>_R1.fastq.gz` and `raw_reads/<id>_R2.fastq.gz`, in either order.
- The first mate's event finds the other missing and waits (the Lambda returns 202, nothing starts).
- The second mate's event starts **one** run. If both mates land at once, they still start only one run.
- Only `_R1`/`_R2` mark mates, and the mates must end in exactly `.fastq.gz`. Rename SRA-style `_1`/`_2` files
  first: a single-end sample called `run_1` stays `run_1`.
- Uploading the pair again starts a new run.

**The run's name** is `<sample>-<UTC time>-<hash>`, e.g. `SRR062634-20261101T123456Z-1a2b3c4d`. It's the
Step Functions execution name and the suffix of every Batch job name, and every job gets it as `RUN_NAME`.
A duplicate S3 event starts nothing.

**Reference** ([#183](../../../pull/183)): `chr20.fa` from `reference/`, unless the upload names another with S3 metadata
`reference` (also `reference-name` / `reference_name`), on either mate:

```bash
aws s3 cp reads.fastq.gz s3://<data-lake>/raw_reads/S1.fastq.gz --metadata reference=GRCh38.fa
```

- The FASTA must already be at `reference/<name>`. Indexes are optional: align builds any that are missing,
  which costs time and memory on a whole genome, so upload them alongside.
- The state machine checks it before any job (`Check_Reference_Name`, then `Check_Reference`, a HeadObject).
  A name that isn't a plain file name (empty, containing `/`, mates that disagree, a misspelt key such as
  `refernce`, metadata the trigger couldn't read) fails with `UnknownReference`; a FASTA that isn't there
  fails with `ReferenceNotFound`. It never falls back to `chr20.fa` silently.
- A manual start sets it in the input: `"reference_name": "GRCh38.fa"`. It's required there: a manual start
  without it fails with `UnknownReference` (the trigger always sets one).

**Quality-gate profile** ([#178](../../../pull/178)): an upload can name the gate's thresholds with S3 metadata
`gate-profile`. The trigger passes it on as `gate_profile`, and the state machine checks it first
(`Check_Gate_Profile`), before any job:

```bash
aws s3 cp reads.fastq.gz s3://<data-lake>/raw_reads/S1.fastq.gz --metadata gate-profile=smoke
```

| Profile | Thresholds (`gate/thresholds/`) | For |
| :--- | :--- | :--- |
| `default` (also when unset) | `default.toml` | real samples (calibrated on HG003/HG005 chr20) |
| `synthetic` | `synthetic.toml` | the **local** synthetic data from `make_test_data.py` (`run_local.py --gate-profile synthetic`) |
| `smoke` | `smoke.toml` | the **cloud** smoke tests (CLOUD0xx: 10,000 real chr20 reads with 10 planted SNPs) |

- Anything else fails the run at once with `UnknownGateProfile`, in either gate mode. That includes mates
  whose `gate-profile` values disagree, a misspelt metadata key with "gate" in it, and metadata the trigger
  couldn't read.
- A manual start sets it in the input: `"gate_profile": "smoke"`.
- **Cloud smoke tests use `smoke`, not `synthetic`.** `synthetic` is calibrated for the local 50 kb synthetic
  reference, while the cloud smoke sample is 10,000 real chr20 reads judged over the whole of chr20 (mean
  depth about 0.02x, 0% at 10x, GC 37.5%), so it fails them. `smoke` is calibrated on the cloud checkpoints
  CLOUD005-016 ([#157](../../../issues/157)); every one passes it.

**AlphaGenome annotation (v3.0):** an upload can ask for the optional annotation of its calls after
the quality gate with S3 metadata `annotate=alphagenome` (on either mate); a manual start sets `"annotate": "alphagenome"`.
- **Both routes, either caller:** every run that succeeds the quality gate can be annotated. The source in the cloud is
  always the AlphaGenome Atlas (`--source atlas`; no AVI file in S3).
- **Sized per run:** a small `Plan_Annotate` job (`tasks.py annotate --plan`) writes `intermediate/annotate-plan/<id>/<run>.json`;
  `Annotate` takes its vCPU, memory and time limit from it. With nothing to annotate the plan says `run: false` and no
  annotate job runs (the reason is in the execution's output, `annotate_plan`).
- **Off by default.** `none` or no value means no annotation. Anything else fails the run before any job with
  `UnknownAnnotation`, like an unknown gate profile.
- **Never fatal.** A failed annotation is noted in the execution's output (`annotate_error`) and the job log, and the run keeps
  the core pipeline's status.
- **The API key** is an SSM SecureString that you put in yourself, with your own identity. Terraform only references
  it by name (`alphagenome_api_key_parameter`, default `/geyser/alphagenome/api-key`):

  ```bash
  AWS_PROFILE=default aws ssm put-parameter --name /geyser/alphagenome/api-key --type SecureString --value "<key>"
  ```

  Until it exists, an annotate job fails to start ("unable to pull secrets"), which is noted as above.

### Choose the caller

| Caller | Where it runs | How to choose it |
| :--- | :--- | :--- |
| `bcftools` (default) | Fargate: regions planned, called in parallel by a `Map` state, merged | nothing to do |
| `deepvariant` | the GPU Batch queue (one g6.4xlarge / g5.4xlarge, scales to zero); **falls back to bcftools** if it fails | a manual start (below), or Terraform `default_caller = "deepvariant"` for every triggered run |

**A manual start** runs any sample whose reads are already in `raw_reads/`, with any caller. The input is
the trigger's document. The name must be 1-80 characters from `A-Z a-z 0-9 _ -`, and it ends up in the job
names.

```bash
aws stepfunctions start-execution --state-machine-arn "$SFN_ARN" \
  --name "SRR062634-$(date -u +%Y%m%dT%H%M%SZ)-manual" \
  --input '{"srr_id": "SRR062634", "reference_name": "chr20.fa", "caller": "deepvariant"}'
```

Without `caller`, or with anything other than `deepvariant`, the run uses bcftools.

**Per-run sizes (`overrides`, manual starts only):** an optional `overrides` object changes align's Fargate
size and the variant-calling window for this run alone, with no Terraform apply. It's for experiments such
as the cost x turnaround grid (`docs/figures/cost-surface-grid.md`). Uploads never set it, so triggered runs
always use Terraform's defaults.

| Key | Type | Default (Terraform) | Allowed |
| :--- | :--- | :--- | :--- |
| `align_vcpu` | string | `"4"` (`stage_resources["align"]`) | with `align_memory_mb`, a valid Fargate pair (`"0.25"`: 512/1024/2048 MiB; `"0.5"`: 1-4 GiB; `"1"`: 2-8 GiB; `"2"`: 4-16 GiB; `"4"`: 8-30 GiB, in 1 GiB steps), where QC or the planner plus align still fit in `fargate_max_vcpus` (so no 8 vCPU at the default cap). Written as Terraform writes it: `"4"`, not `"4.0"` |
| `align_memory_mb` | string | `"8192"` | as above |
| `variant_region_window` | number | `50000000` (`variant_region_window`) | a whole number from 5,000,000 to 250,000,000 (bases per region; the planner and the merge both use it). 65 Mb = 1 shard on chr20, 33 Mb = 2, 17 Mb = 4, 9 Mb = 8 |
| `chunk_count` | number | `24` (`genome_chunk_count`) | large route only: a whole number from 2 to 96 (equal chunks the pre-flight cuts) |
| `align_concurrency` | number | `5` (`genome_align_concurrency`) | large route only: chunks aligned at once, 1 to (`fargate_spot_max_vcpus` − QC's vCPU) ÷ a chunk's vCPU = (48 − 8) ÷ 8 = **5** |
| `calling_concurrency` | number | `4` (`variants_max_concurrency`) | both routes: calling shards at once, 1 to `fargate_spot_max_vcpus` ÷ a shard's vCPU = **48** |
| `merge_threads` | number | `8` (`genome_merge_threads`) | large route only: the merge's samtools threads, 1 to its vCPU (**8**) |

The maxima come from the **Fargate Spot** cap, so a run that asks for the most still stays on Spot prices. Raising
them (a "fast" genome) is a cap raise in Terraform: a compute-environment change, the user's apply.

**Big inputs (v1.3): the large route.** The trigger passes the upload's size as `input_bytes` (both mates added
up). At or above `large_input_gb` (5 GB) a run takes the large route:
- QC on the whole input, alongside a **pre-flight**: one streaming pass for gzip integrity, the read count, mates
  in step and `chunk_count` equal chunks;
- the chunks aligned in parallel (bwa-mem2), then a streaming merge (markdup, CRAM).

The outputs are the same keys as today's, so calling, the gate and everything after are unchanged. Smaller
inputs, or none, run as before.
- A truncated or corrupt upload fails in the pre-flight (`qc_reports/<id>.preflight.json` says why), before any
  alignment.
- A manual start chooses the route with `input_bytes`. A chr20 **canary on the large route**:
  `"input_bytes": 5000000000, "overrides": {"chunk_count": 4}`.
- **giraffe stays chromosome-scale:** `aligner` other than bwa-mem2 on a large input fails before any job with
  `AlignerNotOnLargeRoute` (haplotype sampling needs the whole sample).
- An `input_bytes` that isn't a non-negative number fails before any job with `InvalidInputBytes`.

Any key left out keeps its default. Only align and the window change: QC, the planner, the shards and the
merge keep their sizes.

```bash
aws stepfunctions start-execution --state-machine-arn "$SFN_ARN" \
  --name "REAL-HG003-30x-chr20-$(date -u +%Y%m%dT%H%M%SZ)-a2w17" \
  --input '{"srr_id": "REAL-HG003-30x-chr20", "reference_name": "chr20.fa",
            "overrides": {"align_vcpu": "2", "align_memory_mb": "4096", "variant_region_window": 17000000}}'
```

**Checked before any job** (`Run_Setting_Rules` → `Set_Run_Settings`, after `Check_Gate_Profile`). Each of
these fails the run with `InvalidRunOverrides`, closed and notified like any failed run, and the Cause lists
the allowed pairs and bounds:
- `overrides` that isn't an object;
- an unknown key (a misspelling such as `align_cpu` is never silently ignored);
- a wrong type (a number for a size, a string for the window);
- a pair that isn't allowed;
- a window out of bounds or not whole.

The settings the run used are in the execution's output, under `run_settings`. A valid size that is too
small for the sample (e.g. 0.25 vCPU / 512 MiB for align) still fails, in its job, with `OutOfMemoryError`.

## Watch a run

- **The Step Functions console:** the execution `<run>` and its graph:
  `Check_Gate_Profile` → `Check_Reference_Name` → `Check_Reference` → the run-settings checks → `QC_and_Align` → `Choose_Caller` → variant calling → `Quality_Gate` → `Pipeline_Succeeded`.
- **The dashboard** (`geyser-dashboard-dev`): runs, failures and durations by stage, result quality, data lake
  storage, the trigger alarm. It's deployed by script; `--dry-run` prints the body and makes no AWS calls:
  ```bash
  python scripts/deploy_dashboard.py --dry-run
  SFN_ARN=<state-machine-arn> python scripts/deploy_dashboard.py --bucket <data-lake> \
    --web-distribution-id <distribution_id>    # the hosted site's CloudFront error rates (us-east-1 metrics)
  ```
  The hosted-site row (web API Lambda requests/errors/throttles and duration p50/p95, CloudFront 4xx/5xx) is in by
  default; `--no-web` leaves it out. The Publish job is a stage group (*Publish (hosted site)*).
- **Run times, and per stage:** queue wait, run time, estimated cost, attempts and the failure reason:
  ```bash
  python scripts/query_pipeline_duration.py --stages                  # the latest 50 runs
  python scripts/query_pipeline_duration.py --status FAILED --stages --stages-csv stages.csv
  ```
  Costs are estimates at Fargate on-demand list prices (`--vcpu-hour-price`, `--gb-hour-price`).
- **Uploads vs runs vs outputs:** lone mates, refused names, failed or unfinished runs, and missing outputs:
  ```bash
  python scripts/reconcile.py --bucket <data-lake>                    # add $SFN_ARN to see open runs too
  python scripts/reconcile.py --bucket <data-lake> --json --fail-on-problems
  python scripts/reconcile.py --bucket <data-lake> --publish-metrics  # CloudWatch GeyserGenomics/ReconcileProblems
  ```
  `--publish-metrics` sends the problem count by kind (dimension `Problem`: `LoneMate`, `Ignored` (non-.gz
  uploads that start nothing), `ConcurrentRuns` (duplicate runs, needs `$SFN_ARN`), ..., `Total`), zeros included.
  It is best effort: a CloudWatch error is a warning. With `--local-dir` the counts go to `metrics/metrics.jsonl`.
  Nothing runs it on a schedule; it is run by hand.
- **Logs:** Batch jobs in `/aws/batch/geyser-<env>`, the trigger in `/aws/lambda/geyser-sfn-trigger-<env>`
  (kept 30 days, `log_retention_days`).

## Stop a run

Stopping an execution in the console does **not** stop the Batch jobs it submitted: they keep running and
billing to the end. Use the script instead. It stops the execution, then terminates every job of that run
that's still queued, starting or running, in every job queue.

```bash
python scripts/stop_run.py <run> --dry-run     # read-only: what it would stop
python scripts/stop_run.py <run>               # stop it
```

The exit code is 0 when nothing of the run is left running, 1 if the execution doesn't exist, and 2 if a job
couldn't be terminated. Run it for a run that ended `TimedOut` too: the 12-hour limit ends the execution, not
its jobs.

## Read the results, the run record and the gate verdict

The outputs are in the same bucket: `qc_reports/<id>.fastp.{html,json}`, `alignments/<id>.cram` + `.crai`,
`variants/<id>.vcf.gz` + `.tbi` (or `variants_deepvariant/` when DeepVariant was the caller), and `stats/<id>/`.
The [README](../README.md#2-how-to-use-the-platform-event-driven) has the full table.

**The run record** says what produced them: the inputs, the reference with its MD5, tool versions, the image
(`app_version`), each stage's status and timings, the outputs, the headline QC stats and the gate's verdict
(`app/provenance.py` documents every field).

```bash
aws s3 cp s3://<data-lake>/runs/<id>.provenance.json - | python -m json.tool
```

Each stage writes its own part under `runs/<id>.provenance/`, and the file above is assembled from them. After
a failed run, the stored file can lag one step behind the failure close. `reconcile.py` and the web UI
assemble the parts themselves, so they're always current.

**The quality gate's verdict:** `pass`, or `fail` with one named reason per failed check. When the gate
couldn't judge the run, the verdict is `null` with the reason.

```bash
aws s3 cp s3://<data-lake>/qc_reports/<id>.gate.json - | python -m json.tool
```

The checks and thresholds are in [`gate/thresholds/default.toml`](../gate/thresholds/default.toml), described
in [`gate/README.md`](../gate/README.md): base quality, GC, reads lost, mapped %, duplicates, properly paired,
mean depth (≥ 20x), breadth at 10x, SNP count and Ti/Tv.

### Warn mode and fail mode

Terraform's `gate_mode` decides what a failed verdict does. **It's `fail`** since [#157](../../../issues/157):
real-sample runs passed with no false fails, and cloud smoke tests pass with the `smoke` profile above. So
**upload smoke tests with `gate-profile=smoke`**: without it they're judged as real samples and the run fails. The profile picks the thresholds (`GATE_CONFIG` for the gate job); the
mode picks what a failure does.

| Verdict | `gate_mode = "warn"` | `gate_mode = "fail"` (current) |
| :--- | :--- | :--- |
| pass | run SUCCEEDED | run SUCCEEDED |
| fail | run SUCCEEDED through `Gate_Warned`; the verdict is in `gate.json`, the run record, the job log and the execution's output | run FAILED with `QualityGateFailed` and the reasons, through `Close_Run_Record` → `Notify_Failure` |
| no verdict (gate job failed, verdict missing or unreadable) | run SUCCEEDED through `Gate_Skipped`; never judged, so it doesn't count as a pass | run FAILED with `QualityGateError` |

In warn mode (the way back, one in-place apply), **a SUCCEEDED run isn't necessarily a good one.** Read `gate.json`, or look for
`Gate_Warned` / `Gate_Skipped` in the execution's history.

## Images: tags and rollback

The Batch job definition runs `<ecr-repo>:dev-local` (Terraform `image_version`; the DeepVariant job uses
`deepvariant_image_version` in its own repository). Every build is pushed twice: as `main-<sha>`, the commit it
was built from, and as `dev-local`, the tag the jobs pull. Each job pulls the tag when it starts, so moving
`dev-local` changes the image for the next job, with no Terraform change.

**Deploy a new image** (built from a clean checkout of `main`):

```bash
REPO=<ecr-repo>                  # e.g. from terraform output -raw ecr_repository_url
SHA=$(git rev-parse --short HEAD)
aws ecr get-login-password | docker login --username AWS --password-stdin "${REPO%%/*}"
docker build -f Dockerfile -t "$REPO:main-$SHA" .
docker tag "$REPO:main-$SHA" "$REPO:dev-local"
docker push "$REPO:main-$SHA" && docker push "$REPO:dev-local"
```

**Roll back** by pointing `dev-local` at an earlier `main-<sha>`. The repository's tags are mutable, and
there's no rebuild:

```bash
MANIFEST=$(aws ecr batch-get-image --repository-name geyser-app \
  --image-ids imageTag=main-<old-sha> --query 'images[0].imageManifest' --output text)
aws ecr put-image --repository-name geyser-app --image-tag dev-local --image-manifest "$MANIFEST"
```

- Check an image against the state machine before rolling back to it. An image from before a state-machine
  change may lack a task the machine calls: images older than scatter-gather can't run its `regions` step.
- ECR keeps the 20 most recent images, so very old rollback tags expire.
- The run record's `app_version` is the tag the job was given (`dev-local`). The exact build is the
  `main-<sha>` that `dev-local` pointed to at the time.

## The hosted site (v1.4, not live yet)

The site is static pages in a private S3 bucket behind CloudFront (`infrastructure/web/`, which the user plans and
applies). A small API Lambda serves uploads, status and downloads (`web_api.tf`). The `geyser-publish` job
(`publish.tf`) writes the pages.
- **Per run:** with `publish_enabled = true`, every run ends by publishing its sample's pages and the index, whether
  it succeeded or failed: a failed run's page shows which stage failed. The only exception is a run that failed
  before any job (a bad profile or reference). A failed publish never changes the run's outcome.
- **Bootstrap, once per new site:** `python manage.py publish --all` (in the `geyser-publish` image, or from a
  checkout for `/figures/`). It writes every sample's pages; the per-run publishes then keep it current.
- **After a publish image whose inline CSS or scripts changed:**
  1. run `publish --all`;
  2. copy `csp-hashes.json`'s `script` and `style` lists from the site's root into the web stack's `script_hashes` /
     `style_hashes`;
  3. apply the web stack (the response-headers policy in place).
  Until then, the pages' inline blocks are blocked by the CSP.
- **Uploads** need the token in the SSM SecureString `web_upload_token_parameter`, which the user puts in. Rotate it
  after sharing it for a demo.

## Cost guardrails, in brief

- **No NAT gateway** (`enable_nat_gateway = false`): Fargate jobs run in a public subnet with a public IP.
- **Capped capacity:** Fargate 8 vCPU on-demand plus 8 on Spot (Spot is tried first); GPU at most 4 vCPU,
  i.e. one instance, scaling back to zero after its job.
- **A monthly budget alert** (`monthly_budget_usd`, default $10) at 50% and 100% of actual spend and 100% of
  forecast.
- **Bounded storage and logs:** ECR keeps 20 images, logs are kept 30 days, and `intermediate/` objects expire
  after 7 days.
- **Small runs cost cents:** about $0.007 for a 10,000-read test sample, about $0.07 for a real 30-35x chr20
  sample, about $0.16 for a DeepVariant run on the GPU (on-demand list prices).
- **The automation role** has its own permissions boundary and a project budget that cuts it off by itself:
  [docs/design/claude-guardrails.md](design/claude-guardrails.md).
- **Failure notifications:** `Notify_Failure` publishes to the SNS topic `geyser-PipelineStatus-<env>`, and
  Terraform subscribes the budget-alert address to it by email (`infrastructure/notifications.tf`, #122). AWS
  first emails a confirmation link, and the subscription delivers nothing until it is clicked. To check:
  `aws sns list-subscriptions-by-topic --topic-arn <topic-arn>`, where a confirmed subscription has an ARN
  and an unconfirmed one shows `PendingConfirmation`.

## Common failures and what you see

| What happened | What you see | What to do |
| :--- | :--- | :--- |
| **Truncated or corrupt upload** | QC or align fails (both reject it) with exit code 1 after **one** attempt (application errors aren't retried); the run ends FAILED through `Close_Run_Record` → `Notify_Failure`; the run record is `failed`; the job log shows the gzip error | upload the file again |
| **Lone mate** (only `_R1` or only `_R2`) | no run; the trigger log says `'<id>': R1 arrived, waiting for s3://…/<id>_R2.fastq.gz`; reconcile: `lone mate` | upload the other mate |
| **Mate not named exactly** (`S1_R1.fq.gz`) | no run; the trigger Lambda raises (`Paired-end mates must be named <id>_R1.fastq.gz / <id>_R2.fastq.gz`), so the dashboard's trigger alarm goes red; reconcile: `refused` | rename to `<id>_R1.fastq.gz` / `<id>_R2.fastq.gz` |
| **Other `.gz` names** (`S1.fq.gz`, `sub/S1.fastq.gz`, `S1.v2.fastq.gz`) | a run starts and fails in its first stage: the stages read only `raw_reads/<id>.fastq.gz`; reconcile: `will fail` | rename to `raw_reads/<id>.fastq.gz` |
| **Not gzipped** (`S1.fastq`) | nothing at all: the S3 notification only fires for `.gz` | gzip it |
| **Both layouts** (`S1.fastq.gz` and `S1_R1/_R2`) | every stage fails with `LayoutError: S1 has both single-end and paired-end (R1/R2) raw files` | delete one layout, start again |
| **DeepVariant fails** | the run continues with bcftools (`DeepVariant` caught → `Prepare_Plan_Command`); the bcftools VCF in `variants/` is the sample's | read the DeepVariant job log |
| **The gate gives no verdict** | warn: SUCCEEDED through `Gate_Skipped`, `gate.json` has `"verdict": null` and why; fail: `QualityGateError` | read the `QualityGate-<id>-<run>` job log |
| **A job stuck waiting in the queue** | no queue limit of its own; the whole run ends `TimedOut` at 12 h (`pipeline_timeout_hours`) | `scripts/stop_run.py <run>`; check the compute environments' capacity |
| **Unknown gate profile** (`gate-profile=strict`, a typo, mates that disagree) | the run fails before any job with `UnknownGateProfile: gate_profile "…" is not one of: default, synthetic, smoke`, through `Close_Run_Record` → `Notify_Failure` | upload again with `default`, `synthetic`, `smoke` or no `gate-profile` |
| **Bad reference name** (`reference=sub/x.fa`, an empty value, mates that disagree, a misspelt key) | the run fails before any job with `UnknownReference: reference_name "…" is not a file name under reference/ (a non-empty string without a slash)`, through `Close_Run_Record` → `Notify_Failure` | upload again with a plain file name, or no `reference` for `chr20.fa` |
| **Reference not in the data lake** (`reference=nope.fa`) | the run fails before any job with `ReferenceNotFound: reference/nope.fa could not be read from <data-lake> (S3.NoSuchKeyException)…`, through `Close_Run_Record` → `Notify_Failure` (`CLOUD018`) | upload the FASTA to `reference/`, or name one that's there |
