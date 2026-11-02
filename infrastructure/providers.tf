terraform {
  required_version = ">= 1.13.3, < 2.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.69.0" # 6.x: CUSTOM budget periods (#76); upgrade notes in docs/design/claude-guardrails.md
    }
    random = {
      source  = "hashicorp/random"
      version = "= 3.7.2"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "= 2.7.1"
    }
  }
}

provider "aws" {
  region = "eu-west-2"

  # Every resource gets these (#64), so Cost Explorer, Resource Groups and myApplications can group the
  # project. Project and Environment become cost allocation tags once the user activates them in
  # Billing. var.aws_application_tag adds the myApplications tag after the AppRegistry application
  # exists (console_extras.tf: two steps, a provider can't read a resource it is creating).
  default_tags {
    tags = merge(
      { Project = var.project_name, Environment = var.environment, ManagedBy = "Terraform" },
      var.aws_application_tag,
    )
  }
}
