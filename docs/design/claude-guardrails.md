# Guardrails for Claude Code on AWS (#73)

Claude Code may run `terraform` and `aws` for this project, provided the project as a whole never costs
more than **$20 of AWS spend from 2 Nov 2026**. Destroying or replacing stateful resources (data lake,
Terraform state, VPC, compute environments, queues) stays the user's decision.

No single control can guarantee a dollar cap: AWS billing data lags by roughly 8-24 hours, so anything
that reacts to spend reacts late. The guardrails therefore combine three layers: a cap on how fast money
can be spent, an automatic stop, and a local check before each command.

## Layer 1: the `geyser-claude` role (AWS-enforced)

`infrastructure/claude_guardrails.tf` creates a role that only the user's IAM user (`var.claude_role_trusted_user`)
can assume, for at most 1 hour per session. It has AdministratorAccess capped by the permissions boundary
`geyser-claude-boundary`. Whatever a session asks for, it gets the intersection of the two.

| Boundary statement | Effect |
|---|---|
| `ProjectServicesOnly` | Allows only the services this project uses: S3, Batch, ECS (read-only), EC2, ECR, Step Functions, Lambda, Logs, CloudWatch, EventBridge, SNS, IAM, DynamoDB, plus read-only Budgets, Pricing, Health, tagging and Service Quotas. RDS, SageMaker, EKS, Savings Plans, Marketplace, Organizations, account settings and Cost Explorer's paid API ($0.01 a request) are outside it. |
| `EuWest2Only` | Denies every regional call outside eu-west-2. |
| `NoIdleBillingOrPurchases` | Denies NAT gateways, Elastic IPs, VPC/VPN/Transit endpoints, dedicated hosts, capacity reservations and up-front purchases. |
| `NoDirectInstances` | Denies launching EC2 instances directly. Batch launches GPU instances with its own service role, so direct launches would only bypass `max_vcpus`. |
| `NoComputeEnvChangesOrStatefulDeletes` | Denies creating, updating or deleting Batch compute environments (they set the spend rate), deleting job queues, buckets, the lock table, ECR repositories, the VPC, subnets and the internet gateway. |
| `ProtectTerraformState` | Denies deleting objects or changing policy, lifecycle or versioning on the state bucket. Reads and writes, which terraform needs, are allowed. |
| `NoNewCredentialsOrRoleHopping` | Denies IAM users, access keys, console logins, removing permissions boundaries, and `sts:AssumeRole*`, so a session can't move to an unbounded role. |
| `GuardrailsAreReadOnly` | The role, the boundary, the kill switch and the budget-action role can be read (so `terraform plan` works) but not changed. Changes to the guardrails are applied by the user. |

**What this protects against, and what it doesn't.** It stops mistakes and runaway spend by a well-meaning agent. It is not
a defence against a hostile principal. For example, IAM is allowed (terraform manages roles), so the role
could create a service role without a boundary and attach it to a Lambda.

## Layer 2: spend-rate caps

| Capacity | Cap | Worst case if fully used (eu-west-2 list prices, approximate) |
|---|---|---|
| Fargate compute environment | `max_vcpus = var.fargate_max_vcpus` (8, was 16): 8 vCPUs in any mix of stage sizes (align 4 vCPU / 8 GiB, QC and a variant shard 1 / 2 GiB, planner and merge 0.25 / 0.5 GiB) | 8 vCPU + 16 GB ≈ **$0.45/h** |
| Fargate Spot compute environment (#124, the queue's first choice) | `max_vcpus = var.fargate_spot_max_vcpus` (8) | at Spot prices (~70% off) ≈ **$0.14/h** |
| GPU compute environment | `max_vcpus = var.gpu_max_vcpus` (16 since v1.5, was 4, before that 64): one g6.4xlarge or g5.4xlarge (`gpu_instance_types`); a precondition keeps the cap at most the smallest type's vCPUs, so it is always one instance | ≈ **$2.1/h** with a 500 GB EBS root (g6 is about 20% less) |
| Public IPv4 per task or instance | | ~$0.005/h each |
| **Total at all caps** | | **≈ $2.7/h** |

The Spot environment adds capacity rather than moving it: jobs go to Spot first and overflow to on-demand
Fargate only when Spot is full or has no capacity, so at both Fargate caps 16 vCPUs can run. Creating a
compute environment is outside the `geyser-claude` boundary, so the user applies that change.

Changing `max_vcpus` is an in-place update. In provider 5.100.0 `max_vcpus` has no ForceNew, and the
CustomizeDiff that forces replacement for environments with a custom service role covers only instance
types, subnets, security groups, allocation strategy and similar fields. The plan must show
`~ update in-place` for `aws_batch_compute_environment.gpu_env` and `aws_batch_compute_environment.geyser_fargate`. **Stop if it shows a replacement.**

Every environment scales to zero when idle (`min_vcpus = 0`). Every Batch job has an attempt timeout (the
gate 15 min, the region planner 30 min, QC and DeepVariant 1 h, variant shards and the merge 2 h, align 4 h),
and the whole run has one too (`pipeline_timeout_hours`, 12 h), so nothing runs indefinitely without a queue
of work.

## Layer 3: the $20 budget and its kill switch (AWS-enforced, delayed)

- Budget `geyser-claude-project-cap`: COST, $20, **account-wide** (it also counts the idle teraflow stack
  and S3 storage, which is deliberately conservative), `CUSTOM` period from `2026-11-02_00:00` to
  `2027-11-01_00:00` (12 months). AWS limits custom COST periods in ways its API docs don't list (both found as
  rejected applies): at most 38 months, and longer than ~14 months only with Cost Explorer's multi-year
  historical data enabled. AWS deletes a budget after its end date, so **renew it before 1 Nov 2027**.
- **Why CUSTOM (#76):** the first version had to be `ANNUALLY`, because provider 5.100.0 predates the `CUSTOM`
  period AWS added in September 2025. AWS calculated an annual budget's actual spend over the whole calendar
  year, whatever the start date: $352.16 (Jan-Apr 2026), so the $8 action attached the kill switch the moment
  it was created. The fail-safe behaved correctly; the period was wrong. Provider 6.69.0 accepts `CUSTOM`.
- Emails at 50% and 80% actual and 100% forecast, to `var.budget_alert_email`.
- **Budget action:** when actual spend passes `var.claude_kill_switch_usd` ($8), AWS Budgets
  automatically attaches the deny-all policy `geyser-claude-kill-switch` to the role. An explicit deny wins
  over everything, including sessions already open.
- Cost: regular budgets are free, and the first two action-enabled budgets are free (AWS Budgets pricing).
  This is the account's only action-enabled budget.

### What the billing lag means

Budgets refresh a few times a day, and cost data can trail usage by up to about 24 hours. If both
environments ran flat out from the moment spend passed $8:

| Lag before the action fires | Spend when it fires |
|---|---|
| 8 h | $8 + 8 × $1.85 ≈ **$23** |
| 24 h | $8 + 24 × $1.85 ≈ **$52** |

So the **kill switch alone does not hold a $20 line** against sustained full-capacity use. It also stops
only geyser-claude: jobs already queued keep running until they finish or time out. The $20 holds because of
the other layers:

- realistic work is far below the caps (a synthetic or small-sample run costs cents; see the
  `cloud-runs` notes);
- the GPU environment only runs when a run asks for it (`caller=deepvariant`; the default caller is bcftools),
  and at most one GPU instance runs at a time (`gpu_max_vcpus` = 16 with only 16-vCPU types, enforced by a
  precondition in batch_gpu.tf);
- the local hook (below) refuses new runs when the budget is close;
- the caps were tightened on 2 Nov 2026 (GPU 64 → 4, Fargate 16 → 8, kill switch $12 → $8) at the user's request;
  full use of both for a whole day is what the hook below exists to prevent.

## Local hook (Claude Code, not Terraform)

A PreToolUse hook in `.claude/` (outside this repo's tracked files, `guard_aws.py`) acts before Claude's own
commands, so it trips **before** the kill switch, on spend AWS has already recorded:

- **No new runs at $6 actual:** refuses uploads to `raw_reads/` (which start the pipeline), Step Functions
  `start-execution`/`redrive-execution` and Batch `submit-job` once the budget's actual spend is $6 or more,
  or when the budget can't be read (fails closed).
- **One run at a time:** refuses starting a run while a pipeline execution is `RUNNING`.
- **GPU jobs only below $4 actual:** refuses GPU jobs once actual spend is $4 or more. Until 2 Nov 2026 it refused all GPU jobs; the user lifted that once #125 had
  moved the GPU environment to the public subnet, where it can pull its image.
- refuses `terraform destroy`, and any apply whose saved-plan check shows a destroy or replace of a bucket,
  the VPC, a compute environment, a queue or `random_id`, or a new NAT gateway.

It only sees commands Claude types: boto3 scripts and other indirect calls are bounded by layers 1-3.

## Setting it up (the user)

1. Apply the plan from the `infra/claude-guardrails` PR, which you review first.
2. Add a profile to `~/.aws/config` (role ARN from `terraform output claude_role_arn`):

   ```ini
   [profile geyser-claude]
   role_arn = <claude_role_arn>
   source_profile = default
   region = eu-west-2
   duration_seconds = 3600
   ```

3. Check it: `aws sts get-caller-identity --profile geyser-claude` should print the `geyser-claude` role.
4. Claude Code uses that profile (`AWS_PROFILE=geyser-claude` in its settings), never the default one.

## If the kill switch fires

1. You get the budget-action email. Claude's AWS calls now fail with AccessDenied.
2. Look at what is running: Batch queues, Step Functions executions (as yourself, not geyser-claude).
   To stop new work starting, disable the queues: `aws batch update-job-queue --job-queue <name> --state DISABLED`.
3. Find the cause in Cost Explorer (it lags; check again the next day).
4. To restore access, detach `geyser-claude-kill-switch` from the role (IAM console, or
   `aws iam detach-role-policy`), and reset the budget action in the Budgets console. Raise
   `claude_project_cap_usd` and `claude_kill_switch_usd` only as a deliberate decision.
