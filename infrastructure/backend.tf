# infrastructure/backend.tf
# Partial backend configuration: the state key is chosen per environment at init time (#45), so dev
# and prod no longer share one state:
#
#   terraform init -backend-config=backend-dev.hcl    # state: env/dev/terraform.tfstate
#   terraform init -backend-config=backend-prod.hcl   # state: env/prod/terraform.tfstate (empty: prod is not deployed)
#
# A plain `terraform init` (no -backend-config) stops and asks for the key, on purpose.
# Until 2 Nov 2026 the single shared key was feature/rebrand-refactor/terraform.tfstate.
#
# Locking (#135): the S3 lockfile (<key>.tflock next to the state, Terraform >= 1.10) AND the
# DynamoDB table, both taken on every run. This is the transition: a run that hasn't re-initialised
# (or an older Terraform) still takes only the DynamoDB lock, which every new run takes too, so no two
# runs can hold the state at once. dynamodb_table goes once every worktree and CI has re-initialised
# (runbook in the #135 PR). Re-initialise after this change:
#   terraform init -reconfigure -backend-config=backend-dev.hcl
terraform {
  backend "s3" {
    bucket         = "geyser-tfstate-488543428961-eu-west-2"
    region         = "eu-west-2"
    use_lockfile   = true
    dynamodb_table = "geyser-tf-lock" # transition only (#135): removed in phase 3
    encrypt        = true
  }
}
