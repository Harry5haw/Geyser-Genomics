# infrastructure/iam_cicd.tf

# -----------------------------
# GitHub repo identity (adjust if the repo moves)
# -----------------------------
locals {
  gh_owner = "Harry5haw"
  gh_repo  = "Geyser-Genomics"
}

# -----------------------------
# Use existing GitHub OIDC provider (do NOT create a new one)
# -----------------------------
data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

# -----------------------------------------------------------------------------
# IAM Role and Policy for the Application (ECR Push) CI/CD Workflow
# -----------------------------------------------------------------------------
resource "aws_iam_role" "geyser_github_ecr_role" {
  name = "${var.project_name}-github-ecr-role-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect = "Allow",
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn },
      Action   = "sts:AssumeRoleWithWebIdentity",
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        },
        # Allow GH Environment-based runs (dev/prod) and main branch refs
        StringLike = {
          "token.actions.githubusercontent.com:sub" = [
            "repo:${local.gh_owner}/${local.gh_repo}:environment:dev",
            "repo:${local.gh_owner}/${local.gh_repo}:environment:prod",
            "repo:${local.gh_owner}/${local.gh_repo}:ref:refs/heads/main"
          ]
        }
      }
    }]
  })

  tags = {
    Name = "${var.project_name}-github-ecr-role"
  }
}

resource "aws_iam_policy" "geyser_github_ecr_policy" {
  name = "${var.project_name}-github-ecr-policy-${var.environment}"

  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Sid      = "ECRLogin",
        Effect   = "Allow",
        Action   = "ecr:GetAuthorizationToken",
        Resource = "*"
      },
      {
        Sid    = "ECRImagePush",
        Effect = "Allow",
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:CompleteLayerUpload",
          "ecr:InitiateLayerUpload",
          "ecr:PutImage",
          "ecr:UploadLayerPart"
        ],
        Resource = aws_ecr_repository.geyser_app.arn
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "github_ecr_attach" {
  role       = aws_iam_role.geyser_github_ecr_role.name
  policy_arn = aws_iam_policy.geyser_github_ecr_policy.arn
}

# -----------------------------------------------------------------------------
# IAM Role for the Infrastructure (Terraform) CI/CD Workflow
# -----------------------------------------------------------------------------
resource "aws_iam_role" "geyser_github_terraform_role" {
  name = "${var.project_name}-github-terraform-role-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect = "Allow",
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn },
      Action   = "sts:AssumeRoleWithWebIdentity",
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        },
        # Allow Terraform runs from envs + main
        StringLike = {
          "token.actions.githubusercontent.com:sub" = [
            "repo:${local.gh_owner}/${local.gh_repo}:environment:dev",
            "repo:${local.gh_owner}/${local.gh_repo}:environment:prod",
            "repo:${local.gh_owner}/${local.gh_repo}:ref:refs/heads/main"
          ]
        }
      }
    }]
  })

  tags = {
    Name = "${var.project_name}-github-terraform-role"
  }
}

resource "aws_iam_role_policy_attachment" "github_terraform_admin_attach" {
  role       = aws_iam_role.geyser_github_terraform_role.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}
