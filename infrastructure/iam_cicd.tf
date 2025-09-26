# infrastructure/iam_cicd.tf
# IAM roles for GitHub Actions OIDC (dev + prod side-by-side)

locals {
  gh_owner = "Harry5shaw"
  gh_repo  = "Geyser-Genomics"
}

# Existing GitHub OIDC provider
data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
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
      Effect = "Allow",
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn },
      Action   = "sts:AssumeRoleWithWebIdentity",
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
          "token.actions.githubusercontent.com:sub" = "repo:${local.gh_owner}/${local.gh_repo}:environment:dev"
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
        Effect   = "Allow",
        Action   = [
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
      Effect = "Allow",
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn },
      Action   = "sts:AssumeRoleWithWebIdentity",
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
          "token.actions.githubusercontent.com:sub" = "repo:${local.gh_owner}/${local.gh_repo}:environment:prod"
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
        Effect   = "Allow",
        Action   = [
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
      Effect = "Allow",
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn },
      Action   = "sts:AssumeRoleWithWebIdentity",
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
          "token.actions.githubusercontent.com:sub" = "repo:${local.gh_owner}/${local.gh_repo}:environment:dev"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "github_terraform_admin_attach_dev" {
  role       = aws_iam_role.geyser_github_terraform_role_dev.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

# PROD Terraform role
resource "aws_iam_role" "geyser_github_terraform_role_prod" {
  name = "geyser-github-terraform-role-prod"

  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect = "Allow",
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn },
      Action   = "sts:AssumeRoleWithWebIdentity",
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
          "token.actions.githubusercontent.com:sub" = "repo:${local.gh_owner}/${local.gh_repo}:environment:prod"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "github_terraform_admin_attach_prod" {
  role       = aws_iam_role.geyser_github_terraform_role_prod.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}
