# infrastructure/iam_batch_task_role.tf

# This defines a dedicated IAM Role for the application code running inside the Batch container.
# This is separate from the Execution Role, which is used to pull the container image.

resource "aws_iam_role" "batch_task_role" {
  name = "${var.project_name}-batch-task-role"

  assume_role_policy = jsonencode({
    Version   = "2012-10-17",
    Statement = [
      {
        Action    = "sts:AssumeRole",
        Effect    = "Allow",
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Project = var.project_name
    Purpose = "Application Permissions"
  }
}

# --- Attach the necessary policies to the new Task Role ---

# 1. Attach the existing S3 access policy
resource "aws_iam_role_policy_attachment" "task_role_s3_access" {
  role       = aws_iam_role.batch_task_role.name
  policy_arn = aws_iam_policy.s3_access_policy.arn
}

# 2. Attach the existing CloudWatch metrics policy
resource "aws_iam_role_policy_attachment" "task_role_cloudwatch_metrics_access" {
  role       = aws_iam_role.batch_task_role.name
  # This references the policy we created earlier in iam_batch.tf
  policy_arn = aws_iam_policy.batch_job_cloudwatch_metrics_policy.arn
}
