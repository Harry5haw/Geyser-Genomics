terraform {
  required_version = ">= 1.13.3, < 2.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 5.100.0"
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
