# Design: the DeepVariant split, infra half (v1.5)

Status: **design only. Nothing is built yet.**
- The app half is #228 (`feature/dv-split`, open), with its contract in `docs/design/deepvariant-split.md`. This
  PR follows it the way #229 followed #225: build it here, merge main in once #228 lands, then plan.
- The GPU environment change (g6.4xlarge, max 16 vCPU, a bigger disk) is a **separate PR, applied by the user**.
  Compute-environment changes are outside geyser-claude's boundary.

## Resources (new)

- **`aws_ecr_repository.geyser_deepvariant_cpu`** (`geyser-deepvariant-cpu`):
  - scan on push;
  - a lifecycle policy keeping 2 images (~7.4 GB uncompressed each).
- **`aws_batch_job_definition.geyser_deepvariant_cpu_job_def`:**
  - **Placement:** Fargate, on the shared queue (Spot first, like the bcftools shards).
  - **Roles:** the batch task role, which already reads and writes `intermediate/*`.
  - **Tags:** `propagate_tags = true`.
  - **Size:** `var.dv_part_resources`, default 8 vCPU / 16 GiB. A chr20 part peaked at ~1.1 GB RSS with 8
    tasks; confirm on the canary.
  - **Disk:** `ephemeralStorage = var.dv_part_disk_gb`. Each part downloads the whole CRAM (~17 GB for a 30x
    genome), the reference and its examples, so the default is 100 and the range is 21-200.
  - **Environment:** the same `DEEPVARIANT_*` limits as the GPU job definition, except `DEEPVARIANT_SHARDS`,
    which the state machine sets per run. Neither job definition sets `DEEPVARIANT_MODEL_TYPE` or
    `DEEPVARIANT_EXTRA_ARGS`, so the GPU job's check 4 holds. A consistency test should pin that they agree.
  - **Retry:** infrastructure failures only, as the other job definitions.
- **The SFN policy** may submit the new job definition (both ARN forms). Nothing else in IAM changes.

## The run setting `dv_parts`

A per-run override, like v1.3's `merge_threads`.
- **Default:** `var.deepvariant_split_parts = 1`, today's single GPU job, so nothing changes until a run asks.
- **Bounds:** 1 to `dv_parts_max = floor(fargate_spot_max_vcpus / dv_part_resources.vcpu)`, which is 48 / 8 = 6.
- **Where it goes:**
  - into `Run_Setting_Rules.defaults` (`setting_count` 7 → 8) and the `_max` bounds;
  - through `Check_Run_Setting_Types` (IsNumeric), `Inspect_Run_Setting_Values` (`dv_parts_text`) and
    `Check_Run_Setting_Values` (1..max, a whole number: no `.`, `E` or `e`);
  - into `Prepare_Run_Overrides_Error`'s Cause;
  - into `Set_Run_Settings`, where `dv_parts` stays a number, for `States.ArrayRange`.
- **`DEEPVARIANT_SHARDS` = parts × part vCPUs.** Step Functions has no multiply, so Terraform renders
  `dv_shards_by_parts = [0, 8, 16, …]`, indexed by parts, and the state machine reads it with
  `States.ArrayGetItem`. The parts never depend on the CPU count Fargate reports.

## States

- **`Choose_Caller`:** deepvariant → **`Choose_DeepVariant_Mode`**. `$.run_settings.dv_parts > 1` goes to
  `Prepare_Examples_Parts`; otherwise `Prepare_DeepVariant_Command`, unchanged.
- **`Prepare_Examples_Parts`** (Pass):
  - `parts = States.ArrayRange(0, States.MathAdd(dv_parts, -1), 1)`;
  - `parts_text`;
  - `shards_text = States.Format('{}', States.ArrayGetItem(dv_shards_by_parts, dv_parts))`.
  Check in Step Functions Local that it accepts nested `ArrayRange`, `MathAdd` and `ArrayGetItem`.
- **`Make_Examples_Parts`** (Map, legacy `Parameters`/`Iterator`, `MaxConcurrencyPath $.run_settings.dv_parts`):
  - **Job name:** `DeepVariant-Examples-{part}-{exec}`. The existing `("DeepVariant-", "DeepVariant")` entry in
    `provenance.JOB_NAME_STAGES` covers it, so no app change is needed.
  - **Command:** `deepvariant_task.py <id> <ref> --make-examples-part P --parts J`.
  - **Environment:** `RUN_NAME`, `PIPELINE_CALLER=deepvariant`, `DEEPVARIANT_SHARDS`.
  - **Task state:** `"Tags": { "Stage": "deepvariant_examples" }` (#149's rule); attempt limit
    `var.dv_part_timeout_seconds`, with `DEEPVARIANT_TIMEOUT_SECONDS` 300 s under it.
  - **Catch:** to `Prepare_Plan_Command`, with `$.deepvariant_error`.
- **`Prepare_DeepVariant_From_Examples_Command`** → the existing **`DeepVariant`** Task. The command is the
  single job's plus `--from-examples`, with the same environment. It reuses the Task, its Catch (the bcftools
  fallback) and its Stage tag.

**Deviation from #228's note, to confirm with T2:** a failed part falls back to bcftools, as a failed GPU job
does. #228's note sends it to `Prepare_Run_Close` instead. The fallback keeps the sample called, and matches
DeepVariant being an extra caller whose failure never fails the run (`provenance.EXTRA_CALLERS`).

## Expected plan (estimated before building; re-state on the PR before planning)

**3 to add, 2 to change, 0 to destroy.**
- **Add:** the ECR repo and its lifecycle policy, and the CPU job definition.
- **Change:** the state machine and the SFN policy.
- Plus 1 change if main has #149 by then and the DV job definition's environment is refactored. The
  recommendation is not to touch it.

## Tests to write

- **Step Functions Local:**
  - `dv_parts` 1 runs today's path;
  - `dv_parts` 3 gives 3 part jobs with the right commands, shards text and job names, then the GPU job with
    `--from-examples`;
  - a failed part falls back to bcftools;
  - out-of-bounds `dv_parts` (0, 7, 2.5, "3") fails before any job;
  - add the `MaxConcurrencyPath` entry to `CONCURRENCY_PATHS` and `for_sfn_local`.
- **Consistency:** `dv_shards_by_parts` matches `dv_part_resources`; both job definitions' `DEEPVARIANT_*`
  agree; the CPU repo and job definition are referenced; `setting_count` is 8.
