# infrastructure/notifications.tf

# SNS Topic for pipeline status notifications
resource "aws_sns_topic" "pipeline_status_topic" {
  name = "${var.project_name}-PipelineStatus-${var.environment}"

  tags = {
    Name        = "${var.project_name}-PipelineStatus"
    Environment = var.environment
    ManagedBy   = "Terraform"
  }
}

# 
