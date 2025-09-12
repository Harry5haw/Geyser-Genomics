# infrastructure/outputs.tf

output "data_lake_bucket_name" {
  description = "The name of the S3 bucket used as the data lake."
  value       = aws_s3_bucket.data_lake.bucket
}

output "ecr_repository_url" {
  description = "The URL of the ECR repository for the genomeflow-app Docker image."
  value       = aws_ecr_repository.genomeflow_app.repository_url
}

output "job_queue_arn" {
  description = "The ARN of the AWS Batch Job Queue."
  value       = aws_batch_job_queue.genomeflow_queue.arn
}

output "genomeflow_app_job_def_arn" {
  description = "The ARN of the AWS Batch Job Definition for the genomeflow-app."
  value       = aws_batch_job_definition.genomeflow_app_job_def.arn
}

output "genomics_pipeline_state_machine_arn" {
  description = "The ARN of the AWS Step Functions state machine orchestrating the genomics pipeline."
  value       = aws_sfn_state_machine.genomics_pipeline_state_machine.id
}

output "pipeline_status_sns_topic_arn" {
  description = "The ARN of the SNS topic for pipeline status notifications (e.g., failures)."
  value       = aws_sns_topic.pipeline_status_topic.arn
}

output "github_ecr_role_arn" {
  description = "The ARN of the IAM role for the ECR push CI/CD workflow."
  value       = aws_iam_role.github_ecr_role.arn
}

output "github_terraform_role_arn" {
  description = "The ARN of the IAM role for the Terraform CI/CD workflow."
  value       = aws_iam_role.github_terraform_role.arn
}