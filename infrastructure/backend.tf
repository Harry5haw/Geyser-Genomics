# infrastructure/backend.tf
# Partial backend configuration: the state key is chosen per environment at init time (#45), so dev
# and prod no longer share one state:
#
#   terraform init -backend-config=backend-dev.hcl    # state: env/dev/terraform.tfstate
#   terraform init -backend-config=backend-prod.hcl   # state: env/prod/terraform.tfstate (empty: prod is not deployed)
#
# A plain `terraform init` (no -backend-config) stops and asks for the key, on purpose.
# Until 2 Nov 2026 the single shared key was feature/rebrand-refactor/terraform.tfstate.
terraform {
  backend "s3" {
    bucket         = "geyser-tfstate-488543428961-eu-west-2"
    region         = "eu-west-2"
    dynamodb_table = "geyser-tf-lock"
    encrypt        = true
  }
}
