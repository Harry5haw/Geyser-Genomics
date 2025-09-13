# infrastructure/step_functions.tf
# (Full file content as before, but with this one change)

# ... (all IAM and log group resources are unchanged) ...

resource "aws_sfn_state_machine" "genomics_pipeline_state_machine" {
  name     = "${var.project_name}-pipeline-sfn-${var.environment}"
  role_arn = aws_iam_role.step_functions_execution_role.arn
  definition = jsonencode({
    Comment = "TerraFlow Genomics Pipeline orchestrated by AWS Step Functions"
    StartAt = "Prepare_Decompress_Command"
    States = {
      # MODIFIED: This state is now configured to run our CANARY diagnostic script.
      Prepare_Decompress_Command = {
        Type = "Pass",
        Parameters = { "JobName.$" = "States.Format('CanaryTest-{}-{}', $.srr_id, $$.Execution.Name)", "ContainerOverrides" = { "Command" = ["python", "debug_canary.py"] } },
        ResultPath = "$.batch_params", Next = "Decompress_SRA"
      },
      # ... (rest of the state machine is unchanged) ...
      Decompress_SRA = {
        Type     = "Task", Resource = "arn:aws:states:::batch:submitJob.sync",
        Parameters = { "JobName.$" = "$.batch_params.JobName", "JobDefinition" = aws_batch_job_definition.genomeflow_app_job_def.name, "JobQueue" = aws_batch_job_queue.genomeflow_queue.name, "ContainerOverrides.$" = "$.batch_params.ContainerOverrides", "Timeout" = { "AttemptDurationSeconds" = 3600 } },
        ResultPath = "$.batch_output", Catch = [{ ErrorEquals = ["States.ALL"], Next = "Notify_Failure", ResultPath = "$.error" }], Next = "Prepare_QC_Command"
      },
      Prepare_QC_Command = {
        Type = "Pass",
        Parameters = { "JobName.$" = "States.Format('QualityControl-{}-{}', $.srr_id, $$.Execution.Name)", "ContainerOverrides" = { "Command.$" = "States.Array('python', 'tasks.py', 'qc', $.srr_id)" } },
        ResultPath = "$.batch_params", Next = "Quality_Control"
      },
      Quality_Control = {
        Type     = "Task", Resource = "arn:aws:states:::batch:submitJob.sync",
        Parameters = { "JobName.$" = "$.batch_params.JobName", "JobDefinition" = aws_batch_job_definition.genomeflow_app_job_def.name, "JobQueue" = aws_batch_job_queue.genomeflow_queue.name, "ContainerOverrides.$" = "$.batch_params.ContainerOverrides", "Timeout" = { "AttemptDurationSeconds" = 1800 } },
        ResultPath = "$.batch_output", Catch = [{ ErrorEquals = ["States.ALL"], Next = "Notify_Failure", ResultPath = "$.error" }], Next = "Prepare_Align_Command"
      },
      Prepare_Align_Command = {
        Type = "Pass",
        Parameters = { "JobName.$" = "States.Format('AlignGenome-{}-{}', $.srr_id, $$.Execution.Name)", "ContainerOverrides" = { "Command.$" = "States.Array('python', 'tasks.py', 'align', $.srr_id, $.reference_name)" } },
        ResultPath = "$.batch_params", Next = "Align_Genome"
      },
      Align_Genome = {
        Type     = "Task", Resource = "arn:aws:states:::batch:submitJob.sync",
        Parameters = { "JobName.$" = "$.batch_params.JobName", "JobDefinition" = aws_batch_job_definition.genomeflow_app_job_def.name, "JobQueue" = aws_batch_job_queue.genomeflow_queue.name, "ContainerOverrides.$" = "$.batch_params.ContainerOverrides", "Timeout" = { "AttemptDurationSeconds" = 14400 } },
        ResultPath = "$.batch_output", Catch = [{ ErrorEquals = ["States.ALL"], Next = "Notify_Failure", ResultPath = "$.error" }], Next = "Prepare_Variants_Command"
      },
      Prepare_Variants_Command = {
        Type = "Pass",
        Parameters = { "JobName.$" = "States.Format('CallVariants-{}-{}', $.srr_id, $$.Execution.Name)", "ContainerOverrides" = { "Command.$" = "States.Array('python', 'tasks.py', 'variants', $.srr_id, $.reference_name)" } },
        ResultPath = "$.batch_params", Next = "Call_Variants"
      },
      Call_Variants = {
        Type     = "Task", Resource = "arn:aws:states:::batch:submitJob.sync",
        Parameters = { "JobName.$" = "$.batch_params.JobName", "JobDefinition" = aws_batch_job_definition.genomeflow_app_job_def.name, "JobQueue" = aws_batch_job_queue.genomeflow_queue.name, "ContainerOverrides.$" = "$.batch_params.ContainerOverrides", "Timeout" = { "AttemptDurationSeconds" = 7200 } },
        ResultPath = "$.batch_output", Catch = [{ ErrorEquals = ["States.ALL"], Next = "Notify_Failure", ResultPath = "$.error" }], End = true
      },
      Notify_Failure = {
        Type     = "Task", Resource = "arn:aws:states:::sns:publish",
        Parameters = { "TopicArn" = aws_sns_topic.pipeline_status_topic.arn, "Message" = { "PipelineName" = "TerraFlow Genomics Pipeline", "ExecutionId" = "$$.Execution.Id", "Status" = "FAILED", "ErrorDetails.$" = "$.error", "Input.$" = "$$", "StartTime" = "$$.Execution.StartTime" }, "MessageAttributes" = { "Status" = { "DataType" = "String", "StringValue" = "FAILED" }, "Pipeline" = { "DataType" = "String", "StringValue" = "TerraFlow Genomics" } } },
        ResultPath = null, End = true
      }
    }
  })
  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.sfn_log_group.arn}:*"
    include_execution_data = true
    level                  = "ALL"
  }
  tags = { Name = "${var.project_name}-pipeline-sfn", Environment = var.environment, ManagedBy = "Terraform" }
  depends_on = [ aws_iam_role_policy_attachment.step_functions_policy_attach, ]
}
