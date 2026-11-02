# Why Step Functions and Batch, not Nextflow (#78)

Most genomics pipelines today are written for a workflow engine: Nextflow, Snakemake, WDL (Cromwell) or CWL.
Geyser isn't. Its stages are Python functions in one container image (`app/tasks.py`), wired together by an
AWS Step Functions state machine (`infrastructure/step_functions.tf`) that runs each stage as an AWS Batch job.
This page explains that choice, says what a workflow engine would have given for free, and says when the
answer should change. Nextflow is used as the comparison because it is the most common engine on AWS Batch
and has the nf-core community pipelines; most of the argument applies to the others too.

**Short answer.** Geyser started as a cloud-engineering project: an event-driven, serverless AWS design with
no servers to run, least-privilege IAM per component and hard cost limits. For a short pipeline (four stages,
one container) that design costs little extra code. The price is real, though: Geyser rebuilds pieces that
Nextflow provides, and that cost grows with every stage, retry rule and scatter step. The comparison below is
meant to make that trade visible, not to argue that the choice was free.

## Geyser's pieces in Nextflow terms

| Nextflow concept | What it does | Geyser's equivalent |
|---|---|---|
| `process` | One step: inputs, outputs, a script, a container, resources | A stage function in `app/tasks.py` (`decompress_task`, `qc_task`, `align_task`, `variants_task`), dispatched by `python tasks.py <task> <id>`; resources come from the single Batch job definition in `infrastructure/main.tf` (2 vCPU / 4 GB for every stage) |
| `workflow` + channels | The DAG: which outputs feed which inputs | The state machine: a `Pass` state builds each command, a `batch:submitJob.sync` task runs it, in a fixed sequence |
| Files in channels, work directory | Engine stages inputs in and outputs out; the user never names intermediate paths | S3 key conventions hardcoded in `tasks.py` (`keys_for` / `keys_of`: `raw_reads/` → `decompressed/` → `alignments/` → `variants/`), shared by convention with the trigger, the web UI and the scripts |
| `Channel.fromFilePairs` | Groups `_R1`/`_R2` mates into one sample | `layout_from_keys` in `tasks.py` plus the mate pairing in the trigger Lambda (`lambda/trigger/index.py`), with the suffix list duplicated in both and kept in step by `tests/test_consistency.py` |
| Executors (`local`, `awsbatch`, `k8s`, `slurm`, ...) and profiles | Same pipeline on a laptop, a cluster or a cloud | `STORAGE_BACKEND=s3\|local` and `METRICS_BACKEND` in `app/storage.py` / `app/metrics.py`, plus `scripts/run_local.py`, which plays the part of Step Functions and Batch offline |
| `publishDir` | Copies final outputs to a results location | Each stage uploads its own outputs to S3 under the conventional keys |
| `-resume` | Skips tasks whose inputs, script and container are unchanged (content-hashed cache) | None. A failed run starts again from the first stage. Step Functions *redrive* can restart a failed execution from the failed state, but it is not a cache: it doesn't notice changed inputs or code. `run_local.py --skip-to-variants` is a manual shortcut |
| `errorStrategy`, `maxRetries`, `memory { 4.GB * task.attempt }` | Retry with more resources | Retries, but not with more resources: the Batch job definition retries infrastructure failures only (3 attempts), the state machine retries Batch API errors with back-off, and every stage has a fixed size set in Terraform (`stage_resources`). An application failure fails the run at once. Nothing grows memory per attempt |
| `-with-trace`, `-with-report`, `-with-timeline` | Per-task duration, CPU, peak memory, I/O, exit status | The `@timed_stage` decorator (CloudWatch `Duration` / `FailureCount`), `scripts/query_pipeline_duration.py --stages` (queue wait, run time, cost, attempts, failure reason) and the dashboard scripts |
| Scatter/gather over intervals | Split calling by region, merge the results | The stages exist (`tasks.py`: split calling by region, then merge); the Step Functions `Map` state that fans them out is the infra half, in progress |
| nf-core modules and pipelines (e.g. nf-core/sarek) | Reviewed, versioned wrappers for hundreds of tools; complete germline pipelines including BWA-MEM2, duplicate marking, DeepVariant and HaplotypeCaller | Hand-written tool commands in `tasks.py` and `app-deepvariant/` |

## What Nextflow would have replaced

The state machine definition, the S3 key conventions and the code that keeps them in agreement, most of the
storage backend, `run_local.py`'s orchestration, the timing metrics, the retry policy, and the resume and
scatter/gather work. In this repository that is roughly `infrastructure/step_functions.tf` (115 lines), the
key-convention and layout code in `app/tasks.py`, most of `app/storage.py`, the orchestration half of
`scripts/run_local.py` (337 lines in all), and the standing rule that a renamed stage or S3 prefix must be
changed by hand in `tasks.py`, the state machine, the Lambda payload and the scripts together, because nothing
checks at build time that they agree.

## What it would not have replaced

A workflow engine runs the pipeline. Everything around it stays:

- **Infrastructure.** Nextflow's `awsbatch` executor submits jobs to a Batch queue someone else built. The VPC,
  subnets, Batch compute environments and queues, ECR, S3 buckets, IAM roles and the GPU environment in
  `infrastructure/` would all still be Terraform.
- **Where the engine runs.** Nextflow is a long-running JVM *head process* that has to stay up for the whole
  run (hours for a 30x genome). On AWS it needs a home: an EC2 instance, a Batch job of its own, Seqera
  Platform, or AWS HealthOmics (managed Nextflow/WDL/CWL). Step Functions has no head process: it is billed per
  state transition (a Geyser run uses about ten) and nothing waits or idles between runs.
- **The trigger.** An S3 upload starting a run, mate pairing, duplicate S3 events and run naming (the trigger
  Lambda) are outside any workflow engine.
- **Images and tools.** The Dockerfiles, tool versions and the DeepVariant / pangenome-aware DeepVariant images.
- **CI and identity.** The test workflow, GitHub OIDC roles, read-only Terraform roles and prod restrictions.
- **Cost controls.** No NAT gateway, Batch `max_vcpus` caps, attempt timeouts, budgets and the guardrails in
  `docs/design/claude-guardrails.md`.
- **Verification.** Synthetic data with planted SNPs, the Docker end-to-end test, and the hap.py benchmark of
  bcftools, DeepVariant and pangenome-aware DeepVariant on HG003 chr20.
- **The web UI and the ops scripts** (dashboard, alarms, reconciliation).

On Fargate in particular, Nextflow's AWS Batch executor needs Seqera's Wave service enabled to move task files
(Fusion is the recommended file layer on top), and its Fargate support has been marked experimental since it
arrived in Nextflow 24.04. That is one more dependency in a design whose point was to have very few.

## Why Geyser still uses Step Functions

1. **The project's subject is the cloud architecture.** Event-driven start, a serverless control plane,
   per-component IAM, everything in Terraform, every run visible in the AWS console. A Nextflow pipeline on
   Batch would still need all of that, with the orchestration moved somewhere less visible.
2. **Nothing runs between samples.** The trigger is a Lambda, the orchestrator is a managed service, Batch on
   Fargate scales to zero. There is no head node to size, patch or pay for.
3. **The pipeline is short and linear.** With four stages in a fixed order, the hand-written wiring is about a
   hundred lines of state machine. Most of Nextflow's value (dynamic DAGs, many tools, caching across long runs)
   only pays off with more stages than this.
4. **The stage logic stays portable anyway.** Stages contain no AWS calls; cloud specifics are confined to
   the storage and metrics backends, the Lambda and Terraform. The same container runs every stage locally
   with `run_local.py`. A future move to an engine would reuse the stage code, not rewrite it.

## The honest cost

Time. Every feature in the "replaced" list above is code written, tested and reviewed in this repository
instead of a line of Nextflow configuration: the key conventions and their consistency test, the local runner,
the timing metrics, and the retries, resume and sharding still to come. The pipeline also gives up
`-resume` (a failed align at hour three means starting again), community-reviewed tool wrappers, and the
ability to run unchanged on an HPC cluster or another cloud.

## When the answer should change

Move to a workflow engine (Nextflow first, given nf-core) when any of these becomes true:

- The pipeline grows past a handful of stages, or gains branches that depend on data (per-sample tool choices,
  optional annotation, joint calling).
- It has to run somewhere other than AWS: an HPC cluster, Kubernetes, or a second cloud. The state machine is
  the one part that doesn't travel.
- Long runs fail often enough that `-resume` would save real money.
- Bioinformaticians, rather than cloud engineers, become the main contributors.

What a move would involve: each stage function becomes a `process` that calls `python tasks.py <task>` (or
the tool commands directly), channels replace the key conventions, `fromFilePairs` replaces the layout
detection, and the state machine is retired. Terraform keeps the Batch queues and IAM; the trigger Lambda
starts a Nextflow head job (or a HealthOmics run) instead of a Step Functions execution. A small spike, a
`nextflow/main.nf` running the stages against `local_bucket/`, would make this comparison concrete; it has
not been built.
