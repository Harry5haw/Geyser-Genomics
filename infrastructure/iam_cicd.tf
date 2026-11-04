# infrastructure/iam_cicd.tf
# IAM roles for GitHub Actions OIDC (dev + prod side-by-side)

locals {
  gh_owner      = "Harry5haw"
  gh_repo       = "Geyser-Genomics"
  gh_sub_prefix = "repo:${local.gh_owner}/${local.gh_repo}"

  # OIDC subjects allowed to assume each environment's roles.
  # The repo's sub claim is customised to include_claim_keys = [repo, context, ref],
  # so a token from an environment job looks like
  #   repo:<owner>/<repo>:environment:prod:ref:refs/heads/main
  # Dev accepts any branch; prod accepts only main.
  # The sub customisation is a GitHub repo setting, not Terraform. Check it with:
  #   gh api repos/<owner>/<repo>/actions/oidc/customization/sub
  # If it is ever reset to the default, these subjects stop matching and CI
  # cannot assume the roles until it is set back.
  oidc_subs_dev = [
    "${local.gh_sub_prefix}:environment:dev:ref:refs/heads/*",
  ]
  oidc_subs_prod = [
    "${local.gh_sub_prefix}:environment:prod:ref:refs/heads/main",
  ]

  # Terraform backend (must match backend.tf and backend-<env>.hcl, which cannot use variables).
  tf_state_bucket = "geyser-tfstate-${data.aws_caller_identity.current.account_id}-${data.aws_region.current.region}"
  tf_state_keys   = ["env/dev/terraform.tfstate", "env/prod/terraform.tfstate"] # one state per environment (#45)
  tf_lock_table   = "geyser-tf-lock"
}

# The account's GitHub OIDC provider. It was created by the retired teraflow stack and only read here;
# with teraflow torn down (#46), geyser owns it. The import block adopts the live provider into this
# state without changing it: every argument below matches what AWS reports (checked 2 Nov 2026), so
# the plan must say "1 to import, 0 to add, 0 to change, 0 to destroy". Order, all by the user:
# (1) `terraform state rm` it from the teraflow state, (2) destroy teraflow, (3) plan and apply this.
# If teraflow is destroyed first, the provider is deleted and every OIDC role below stops working.
import {
  to = aws_iam_openid_connect_provider.github
  id = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/token.actions.githubusercontent.com"
}

resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]

  lifecycle {
    # Four CI roles (and everything CI does in AWS) trust this provider.
    prevent_destroy = true
  }
}

# ---------------------------
# ECR roles & policies
# ---------------------------

# DEV ECR role
resource "aws_iam_role" "geyser_github_ecr_role_dev" {
  name = "geyser-github-ecr-role-dev"

  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect    = "Allow",
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn },
      Action    = "sts:AssumeRoleWithWebIdentity",
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        },
        StringLike = {
          "token.actions.githubusercontent.com:sub" = local.oidc_subs_dev
        }
      }
    }]
  })
}

resource "aws_iam_policy" "geyser_github_ecr_policy_dev" {
  name = "geyser-github-ecr-policy-dev"

  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Effect   = "Allow",
        Action   = ["ecr:GetAuthorizationToken"],
        Resource = "*"
      },
      {
        Effect = "Allow",
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:CompleteLayerUpload",
          "ecr:InitiateLayerUpload",
          "ecr:PutImage",
          "ecr:UploadLayerPart",
          "ecr:BatchGetImage",
          "ecr:DescribeRepositories"
        ],
        Resource = aws_ecr_repository.geyser_app.arn
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "github_ecr_attach_dev" {
  role       = aws_iam_role.geyser_github_ecr_role_dev.name
  policy_arn = aws_iam_policy.geyser_github_ecr_policy_dev.arn
}

# PROD ECR role
resource "aws_iam_role" "geyser_github_ecr_role_prod" {
  name = "geyser-github-ecr-role-prod"

  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect    = "Allow",
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn },
      Action    = "sts:AssumeRoleWithWebIdentity",
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
          "token.actions.githubusercontent.com:sub" = local.oidc_subs_prod
        }
      }
    }]
  })
}

resource "aws_iam_policy" "geyser_github_ecr_policy_prod" {
  name = "geyser-github-ecr-policy-prod"

  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Effect   = "Allow",
        Action   = ["ecr:GetAuthorizationToken"],
        Resource = "*"
      },
      {
        Effect = "Allow",
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:CompleteLayerUpload",
          "ecr:InitiateLayerUpload",
          "ecr:PutImage",
          "ecr:UploadLayerPart",
          "ecr:BatchGetImage",
          "ecr:DescribeRepositories"
        ],
        Resource = aws_ecr_repository.geyser_app.arn
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "github_ecr_attach_prod" {
  role       = aws_iam_role.geyser_github_ecr_role_prod.name
  policy_arn = aws_iam_policy.geyser_github_ecr_policy_prod.arn
}

# ---------------------------
# Terraform roles
# ---------------------------

# DEV Terraform role
resource "aws_iam_role" "geyser_github_terraform_role_dev" {
  name = "geyser-github-terraform-role-dev"

  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect    = "Allow",
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn },
      Action    = "sts:AssumeRoleWithWebIdentity",
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        },
        StringLike = {
          "token.actions.githubusercontent.com:sub" = local.oidc_subs_dev
        }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "github_terraform_readonly_attach_dev" {
  role       = aws_iam_role.geyser_github_terraform_role_dev.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

resource "aws_iam_role_policy_attachment" "github_terraform_plan_attach_dev" {
  role       = aws_iam_role.geyser_github_terraform_role_dev.name
  policy_arn = aws_iam_policy.geyser_github_terraform_plan_policy.arn
}

# PROD Terraform role
resource "aws_iam_role" "geyser_github_terraform_role_prod" {
  name = "geyser-github-terraform-role-prod"

  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect    = "Allow",
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn },
      Action    = "sts:AssumeRoleWithWebIdentity",
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
          "token.actions.githubusercontent.com:sub" = local.oidc_subs_prod
        }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "github_terraform_readonly_attach_prod" {
  role       = aws_iam_role.geyser_github_terraform_role_prod.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

resource "aws_iam_role_policy_attachment" "github_terraform_plan_attach_prod" {
  role       = aws_iam_role.geyser_github_terraform_role_prod.name
  policy_arn = aws_iam_policy.geyser_github_terraform_plan_policy.arn
}

# ---------------------------
# Terraform plan policy (shared by the dev and prod terraform roles)
# ---------------------------
# The CI terraform roles only ever run `terraform plan`. ReadOnlyAccess covers
# refreshing resources; this adds what the S3 backend needs to read state and
# take the DynamoDB lock, and blocks reading genomic data from the data lake.
# No s3:PutObject: plan never writes state. State and lock table use AWS-managed
# encryption (AES256 / AWS-owned key), so no kms:Decrypt is needed.
resource "aws_iam_policy" "geyser_github_terraform_plan_policy" {
  name        = "geyser-github-terraform-plan-policy"
  description = "Read Terraform state and take the plan lock; deny reading data-lake objects."

  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Sid      = "StateBucketList",
        Effect   = "Allow",
        Action   = "s3:ListBucket",
        Resource = "arn:aws:s3:::${local.tf_state_bucket}"
      },
      {
        Sid      = "StateObjectRead",
        Effect   = "Allow",
        Action   = "s3:GetObject",
        Resource = [for key in local.tf_state_keys : "arn:aws:s3:::${local.tf_state_bucket}/${key}"]
      },
      {
        Sid    = "StateLock",
        Effect = "Allow",
        Action = [
          "dynamodb:DescribeTable",
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:DeleteItem"
        ],
        Resource = "arn:aws:dynamodb:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:table/${local.tf_lock_table}"
      },
      {
        Sid      = "DenyDataLakeObjectRead",
        Effect   = "Deny",
        Action   = "s3:GetObject*",
        Resource = "${aws_s3_bucket.data_lake.arn}/*"
      }
    ]
  })
}
