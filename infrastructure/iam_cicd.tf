# infrastructure/iam_cicd.tf

# -----------------------------------------------------------------------------
# GitHub OIDC Provider for AWS
# This resource tells your AWS account to trust GitHub Actions.
# -----------------------------------------------------------------------------
resource "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"

  client_id_list = [
    "sts.amazonaws.com",
  ]

  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"] # Standard thumbprint for GitHub OIDC
}

# -----------------------------------------------------------------------------
# IAM Role and Policy for the Application (ECR Push) CI/CD Workflow
# -----------------------------------------------------------------------------
resource "aws_iam_role" "github_ecr_role" {
  name = "${var.project_name}-github-ecr-role-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Effect = "Allow",
        Principal = {
          Federated = aws_iam_openid_connect_provider.github.arn
        },
        Action = "sts:AssumeRoleWithWebIdentity",
        Condition = {
          StringEquals = {
            "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          },
          StringLike = {
            # Replace with your GitHub username/repo
            "token.actions.githubusercontent.com:sub" = "repo:Harry5haw/genomeflow-cloud-platform:ref:refs/heads/main"
          }
        }
      }
    ]
  })

  tags = {
    Name = "${var.project_name}-github-ecr-role"
  }
}

resource "aws_iam_policy" "github_ecr_policy" {
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
        Resource = aws_ecr_repository.genomeflow_app.arn
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "github_ecr_attach" {
  role       = aws_iam_role.github_ecr_role.name
  policy_arn = aws_iam_policy.github_ecr_policy.arn
}

# -----------------------------------------------------------------------------
# IAM Role for the Infrastructure (Terraform) CI/CD Workflow
# -----------------------------------------------------------------------------
resource "aws_iam_role" "github_terraform_role" {
  name = "${var.project_name}-github-terraform-role-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Effect = "Allow",
        Principal = {
          Federated = aws_iam_openid_connect_provider.github.arn
        },
        Action = "sts:AssumeRoleWithWebIdentity",
        Condition = {
          StringEquals = {
            "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          },
          StringLike = {
            # Replace with your GitHub username/repo
            "token.actions.githubusercontent.com:sub" = "repo:Harry5haw/genomeflow-cloud-platform:*" # Use wildcard for PR branches
          }
        }
      }
    ]
  })

  tags = {
    Name = "${var.project_name}-github-terraform-role"
  }
}

resource "aws_iam_role_policy_attachment" "github_terraform_admin_attach" {
  role       = aws_iam_role.github_terraform_role.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess" # Using the managed policy for simplicity
}
