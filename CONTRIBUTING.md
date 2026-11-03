# Contributing to Geyser Genomics

Geyser is developed **offline first**: the whole pipeline runs locally in Docker against a folder, and the
cloud is a checkpoint, not a development loop. Every change is proven on synthetic data with planted
mutations before it goes anywhere near AWS.

## Set up

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt      # Windows: .venv/Scripts/python
docker build -f Dockerfile -t geyser-app .
```

## The checks every change passes

| Check | Command | When |
|---|---|---|
| Unit tests | `pytest` | every edit; no Docker or AWS needed |
| Lint and format | `ruff check .` and `ruff format --check .` (config in `pyproject.toml`; `ruff format .` fixes formatting); `terraform fmt -check -recursive` in `infrastructure/` | every edit |
| Docker end-to-end | `GEYSER_E2E=1 pytest tests/test_e2e_docker.py` | before a PR; needs the image built |
| Truth check, single-end | `python scripts/make_test_data.py` then `python scripts/run_local.py --sample SYNTH001 --reference synthetic_ref.fa --check` | before a PR |
| Truth check, paired-end | `python scripts/make_test_data.py --sample PAIRED001 --paired` then `python scripts/run_local.py --sample PAIRED001 --reference synthetic_ref.fa --check` | before a PR |
| DeepVariant (CPU image) | `GEYSER_DV_E2E=1 pytest tests/test_e2e_deepvariant.py` | when the caller changes; CI runs it on PRs |
| Giraffe align (`ALIGNER=giraffe`) | `GEYSER_GIRAFFE_E2E=1 pytest tests/test_e2e_giraffe.py` (needs geyser-app and `docker build -f app-giraffe/Dockerfile -t geyser-giraffe .`) | when the align stage or app-giraffe changes; CI runs it on PRs |

`--check` must report all planted SNPs called and no unexpected calls. CI (`.github/workflows/tests.yml`)
runs the lint job (ruff, `terraform fmt`, `terraform validate -backend=false`), the unit tests and the Docker
end-to-end test on every push and pull request, with no AWS credentials.
Changes to a caller or aligner are also benchmarked with `scripts/benchmark_happy.py` (hap.py against the
GIAB truth set) before they merge.

## Working on a change

1. **Start from an issue.** Each issue has an outcome-style title, a `## Context / ## Tasks / ## Done when`
   body (see `.github/ISSUE_TEMPLATE/`), a milestone and labels. Decisions made along the way get their own
   issue from the decision template, closed once decided, and linked from the PR.
2. **Branch from `main`**, named for the kind of change: `feature/<slug>`, `infra/<slug>`, `docs/<slug>`,
   `test/<slug>`, `fix/<slug>`. To work on several branches at once, give each its own worktree:

   ```bash
   git worktree add ../geyser-worktrees/<slug> -b feature/<slug> origin/main
   ```

   `git worktree remove` deletes ignored files too (local data, `terraform.tfvars`), so keep anything you
   need outside the worktree.
3. **Commits** are imperative and prefixed by the component they change: `Align stage: ...`, `Trigger: ...`,
   `Storage: ...`, `Tests: ...`, `README: ...`, `CI: ...`. One logical change each; fixes from a review go
   in a `Review fixes: ...` commit. Issue numbers go in the PR, not in commit subjects.
4. **Keep the stage contract in step.** Stages talk to each other only through S3 key conventions. If you
   rename a stage, change a key prefix or add a CLI argument, update `app/tasks.py`, the state machine in
   `infrastructure/step_functions.tf`, the trigger Lambda and the scripts together. `tests/test_consistency.py`
   checks what it can.
5. **Docs in the same PR**: the README section, and the `docs/design/` page if there is one.
6. **Open the PR** from the template (`.github/pull_request_template.md`):
   - `## Summary`
   - `### What changed`, per file or component
   - `### Testing`: what ran, with a results table for the end-to-end and truth checks
   - `## Related issues`: `Closes #N` / `Part of #N` / `Refs #N`
   - `## Dissertation`: the project is also written up as a report. Note the chapter (design / implementation
     / evaluation), one paragraph, and the script, CSV or run behind any number, or write "None".

## Infrastructure changes

Anything under `infrastructure/` (and `.github/workflows/deploy-platform.yml`) is reviewed differently:

- It goes in its own PR, separate from application changes.
- The PR includes a `terraform plan` summary, and the full plan is reviewed before anything is applied.
  It merges only after it has been applied and AWS matches the branch.
- Watch every plan for a replace or destroy of a compute environment, job queue, S3 bucket, VPC or
  `random_id`; that stops the change.
- Nothing that bills by the hour while idle (NAT gateways, for example) goes in without an explicit decision.

Dependabot's monthly Terraform update PRs follow the same rules (see `.github/dependabot.yml`).

## Secrets and data

- Never commit `*.tfvars`, `*.tfplan`, credentials, account IDs, ARNs or email addresses. They are
  gitignored or kept out of tracked files; see [SECURITY.md](SECURITY.md).
- Never commit genomic data (`*.fastq.gz`, `*.bam`, `*.vcf.gz`); tests generate their own synthetic data.
