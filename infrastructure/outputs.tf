# infrastructure/outputs.tf

output "data_lake_bucket_name" {
  description = "The name of the S3 bucket used as the data lake."
  value       = aws_s3_bucket.data_lake.bucket
}

output "ecr_repository_url" {
  description = "The URL of the ECR repository for the geyser-app Docker image."
  value       = aws_ecr_repository.geyser_app.repository_url
}

output "job_queue_arn" {
  description = "The ARN of the AWS Batch Job Queue."
  value       = aws_batch_job_queue.geyser_queue.arn
}

output "geyser_app_job_def_arn" {
  description = "The ARN of the AWS Batch Job Definition for the geyser-app."
  value       = aws_batch_job_definition.geyser_app_job_def.arn
}

output "geyser_pipeline_state_machine_arn" {
  description = "The ARN of the AWS Step Functions state machine orchestrating the genomics pipeline."
  value       = aws_sfn_state_machine.geyser_pipeline_state_machine.id
}

output "pipeline_status_sns_topic_arn" {
  description = "The ARN of the SNS topic for pipeline status notifications (e.g., failures)."
  value       = aws_sns_topic.geyser_pipeline_status_topic.arn
}

# --- CI/CD IAM Role Outputs ---

# Dev outputs
output "geyser_github_ecr_role_dev_arn" {
  description = "The ARN of the IAM role for the ECR push CI/CD workflow (dev)."
  value       = aws_iam_role.geyser_github_ecr_role_dev.arn
}

output "geyser_github_terraform_role_dev_arn" {
  description = "The ARN of the IAM role for the Terraform CI/CD workflow (dev)."
  value       = aws_iam_role.geyser_github_terraform_role_dev.arn
}

# Prod outputs
output "geyser_github_ecr_role_prod_arn" {
  description = "The ARN of the IAM role for the ECR push CI/CD workflow (prod)."
  value       = aws_iam_role.geyser_github_ecr_role_prod.arn
}

output "geyser_github_terraform_role_prod_arn" {
  description = "The ARN of the IAM role for the Terraform CI/CD workflow (prod)."
  value       = aws_iam_role.geyser_github_terraform_role_prod.arn
}
