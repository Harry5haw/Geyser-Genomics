# infrastructure/backend.tf
terraform {
  backend "s3" {
    bucket         = "geyser-tfstate-488543428961-eu-west-2"
    key            = "feature/rebrand-refactor/terraform.tfstate"
    region         = "eu-west-2"
    dynamodb_table = "geyser-tf-lock"
    encrypt        = true
  }
}
