# infrastructure/notifications.tf

# SNS Topic for pipeline status notifications
resource "aws_sns_topic" "geyser_pipeline_status_topic" {
  name = "${var.project_name}-PipelineStatus-${var.environment}"

  tags = {
    Name        = "${var.project_name}-PipelineStatus"
    Environment = var.environment
    ManagedBy   = "Terraform"
  }
}

# Failed runs (the state machine's Notify_Failure step) email the same address as the budget alerts
# (#121). The address comes from terraform.tfvars and is never committed. AWS first emails a
# confirmation link: until it is clicked the subscription stays PendingConfirmation and delivers
# nothing. Terraform can't delete an unconfirmed email subscription (it only drops it from state;
# AWS removes it after 3 days).
resource "aws_sns_topic_subscription" "pipeline_status_email" {
  topic_arn = aws_sns_topic.geyser_pipeline_status_topic.arn
  protocol  = "email"
  endpoint  = var.budget_alert_email
}
