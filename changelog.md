# Changelog

Notable changes, newest first, built from the merged pull requests (each entry links its PR; the PR
descriptions have the detail and the test results). There are no tagged releases yet apart from
`v0.1-airflow` (the Airflow design, 11 Sep 2025); release tags and GitHub Releases are a separate decision.
"Applied" means the change is live in AWS; everything else is code on `main`.

## Unreleased: 2 November 2026, toward 1.0

### Pipeline
- **Three stages instead of four.** QC and alignment read the gzipped upload directly, so the decompress
  stage and its `decompressed/` copy are gone, and the two run side by side. ([#86](../../pull/86))
- **bwa-mem2 is the default aligner**, with an automatic fallback to bwa for references whose first contig
  doesn't start with an N gap, where bwa-mem2 misplaces reads (upstream bwa-mem2#262). ([#88](../../pull/88))
- **fastp replaces FastQC:** one HTML + JSON report per sample in report-only mode; FastQC reports from older
  runs still count as QC done. ([#103](../../pull/103))
- **CRAM + `.crai`** instead of BAM for alignments, and **tabix-indexed VCFs**. ([#99](../../pull/99))
- **QC statistics per sample** under `stats/<id>/`: `samtools flagstat`, `mosdepth` coverage, `bcftools
  stats`. ([#102](../../pull/102))
- **Provenance run record** `runs/<id>.provenance.json`, assembled from one part per stage; shown by
  `reconcile.py`. ([#105](../../pull/105))
- **Quality gate** (`gate/`, Rust): a run's fastp report, QC statistics and run record against thresholds
  calibrated on HG003/HG005 chr20, with a JSON verdict naming each failed check ([#116](../../pull/116),
  [#123](../../pull/123)). Built into the image and run as `tasks.py gate` after variant calling, in warn
  mode, with the verdict in `qc_reports/<id>.gate.json` and the run record ([#148](../../pull/148)).

### Infrastructure (applied)
- **QC and alignment in a `Parallel` state**, then variant calling. The definition moved to
  `infrastructure/state_machine.asl.json` and is tested in Step Functions Local in CI. Cloud run: 1 min 52 s,
  down from 3 min 45 s. ([#89](../../pull/89))
- **Resilience and per-stage sizing:** infrastructure-only retries in the job definition, Batch API retries
  in the state machine, a 12-hour run limit, per-stage vCPU/memory, optional larger job disk, and
  Terraform-managed log groups kept 30 days. ([#106](../../pull/106))
- **Terraform state per environment** (`env/dev`, `env/prod`; prod intentionally empty). ([#75](../../pull/75))
- **Guardrails for Claude Code on AWS:** the `geyser-claude` role with a permissions boundary, Batch capacity
  caps and a $20 project budget whose action attaches a deny-all policy. ([#74](../../pull/74))
- **AWS provider 6.69.0**, so the guardrail budget can use a custom 12-month period. ([#77](../../pull/77))

### Fixed
- **The first cloud run since the rebuild failed in decompress:** a gzip stream over an S3 download claims to
  be seekable, and boto3 seeked it. The stream was made forward-only, with a regression test against boto3's
  real transfer code. ([#72](../../pull/72))

### Storage, monitoring and operations
- **S3-compatible storage:** `S3_ENDPOINT_URL` and `S3_ADDRESSING_STYLE` point the S3 backend at any
  S3-compatible service; outside AWS, checksums are sent only when required. ([#85](../../pull/85))
- **Monitoring, built and tested offline:** a CloudWatch dashboard, the trigger-Lambda alarm, per-stage queue
  wait / run time / cost / failure reasons, and `reconcile.py` (uploads vs runs vs outputs). Not deployed yet.
  ([#54](../../pull/54))

### Benchmarks
- **bwa vs bwa-mem2 on HG003 chr20:** bwa-mem2 makes the align stage 1.56× faster (812 s → 521 s) for
  2.3 GiB more peak memory, with equivalent calls by hap.py. ([#110](../../pull/110))
- **HG005 pangenome benchmark** against a same-data HG003 control, with GIAB stratifications by region type.
  ([#95](../../pull/95))

### CI, dependencies and repository
- **CI quality gates:** ruff check and format, `terraform fmt` and `validate`; config in `pyproject.toml`.
  ([#107](../../pull/107))
- **Docker layer cache in CI**, written from main only, rebuilt from scratch weekly. ([#91](../../pull/91))
- **Exact Python pins and Dependabot** (pip, GitHub Actions and Docker weekly; Terraform monthly); base image
  pinned by digest. ([#92](../../pull/92), [#98](../../pull/98), [#96](../../pull/96))
- **CONTRIBUTING, SECURITY, CITATION.cff and issue templates** ([#93](../../pull/93)); a PR template with
  Related issues and Dissertation sections ([#83](../../pull/83)).
- **Docs:** why Step Functions and Batch rather than Nextflow ([#81](../../pull/81)); one banner, no
  third-party logos, the 2.0 release banner ([#90](../../pull/90)).

## 1 November 2026: the offline-first rebuild

### Added
- **The pipeline runs offline:** storage and metrics backends (`STORAGE_BACKEND=s3|local`), a local runner,
  synthetic data with planted SNPs, unit tests and a Docker end-to-end truth check in CI. ([#32](../../pull/32))
- **DeepVariant** as an alternative caller on a local NVIDIA GPU, with timings and accuracy. ([#33](../../pull/33))
- **Pangenome-aware DeepVariant:** a third caller, a chr20 HPRC graph built locally, and a hap.py benchmark on
  HG003 chr20 that reproduces Google's published result. ([#70](../../pull/70))
- **Django web UI** for the offline pipeline: upload, demo samples, stage status, QC, variants, downloads.
  ([#37](../../pull/37))
- **Paired-end input** (`_R1`/`_R2`): trigger pairing, layout-aware stages, `samtools fixmate`, web upload.
  ([#68](../../pull/68))
- **Duplicate marking** with `samtools markdup` in the align stage. ([#53](../../pull/53))
- **Run names** `<sample>-<UTC time>-<hash>`; duplicate S3 events start one run. ([#65](../../pull/65))
- **Tests** for stage I/O, reference indexing, S3 errors and the CloudWatch payload. ([#55](../../pull/55))

### Infrastructure and security (applied)
- **CI/CD IAM matching what is live**, and a manual, plan-only Terraform workflow. ([#34](../../pull/34))
- **Least privilege for CI:** read-only Terraform roles; prod roles trust only `main`. ([#35](../../pull/35))
- **Cost-safe networking:** NAT gateway optional and off, Fargate in the public subnet, ECR lifecycle,
  7-day Step Functions logs, a $10 budget alert. ([#36](../../pull/36))
- **Trigger Lambda read access** to `raw_reads/` for paired-end mate checks. ([#69](../../pull/69))

### Changed
- The last legacy project names removed from the code ([#49](../../pull/49)); the README made accurate and
  more visual ([#50](../../pull/50)), with pangenome-aware calling as the top priority ([#52](../../pull/52)).

## September 2025: Step Functions and the Geyser name
- **Orchestration moved from Airflow to AWS Step Functions.** ([#1](../../pull/1)) The Airflow design is
  tagged `v0.1-airflow`.
- PR workflow validation ([#4](../../pull/4)); the dashboard became a standalone deployer instead of
  Terraform ([#28](../../pull/28)); infrastructure and CI renamed to Geyser ([#29](../../pull/29)); the deploy
  workflow fixed and an OIDC test workflow added ([#30](../../pull/30)).

## June 2025: from a local simulation to AWS Batch
- **Architectural pivot:** from a stateful Airflow worker with a shared volume to stateless jobs on AWS Batch
  that read from and write to S3, with Airflow only orchestrating. The aim was to remove the
  download/process/upload pattern of every task, so that each job is ephemeral, scalable and cheaper.
- The first Terraform (S3 data lake, ECR, Batch), the `tasks.py` CLI with the bioinformatics tasks, and the
  first Dockerfile.
