# infrastructure/resilience.tf: per-stage sizing, run time limit and log retention (#104).
# The retry rules live with what they retry: the Batch job definition (main.tf) and the state machine
# (state_machine.asl.json).

locals {
  # Fargate's valid memory range (MiB) per vCPU count, for the stage_resources check.
  fargate_memory_mb = {
    "1" = [2048, 8192]
    "2" = [4096, 16384]
    "4" = [8192, 30720]
  }
}

# Batch job logs and the trigger Lambda's logs used to go to log groups AWS created on first use
# (/aws/batch/job, /aws/lambda/geyser-sfn-trigger), which keep everything forever and aren't in
# Terraform. These two are, with a retention period; the job definition and the Lambda point at them.
# The old groups keep their existing logs and are left alone (deleting them is a separate decision).
resource "aws_cloudwatch_log_group" "geyser_batch_jobs" {
  name              = "/aws/batch/${var.project_name}-${var.environment}"
  retention_in_days = var.log_retention_days
  tags              = { Name = "${var.project_name}-batch-jobs", Environment = var.environment, ManagedBy = "Terraform" }
}

resource "aws_cloudwatch_log_group" "geyser_sfn_trigger" {
  name              = "/aws/lambda/${var.project_name}-sfn-trigger-${var.environment}"
  retention_in_days = var.log_retention_days
  tags              = { Name = "${var.project_name}-sfn-trigger-logs", Environment = var.environment, ManagedBy = "Terraform" }
}
