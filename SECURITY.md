# Security policy

## Reporting a vulnerability

Please report security problems **privately**, not in a public issue or pull request. Use GitHub's
private vulnerability reporting: the repository's **Security** tab, then **Report a vulnerability**.
Include what you found, where (file, resource or workflow), and how to reproduce it.

You can expect an acknowledgement within a week. Fixes are made on a private branch and released once a
fix is merged and, for infrastructure, applied.

## Supported versions

Only `main` is supported. There are no maintained release branches.

## Scope

In scope:

- The Terraform configuration in `infrastructure/`: IAM roles and policies, the GitHub OIDC trust, S3
  bucket access, the Step Functions state machine, the trigger Lambda, AWS Batch and networking.
- The GitHub Actions workflows in `.github/workflows/`.
- The pipeline code in `app/`, `app-deepvariant/` and `lambda/`, and the container images built from them.
- The Django web UI in `web/` (it is built to run locally; it is not deployed).

Out of scope: vulnerabilities in the third-party tools and images the pipeline uses (BWA, bwa-mem2,
samtools, bcftools, fastp, DeepVariant). Please report those upstream.

## How the project protects itself

- **No long-lived cloud credentials in CI.** GitHub Actions authenticates to AWS with OIDC. The Terraform
  roles are read-only, and production roles only trust the `main` branch. The test workflow uses no AWS
  credentials at all.
- **No secrets in tracked files.** Terraform variables (`*.tfvars`), plans (`*.tfplan`), credentials,
  account IDs, ARNs and email addresses stay out of the repository. Plans and applies run locally after
  review.
- **Cost guardrails.** Batch compute capacity is capped, jobs have attempt timeouts, there is no NAT
  gateway by default, and budgets alert on spend (see `docs/design/claude-guardrails.md` for the automated
  agent's role and its limits).
- **Pinned dependencies.** Python packages are pinned exactly and updated through reviewed, tested
  Dependabot PRs.
