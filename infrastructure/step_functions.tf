# infrastructure/step_functions.tf: the pipeline state machine (definition in state_machine.asl.json)

resource "aws_iam_role" "geyser_sfn_execution_role" {
  name = "${var.project_name}-sfn-execution-role-${var.environment}"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17",
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "states.amazonaws.com" } }]
  })
  tags = { Name = "${var.project_name}-sfn-execution-role", Environment = var.environment, ManagedBy = "Terraform" }
}

resource "aws_iam_policy" "geyser_sfn_execution_policy" {
  name        = "${var.project_name}-sfn-execution-policy-${var.environment}"
  description = "IAM policy for Step Functions to submit jobs to AWS Batch, log to CloudWatch, and manage events."
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Sid = "AWSBatchPermissions", Effect = "Allow", Action = ["batch:SubmitJob", "batch:DescribeJobs", "batch:TerminateJob"],
        Resource = [
          aws_batch_job_queue.geyser_queue.arn,
          aws_batch_job_queue.gpu_queue.arn,
          "arn:aws:batch:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:job-definition/${aws_batch_job_definition.geyser_deepvariant_job_def.name}",
          "arn:aws:batch:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:job-definition/${aws_batch_job_definition.geyser_deepvariant_job_def.name}:*",
          "arn:aws:batch:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:job-definition/${aws_batch_job_definition.geyser_app_job_def.name}",
          "arn:aws:batch:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:job-definition/${aws_batch_job_definition.geyser_app_job_def.name}:*"
        ]
      },
      {
        Sid = "CloudWatchLogsPermissions", Effect = "Allow", Action = ["logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery", "logs:DeleteLogDelivery", "logs:ListLogDeliveries", "logs:PutResourcePolicy", "logs:DescribeResourcePolicies", "logs:DescribeLogGroups"], Resource = "*"
      },
      { Sid = "SNSPublishPermissions", Effect = "Allow", Action = "sns:Publish", Resource = aws_sns_topic.geyser_pipeline_status_topic.arn },
      # Read_Regions reads the planner's output (tasks.py regions --output-key) straight from S3.
      { Sid = "ReadVariantRegions", Effect = "Allow", Action = "s3:GetObject", Resource = "${aws_s3_bucket.data_lake.arn}/intermediate/variant-regions/*" },
      # Check_Reference checks the run's reference/<name> exists before any job (HeadObject needs
      # s3:GetObject). s3:ListBucket makes S3 answer a missing reference with 404 (NoSuchKey) instead of
      # 403, so the run fails with "not found" rather than an access error. As for the trigger Lambda,
      # it can't be narrowed by prefix: HeadObject carries no s3:prefix to match.
      { Sid = "CheckReference", Effect = "Allow", Action = "s3:GetObject", Resource = "${aws_s3_bucket.data_lake.arn}/reference/*" },
      { Sid = "MissingReferenceIs404", Effect = "Allow", Action = "s3:ListBucket", Resource = aws_s3_bucket.data_lake.arn },
      # Close_Run_Record writes a failed run's closing run-record part (#155), and only that object.
      { Sid = "CloseRunRecord", Effect = "Allow", Action = "s3:PutObject", Resource = "${aws_s3_bucket.data_lake.arn}/runs/*.provenance/Finalize.json" },
      # Read_Gate_Verdict reads the quality gate's verdict (tasks.py gate) to decide warn or fail.
      { Sid = "ReadGateVerdicts", Effect = "Allow", Action = "s3:GetObject", Resource = "${aws_s3_bucket.data_lake.arn}/qc_reports/*.gate.json" },
      { Sid = "EventsPermissions", Effect = "Allow", Action = ["events:PutRule", "events:DeleteRule", "events:PutTargets", "events:RemoveTargets"], Resource = "*" }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "step_functions_policy_attach" {
  role       = aws_iam_role.geyser_sfn_execution_role.name
  policy_arn = aws_iam_policy.geyser_sfn_execution_policy.arn
}

resource "aws_cloudwatch_log_group" "geyser_sfn_log_group" {
  name              = "/aws/vendedlogs/states/${var.project_name}-sfn-${var.environment}"
  retention_in_days = 7
  tags              = { Name = "${var.project_name}-sfn-log-group", Environment = var.environment, ManagedBy = "Terraform" }
}

resource "aws_sfn_state_machine" "geyser_pipeline_state_machine" {
  name     = "${var.project_name}-pipeline-sfn-${var.environment}"
  role_arn = aws_iam_role.geyser_sfn_execution_role.arn
  # The definition lives in state_machine.asl.json so that tests/test_state_machine_local.py can run
  # the very same file in Step Functions Local (Docker, no AWS). Only these values are filled in.
  definition = templatefile("${path.module}/state_machine.asl.json", {
    job_definition             = aws_batch_job_definition.geyser_app_job_def.name
    job_queue                  = aws_batch_job_queue.geyser_queue.name
    deepvariant_job_definition = aws_batch_job_definition.geyser_deepvariant_job_def.name
    gpu_job_queue              = aws_batch_job_queue.gpu_queue.name
    sns_topic_arn              = aws_sns_topic.geyser_pipeline_status_topic.arn
    pipeline_timeout_seconds   = var.pipeline_timeout_hours * 3600
    qc_vcpu                    = var.stage_resources["qc"].vcpu
    qc_memory_mb               = var.stage_resources["qc"].memory_mb
    align_vcpu                 = var.stage_resources["align"].vcpu
    align_memory_mb            = var.stage_resources["align"].memory_mb
    variants_vcpu              = var.stage_resources["variants"].vcpu
    variants_memory_mb         = var.stage_resources["variants"].memory_mb
    plan_vcpu                  = var.stage_resources["plan"].vcpu
    plan_memory_mb             = var.stage_resources["plan"].memory_mb
    merge_vcpu                 = var.stage_resources["merge"].vcpu
    merge_memory_mb            = var.stage_resources["merge"].memory_mb
    bucket                     = aws_s3_bucket.data_lake.bucket
    variant_region_window      = var.variant_region_window
    variants_max_concurrency   = var.variants_max_concurrency
    gate_mode                  = var.gate_mode
  })

  lifecycle {
    precondition {
      # QC and the region planner (after QC, in the same branch) each run alongside align, never together.
      condition     = max(var.stage_resources["qc"].vcpu, var.stage_resources["plan"].vcpu) + var.stage_resources["align"].vcpu <= var.fargate_max_vcpus
      error_message = "QC (then the region planner) and align run at the same time: their vCPUs together must fit in fargate_max_vcpus."
    }
    precondition {
      condition     = var.variants_max_concurrency * var.stage_resources["variants"].vcpu <= var.fargate_max_vcpus
      error_message = "variants_max_concurrency shards of the variants size must fit in fargate_max_vcpus (more would only queue)."
    }
  }
  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.geyser_sfn_log_group.arn}:*"
    include_execution_data = true
    level                  = "ERROR" # errors only, to keep log volume (and cost) low
  }
  tags       = { Name = "${var.project_name}-pipeline-sfn", Environment = var.environment, ManagedBy = "Terraform" }
  depends_on = [aws_iam_role_policy_attachment.step_functions_policy_attach, ]
}
