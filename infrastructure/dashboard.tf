# infrastructure/dashboard.tf (The Final, "Brute Force" Version)

resource "aws_cloudwatch_dashboard" "main_dashboard" {
  dashboard_name = "${var.project_name}-dashboard-${var.environment}"

  dashboard_body = jsonencode({
    widgets = [
      # WIDGET 1: Pipeline Task Duration Graph
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
          title   = "Pipeline Task Duration for Run: small_testV25"
          period  = 300
          stat    = "Average"
          # This is the final, "brute force" method. We are not using any
          # search or aggregation. We are hardcoding the exact four metric
          # streams for a single, known-good pipeline run.
          metrics = [
            [ "TerraFlowGenomics", "Duration", "SampleId", "small_testV25", "Status", "Success", "TaskName", "Decompress" ],
            [ ".", ".", ".", ".", ".", ".", ".", "QualityControl" ],
            [ ".", ".", ".", ".", ".", ".", ".", "Align" ],
            [ ".", ".", ".", ".", ".", ".", ".", "CallVariants" ]
          ]
        }
      },
      # WIDGET 2: Total Pipeline Runs
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
      }
    ]
  })
}
