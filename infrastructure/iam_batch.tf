# infrastructure/iam_batch.tf

# This file contains IAM policies specifically for the AWS Batch jobs.

# --- IAM Policy to allow Batch Jobs to publish CloudWatch Metrics ---
# This policy grants the least privilege required for the instrumented
# Python application to send custom metrics.
resource "aws_iam_policy" "batch_job_cloudwatch_metrics_policy" {
  name        = "${var.project_name}-batch-job-cloudwatch-metrics-policy"
  description = "Allows Batch jobs to put custom metrics into CloudWatch"

  policy = jsonencode({
    Version   = "2012-10-17",
    Statement = [
      {
        Sid      = "AllowCloudWatchPutMetricData",
        Effect   = "Allow",
        Action   = "cloudwatch:PutMetricData",
        Resource = "*" # This action does not support resource-level permissions
      }
    ]
  })
}

# --- Attach the new CloudWatch policy to the existing Batch Job Role ---
resource "aws_iam_role_policy_attachment" "batch_job_cloudwatch_metrics_attach" {
  # This references the existing role defined in main.tf
  role       = aws_iam_role.ecs_task_execution_role.name
  policy_arn = aws_iam_policy.batch_job_cloudwatch_metrics_policy.arn
}
