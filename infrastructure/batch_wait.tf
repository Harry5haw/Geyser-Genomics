# infrastructure/batch_wait.tf
#
# How long Batch jobs wait before they start (#138): matters most on the EC2 GPU queue, where a job can
# wait minutes for an instance, but recorded for every queue of the project.
#
# Batch publishes a "Batch Job State Change" event for every status; the RUNNING one carries the job's
# createdAt and startedAt. An EventBridge rule (free for AWS service events) sends those events to a log
# group; the dashboard's Logs Insights widgets compute startedAt - createdAt (submit -> container running:
# scheduling, capacity, image pull) by stage and queue when viewed (scripts/monitoring.py). No Lambda and
# no custom metrics: a few hundred bytes of log per job instead of $0.30 a month per stage x queue metric.
# Everything here (events:*, logs:*) is inside the geyser-claude permissions boundary.

resource "aws_cloudwatch_log_group" "batch_job_events" {
  # EventBridge can only deliver to log groups under /aws/events/.
  name              = "/aws/events/${var.project_name}-batch-jobs-${var.environment}"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_event_rule" "batch_job_running" {
  name        = "${var.project_name}-batch-job-running-${var.environment}"
  description = "Batch jobs of this project reaching RUNNING (queue wait, #138)."
  event_pattern = jsonencode({
    source      = ["aws.batch"]
    detail-type = ["Batch Job State Change"]
    detail = {
      status = ["RUNNING"]
      # This project's queues only (geyser-job-queue, geyser-gpu-queue-dev, ...), not the legacy stack's.
      jobQueue = [{ prefix = "arn:aws:batch:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:job-queue/${var.project_name}-" }]
    }
  })
}

resource "aws_cloudwatch_event_target" "batch_job_running_to_logs" {
  rule = aws_cloudwatch_event_rule.batch_job_running.name
  arn  = aws_cloudwatch_log_group.batch_job_events.arn
}

# EventBridge writes to CloudWatch Logs only with a resource policy on the log group that allows it.
resource "aws_cloudwatch_log_resource_policy" "events_to_batch_job_events" {
  policy_name = "${var.project_name}-events-to-batch-job-events-${var.environment}"
  policy_document = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "EventBridgeToBatchJobEvents"
      Effect    = "Allow"
      Principal = { Service = ["events.amazonaws.com", "delivery.logs.amazonaws.com"] }
      Action    = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource  = "${aws_cloudwatch_log_group.batch_job_events.arn}:*"
      Condition = { ArnEquals = { "aws:SourceArn" = aws_cloudwatch_event_rule.batch_job_running.arn } }
    }]
  })
}
