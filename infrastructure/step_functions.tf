# infrastructure/step_functions.tf

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

resource "aws_iam_role" "step_functions_execution_role" {
  name = "${var.project_name}-sfn-execution-role-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "states.amazonaws.com" } }]
  })
  tags = { Name = "${var.project_name}-sfn-execution-role", Environment = var.environment, ManagedBy = "Terraform" }
}

resource "aws_iam_policy" "step_functions_execution_policy" {
  name        = "${var.project_name}-sfn-execution-policy-${var.environment}"
  description = "IAM policy for Step Functions to submit jobs to AWS Batch, log to CloudWatch, and manage events."
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Sid    = "AWSBatchPermissions",
        Effect = "Allow",
        Action = ["batch:SubmitJob", "batch:DescribeJobs", "batch:TerminateJob"],
        Resource = [
          aws_batch_job_queue.genomeflow_queue.arn,
          "arn:aws:batch:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:job-definition/${aws_batch_job_definition.genomeflow_app_job_def.name}",
          "arn:aws:batch:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:job-definition/${aws_batch_job_definition.genomeflow_app_job_def.name}:*"
        ]
      },
      {
        Sid    = "CloudWatchLogsPermissions",
        Effect = "Allow",
        Action = ["logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery", "logs:DeleteLogDelivery", "logs:ListLogDeliveries", "logs:PutResourcePolicy", "logs:DescribeResourcePolicies", "logs:DescribeLogGroups"],
        Resource = "*"
      },
      { Sid = "SNSPublishPermissions", Effect = "Allow", Action = "sns:Publish", Resource = aws_sns_topic.pipeline_status_topic.arn },
      { Sid = "EventsPermissions", Effect = "Allow", Action = ["events:PutRule", "events:DeleteRule", "events:PutTargets", "events:RemoveTargets"], Resource = "*" }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "step_functions_policy_attach" {
  role       = aws_iam_role.step_functions_execution_role.name
  policy_arn = aws_iam_policy.step_functions_execution_policy.arn
}

resource "aws_cloudwatch_log_group" "sfn_log_group" {
  name              = "/aws/vendedlogs/states/${var.project_name}-sfn-${var.environment}"
  retention_in_days = 30
  tags              = { Name = "${var.project_name}-sfn-log-group", Environment = var.environment, ManagedBy = "Terraform" }
}

resource "aws_sfn_state_machine" "genomics_pipeline_state_machine" {
  name     = "${var.project_name}-pipeline-sfn-${var.environment}"
  role_arn = aws_iam_role.step_functions_execution_role.arn
  definition = jsonencode({
    Comment = "TerraFlow Genomics Pipeline orchestrated by AWS Step Functions"
    StartAt = "Prepare_Decompress_Command"
    States = {
      Prepare_Decompress_Command = {
        Type = "Pass",
        Parameters = { "JobName.$" = "States.Format('DecompressSRA-{}-{}', $.srr_id, $$.Execution.Name)", "ContainerOverrides" = { "Command.$" = "States.Array('python', 'tasks.py', 'decompress', $.srr_id)" } },
        ResultPath = "$.batch_params", Next = "Decompress_SRA"
      },
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

  # CORRECTED: This block is now in the correct multi-line format.
  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.sfn_log_group.arn}:*"
    include_execution_data = true
    level                  = "ALL"
  }

  tags = {
    Name        = "${var.project_name}-pipeline-sfn"
    Environment = var.environment
    ManagedBy   = "Terraform"
  }

  depends_on = [
    aws_iam_role_policy_attachment.step_functions_policy_attach,
  ]
}
