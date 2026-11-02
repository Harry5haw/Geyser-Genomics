# infrastructure/account/main.tf
#
# Account-level console extras (#64), in their own small stack and state:
#   terraform -chdir=infrastructure/account init -backend-config=backend-account.hcl
#   AWS_PROFILE=default terraform -chdir=infrastructure/account plan -var-file=../terraform.tfvars -out=account.tfplan
#
# Why separate from infrastructure/: Cost Explorer, IAM Access Analyzer and AppRegistry are outside the
# geyser-claude permissions boundary, which can't even read them. In the dev state, every geyser-claude
# plan would fail refreshing these resources. Here only the user plans and applies; geyser-claude never
# touches this state.

terraform {
  required_version = ">= 1.13.3, < 2.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.69.0" # the same pin as infrastructure/providers.tf
    }
  }

  # Same state bucket and lock table as the main stack, its own key (backend-account.hcl).
  backend "s3" {
    bucket         = "geyser-tfstate-488543428961-eu-west-2"
    region         = "eu-west-2"
    dynamodb_table = "geyser-tf-lock"
    encrypt        = true
  }
}

provider "aws" {
  region = "eu-west-2"

  default_tags {
    tags = { Project = var.project_name, Environment = var.environment, ManagedBy = "Terraform" }
  }
}

data "aws_caller_identity" "current" {}
