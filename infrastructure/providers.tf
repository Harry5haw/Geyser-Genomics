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
}
