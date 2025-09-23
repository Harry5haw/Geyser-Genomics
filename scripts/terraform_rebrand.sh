#!/usr/bin/env bash
set -euo pipefail

echo "🔄 Replacing branded Terraform resource names with Geyser equivalents..."

# Only loop over *.tf files
for file in $(find infrastructure -type f -name "*.tf"); do
  echo "  -> Updating $file"

  # Resource renames
  sed -i 's/genomeflow_app_job_def/geyser_app_job_def/g' "$file"
  sed -i 's/genomeflow_queue/geyser_queue/g' "$file"
  sed -i 's/genomeflow_fargate/geyser_fargate/g' "$file"
  sed -i 's/sfn_log_group/geyser_sfn_log_group/g' "$file"
  sed -i 's/pipeline_status_topic/geyser_pipeline_status_topic/g' "$file"
  sed -i 's/sfn_trigger/geyser_sfn_trigger/g' "$file"
  sed -i 's/genomics_pipeline_state_machine/geyser_pipeline_state_machine/g' "$file"

  # IAM roles
  sed -i 's/aws_batch_service_role/geyser_batch_service_role/g' "$file"
  sed -i 's/batch_task_role/geyser_batch_task_role/g' "$file"
  sed -i 's/ecs_task_execution_role/geyser_batch_execution_role/g' "$file"
  sed -i 's/sfn_trigger_lambda_role/geyser_sfn_trigger_lambda_role/g' "$file"
  sed -i 's/step_functions_execution_role/geyser_sfn_execution_role/g' "$file"
  sed -i 's/github_ecr_role/geyser_github_ecr_role/g' "$file"
  sed -i 's/github_terraform_role/geyser_github_terraform_role/g' "$file"

  # IAM policies
  sed -i 's/s3_access_policy/geyser_s3_access_policy/g' "$file"
  sed -i 's/batch_job_cloudwatch_metrics_policy/geyser_batch_metrics_policy/g' "$file"
  sed -i 's/step_functions_execution_policy/geyser_sfn_execution_policy/g' "$file"
  sed -i 's/sfn_trigger_lambda_start_execution_policy/geyser_sfn_trigger_policy/g' "$file"
  sed -i 's/github_ecr_policy/geyser_github_ecr_policy/g' "$file"

done

echo "✅ Terraform .tf files updated with Geyser names."
