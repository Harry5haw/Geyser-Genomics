# infrastructure/dashboard.tf

resource "aws_cloudwatch_dashboard" "main_dashboard" {
  dashboard_name = "${var.project_name}-dashboard-${var.environment}"

  dashboard_body = jsonencode({
    widgets = [
      # WIDGET 1: Pipeline Task Duration Graph (Final Version)
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 16
        height = 8
        properties = {
          view    = "timeSeries"
          stacked = false
          region  = data.aws_region.current.name
          title   = "Pipeline Task Duration (Seconds)"
          period  = 300
          stat    = "Average"
          # This is the most explicit, "brute force" method.
          # It defines each line individually, which is the most reliable way
          # to render data when complex functions fail.
          # This query finds metrics with the specified dimension, ignoring others (like SampleId).
          metrics = [
            ["TerraFlowGenomicsV2", "Duration", "TaskName", "Decompress", "Status", "Success"],
            [".", ".", ".", "QualityControl", ".", "."],
            [".", ".", ".", "Align", ".", "."],
            [".", ".", ".", "CallVariants", ".", "."]
          ]
        }
      },
      # WIDGET 2: Total Pipeline Runs (Existing)
      {
        type   = "metric"
        x      = 16
        y      = 0
        width  = 8
        height = 4
        properties = {
          view    = "singleValue"
          region  = data.aws_region.current.name
          title   = "Total Pipeline Executions"
          metrics = [
            ["AWS/States", "ExecutionsStarted", "StateMachineArn", aws_sfn_state_machine.genomics_pipeline_state_machine.id]
          ]
          stat = "Sum"
        }
      },
      # WIDGET 3: Alarm Status (Existing)
      {
        type   = "alarm"
        x      = 16
        y      = 4
        width  = 8
        height = 4
        properties = {
          title = "Pipeline Alarms"
          alarms = [
            "arn:aws:cloudwatch:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:alarm:TerraFlow*",
            "arn:aws:cloudwatch:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:alarm:${aws_sfn_state_machine.genomics_pipeline_state_machine.name}*"
          ]
        }
      }
    ]
  })
}
