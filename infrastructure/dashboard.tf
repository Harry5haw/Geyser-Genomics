resource "aws_cloudwatch_dashboard" "main_dashboard" {
  dashboard_name = "${var.project_name}-dashboard-${var.environment}"

  # Use the templatefile function to render the proven JSON body.
  # This avoids all the complexities and potential errors of using jsonencode for this structure.
  dashboard_body = templatefile("${path.module}/dashboard_body.json", {
    region            = data.aws_region.current.name
    state_machine_arn = aws_sfn_state_machine.genomics_pipeline_state_machine.id
  })
}
