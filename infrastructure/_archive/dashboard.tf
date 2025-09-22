resource "null_resource" "cloudwatch_dashboard_manager" {

  triggers = {
    script_content    = filebase64sha256("${path.module}/manage_dashboard.py")
    requirements_hash = filebase64sha256("${path.module}/requirements.txt") # Trigger rebuild if requirements change
    dashboard_name    = "${var.project_name}-dashboard-${var.environment}"
    state_machine_arn = aws_sfn_state_machine.genomics_pipeline_state_machine.id
    aws_region        = data.aws_region.current.name
  }

  provisioner "local-exec" {
    # This command is now idempotent and self-contained.
    # It ensures dependencies are installed before the script runs.
    command     = "python3 -m pip install -r ${path.module}/requirements.txt && python3 ${path.module}/manage_dashboard.py create"
    interpreter = ["bash", "-c"] # Use a proper shell to handle the '&&' operator reliably

    environment = {
      DASHBOARD_NAME = self.triggers.dashboard_name
      AWS_REGION     = self.triggers.aws_region
      SFN_ARN        = self.triggers.state_machine_arn
    }
  }

  provisioner "local-exec" {
    when        = destroy
    command     = "python3 -m pip install -r ${path.module}/requirements.txt && python3 ${path.module}/manage_dashboard.py destroy"
    interpreter = ["bash", "-c"]

    environment = {
      DASHBOARD_NAME = self.triggers.dashboard_name
      AWS_REGION     = self.triggers.aws_region
      SFN_ARN        = self.triggers.state_machine_arn
    }
  }
}
