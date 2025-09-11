# infrastructure/providers.tf

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7.0"
    }
  }

  # Add this backend block
  backend "s3" {
    bucket         = "teraflow-tfstate-488543428961-eu-west-2" # <-- Replace with your bucket name
    key            = "global/s3/terraform.tfstate"
    region         = "eu-west-2"
    dynamodb_table = "teraflow-tf-lock" # <-- Replace with your DynamoDB table name
    encrypt        = true
  }
}

provider "aws" {
  region = "eu-west-2"
}
