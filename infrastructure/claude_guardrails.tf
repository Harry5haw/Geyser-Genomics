# infrastructure/claude_guardrails.tf
# Guardrails that let Claude Code run terraform and aws commands for this project (#73).
#
# Three layers (docs/design/claude-guardrails.md):
#   1. Role geyser-claude: AdministratorAccess capped by the permissions boundary below.
#      Only the user's IAM user can assume it.
#   2. Spend-rate cap: GPU Batch env max_vcpus (var.gpu_max_vcpus, batch_gpu.tf). Claude
#      cannot create, update or delete compute environments, so the cap holds.
#   3. Kill switch: a $20 project budget counted from 2 Nov 2026. A budget action attaches
#      a deny-all policy to geyser-claude once actual spend passes var.claude_kill_switch_usd.
#
# What this does and doesn't protect against: it stops mistakes and runaway spend by a
# well-meaning agent. It is not a defence against a hostile principal: the role can still,
# for example, create a service role without a boundary and attach it to a Lambda.

locals {
  claude_role_name        = "${var.project_name}-claude"
  claude_role_arn         = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${local.claude_role_name}"
  claude_boundary_name    = "${var.project_name}-claude-boundary"
  claude_kill_switch_name = "${var.project_name}-claude-kill-switch"
  budget_action_role_name = "${var.project_name}-budget-action-role"
  claude_project_cap_name = "${var.project_name}-claude-project-cap"
  iam_policy_arn_prefix   = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:policy"
  claude_guardrail_iam_arns = [
    local.claude_role_arn,
    "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${local.budget_action_role_name}",
    "${local.iam_policy_arn_prefix}/${local.claude_boundary_name}",
    "${local.iam_policy_arn_prefix}/${local.claude_kill_switch_name}",
  ]
}

# ---------------------------------------------------------------------------
# Layer 1: the role and its permissions boundary
# ---------------------------------------------------------------------------

resource "aws_iam_policy" "claude_boundary" {
  name        = local.claude_boundary_name
  description = "Caps what geyser-claude can do: project services only, eu-west-2, no costly or irreversible actions (#73)."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Only the services this project uses. Anything else (RDS, SageMaker, EKS, Savings
        # Plans, Marketplace, Organizations, account settings, Cost Explorer's paid API...) is
        # outside the boundary and therefore denied.
        Sid    = "ProjectServicesOnly"
        Effect = "Allow"
        Action = [
          # ecs read-only: Batch runs Fargate tasks with its own role; ecs:RunTask would bypass
          # the compute environment's vCPU cap.
          "s3:*", "batch:*", "ecs:Describe*", "ecs:List*", "ec2:*", "ecr:*", "states:*", "lambda:*", "logs:*",
          "cloudwatch:*", "events:*", "sns:*", "iam:*", "sts:GetCallerIdentity", "dynamodb:*",
          "budgets:View*", "budgets:Describe*", "budgets:ListTagsForResource", "pricing:*", "health:Describe*",
          "tag:Get*", "servicequotas:Get*", "servicequotas:List*",
        ]
        Resource = "*"
      },
      {
        Sid       = "EuWest2Only"
        Effect    = "Deny"
        NotAction = ["iam:*", "sts:*", "budgets:*", "pricing:*", "health:*", "s3:ListAllMyBuckets"]
        Resource  = "*"
        Condition = { StringNotEquals = { "aws:RequestedRegion" = "eu-west-2" } }
      },
      {
        # Things that bill by the hour while idle, or commit money up front.
        Sid    = "NoIdleBillingOrPurchases"
        Effect = "Deny"
        Action = [
          "ec2:CreateNatGateway", "ec2:AllocateAddress", "ec2:CreateVpcEndpoint",
          "ec2:CreateClientVpnEndpoint", "ec2:CreateVpnConnection", "ec2:CreateTransitGateway",
          "ec2:AllocateHosts", "ec2:CreateCapacityReservation", "ec2:PurchaseReservedInstancesOffering",
          "ec2:PurchaseScheduledInstances", "ec2:PurchaseHostReservation",
        ]
        Resource = "*"
      },
      {
        # Batch launches GPU instances with its own service role, so geyser-claude never needs to.
        # Launching directly would bypass the compute environment's max_vcpus cap.
        Sid    = "NoDirectInstances"
        Effect = "Deny"
        Action = [
          "ec2:RunInstances", "ec2:StartInstances", "ec2:RequestSpotInstances", "ec2:RequestSpotFleet",
          "ec2:CreateFleet", "ec2:ModifyInstanceAttribute",
        ]
        Resource = "*"
      },
      {
        # Compute environments set the spend rate (instance types, max vCPUs) and replacing one
        # is a stop-the-plan event: they stay the user's. So do deletions of stateful resources.
        Sid    = "NoComputeEnvChangesOrStatefulDeletes"
        Effect = "Deny"
        Action = [
          "batch:CreateComputeEnvironment", "batch:UpdateComputeEnvironment",
          "batch:DeleteComputeEnvironment", "batch:DeleteJobQueue",
          "s3:DeleteBucket", "dynamodb:DeleteTable", "ecr:DeleteRepository",
          "ec2:DeleteVpc", "ec2:DeleteSubnet", "ec2:DeleteInternetGateway",
        ]
        Resource = "*"
      },
      {
        Sid    = "ProtectTerraformState"
        Effect = "Deny"
        Action = [
          "s3:DeleteObject", "s3:DeleteObjectVersion", "s3:PutBucketPolicy", "s3:DeleteBucketPolicy",
          "s3:PutLifecycleConfiguration", "s3:PutBucketVersioning",
        ]
        Resource = ["arn:aws:s3:::${local.tf_state_bucket}", "arn:aws:s3:::${local.tf_state_bucket}/*"]
      },
      {
        # No long-lived credentials, no hopping to a role without this boundary.
        Sid    = "NoNewCredentialsOrRoleHopping"
        Effect = "Deny"
        Action = [
          "iam:CreateUser", "iam:CreateAccessKey", "iam:CreateLoginProfile", "iam:UpdateLoginProfile",
          "iam:AttachUserPolicy", "iam:PutUserPolicy", "iam:AddUserToGroup",
          "iam:DeleteRolePermissionsBoundary", "iam:DeleteUserPermissionsBoundary",
          "sts:AssumeRole", "sts:AssumeRoleWithWebIdentity", "sts:AssumeRoleWithSAML",
        ]
        Resource = "*"
      },
      {
        # The guardrails themselves can be read (so terraform plan works) but not changed.
        Sid    = "GuardrailsAreReadOnly"
        Effect = "Deny"
        Action = [
          "iam:Attach*", "iam:Detach*", "iam:Put*", "iam:Delete*", "iam:Update*", "iam:Create*",
          "iam:Tag*", "iam:Untag*", "iam:SetDefaultPolicyVersion", "iam:PassRole",
        ]
        Resource = local.claude_guardrail_iam_arns
      },
    ]
  })
}

resource "aws_iam_role" "claude" {
  name                 = local.claude_role_name
  description          = "Claude Code's role for terraform/aws in this project. Capped by ${local.claude_boundary_name} and the project budget kill switch (#73)."
  max_session_duration = 3600
  permissions_boundary = aws_iam_policy.claude_boundary.arn

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
      Action    = "sts:AssumeRole"
      Condition = {
        ArnEquals = {
          "aws:PrincipalArn" = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:user/${var.claude_role_trusted_user}"
        }
      }
    }]
  })

  tags = {
    Project = var.project_name
    Purpose = "claude-code-guardrails"
  }
}

resource "aws_iam_role_policy_attachment" "claude_admin" {
  role       = aws_iam_role.claude.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

# ---------------------------------------------------------------------------
# Layer 3: $20 project budget with an automatic kill switch
# ---------------------------------------------------------------------------

resource "aws_iam_policy" "claude_kill_switch" {
  name        = local.claude_kill_switch_name
  description = "Deny-all, attached to geyser-claude by the project budget action when spend passes the kill-switch threshold (#73). Detach it by hand to restore access."

  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Sid = "BudgetExhausted", Effect = "Deny", Action = "*", Resource = "*" }]
  })
}

resource "aws_iam_role" "budget_action" {
  name        = local.budget_action_role_name
  description = "Lets AWS Budgets attach the kill switch to geyser-claude (#73)."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "budgets.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "budget_action" {
  name = "${var.project_name}-budget-action-attach-kill-switch"
  role = aws_iam_role.budget_action.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = ["iam:AttachRolePolicy", "iam:DetachRolePolicy"]
      Resource  = aws_iam_role.claude.arn
      Condition = { ArnEquals = { "iam:PolicyARN" = aws_iam_policy.claude_kill_switch.arn } }
    }]
  })
}

# Account-wide (no cost filters), so it also counts the idle teraflow stack and S3 storage:
# conservative on purpose. CUSTOM period (provider 6.x, #76): counts only spend from the start date,
# never resets. ANNUALLY (the first version) counted the whole calendar year, incl. Jan-Apr 2026's $352,
# and fired the kill switch on creation. AWS limits CUSTOM COST periods: at most 38 months, and over ~14 months
# only with Cost Explorer multi-year data enabled; 12 months needs neither. AWS deletes a budget after its end
# date: renew this before 1 Nov 2027.
resource "aws_budgets_budget" "claude_project_cap" {
  name              = local.claude_project_cap_name
  budget_type       = "COST"
  limit_amount      = format("%.2f", var.claude_project_cap_usd)
  limit_unit        = "USD"
  time_unit         = "CUSTOM"
  time_period_start = var.claude_project_cap_start
  time_period_end   = "2027-11-01_00:00"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 50
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_alert_email]
  }
}

# Free: the first two action-enabled budgets per account are free (AWS Budgets pricing).
resource "aws_budgets_budget_action" "claude_kill_switch" {
  budget_name        = aws_budgets_budget.claude_project_cap.name
  action_type        = "APPLY_IAM_POLICY"
  approval_model     = "AUTOMATIC"
  notification_type  = "ACTUAL"
  execution_role_arn = aws_iam_role.budget_action.arn

  action_threshold {
    action_threshold_type  = "ABSOLUTE_VALUE"
    action_threshold_value = var.claude_kill_switch_usd
  }

  definition {
    iam_action_definition {
      policy_arn = aws_iam_policy.claude_kill_switch.arn
      roles      = [aws_iam_role.claude.name]
    }
  }

  subscriber {
    address           = var.budget_alert_email
    subscription_type = "EMAIL"
  }

  depends_on = [aws_iam_role_policy.budget_action]
}
