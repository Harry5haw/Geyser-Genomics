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
          "arn:aws:batch:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:job-definition/${aws_batch_job_definition.geyser_app_job_def.name}:*",
          "arn:aws:batch:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:job-definition/${aws_batch_job_definition.geyser_giraffe_job_def.name}",
          "arn:aws:batch:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:job-definition/${aws_batch_job_definition.geyser_giraffe_job_def.name}:*",
          # W3: the Publish job (publish.tf), on the same Fargate queue.
          "arn:aws:batch:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:job-definition/${aws_batch_job_definition.geyser_publish_job_def.name}",
          "arn:aws:batch:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:job-definition/${aws_batch_job_definition.geyser_publish_job_def.name}:*",
          # v3.0: the optional AlphaGenome annotation (alphagenome.tf), on the same Fargate queue.
          "arn:aws:batch:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:job-definition/${aws_batch_job_definition.geyser_annotate_job_def.name}}",
          "arn:aws:batch:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:job-definition/${aws_batch_job_definition.geyser_annotate_job_def.name}:*",
          # v1.3: the large route's jobs (genome_scale.tf), on the same Fargate queue.
          "arn:aws:batch:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:job-definition/${aws_batch_job_definition.geyser_app_large_job_def.name}",
          "arn:aws:batch:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:job-definition/${aws_batch_job_definition.geyser_app_large_job_def.name}:*",
          # v1.5: the DeepVariant split's make_examples part jobs (deepvariant_cpu.tf), on the same Fargate queue.
          "arn:aws:batch:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:job-definition/${aws_batch_job_definition.geyser_deepvariant_cpu_job_def.name}",
          "arn:aws:batch:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:job-definition/${aws_batch_job_definition.geyser_deepvariant_cpu_job_def.name}:*"
        ]
      },
      {
        # SubmitJob with Tags (the Stage tag per job, #64) also needs TagResource on the new job.
        Sid      = "AWSBatchTagJobs", Effect = "Allow", Action = "batch:TagResource",
        Resource = "arn:aws:batch:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:job/*"
      },
      {
        Sid = "CloudWatchLogsPermissions", Effect = "Allow", Action = ["logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery", "logs:DeleteLogDelivery", "logs:ListLogDeliveries", "logs:PutResourcePolicy", "logs:DescribeResourcePolicies", "logs:DescribeLogGroups"], Resource = "*"
      },
      { Sid = "SNSPublishPermissions", Effect = "Allow", Action = "sns:Publish", Resource = aws_sns_topic.geyser_pipeline_status_topic.arn },
      # Read_Regions reads the planner's output (tasks.py regions --output-key) straight from S3.
      { Sid = "ReadVariantRegions", Effect = "Allow", Action = "s3:GetObject", Resource = "${aws_s3_bucket.data_lake.arn}/intermediate/variant-regions/*" },
      # v3.0: the annotation's plan (Plan_Annotate writes it, Read_Annotate_Plan reads it).
      { Sid = "ReadAnnotatePlan", Effect = "Allow", Action = "s3:GetObject", Resource = "${aws_s3_bucket.data_lake.arn}/intermediate/annotate-plan/*" },
      # Read_Manifest reads the pre-flight's chunk manifest (tasks.py preflight) on the large route, and only that.
      { Sid = "ReadChunkManifests", Effect = "Allow", Action = "s3:GetObject", Resource = "${aws_s3_bucket.data_lake.arn}/intermediate/chunks/*/manifest.json" },
      # Check_Reference checks the run's reference/<name> exists before any job (HeadObject needs
      # s3:GetObject). s3:ListBucket makes S3 answer a missing reference with 404 (NoSuchKey) instead of
      # 403, so the run fails with "not found" rather than an access error. As for the trigger Lambda,
      # it can't be narrowed by prefix: HeadObject carries no s3:prefix to match.
      { Sid = "CheckReference", Effect = "Allow", Action = "s3:GetObject", Resource = "${aws_s3_bucket.data_lake.arn}/reference/*" },
      # v2.0: Check_Alignment (from_alignment runs) checks the sample's CRAM the same way.
      { Sid = "CheckAlignment", Effect = "Allow", Action = "s3:GetObject", Resource = "${aws_s3_bucket.data_lake.arn}/alignments/*.cram" },
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

locals {
  # Per-run overrides (the input's "overrides" object, checked in Run_Setting_Rules and after): the
  # vCPU/MiB pairs align may ask for. Fargate's valid memory per vCPU (resilience.tf: 0.25 vCPU takes
  # 512, 1024 or 2048 MiB; the others any 1 GiB step in range), and only sizes where QC or the planner
  # plus align still fit in fargate_max_vcpus, the same rule as the precondition below. "<vcpu>/<MiB>"
  # strings, as the state machine formats them.
  fargate_memory_options = {
    for vcpu, range_mb in local.fargate_memory_mb :
    vcpu => vcpu == "0.25" ? [512, 1024, 2048] : range(range_mb[0], range_mb[1] + 1, 1024)
  }
  align_override_sizes = flatten([
    for vcpu, options in local.fargate_memory_options : [for mb in options : "${vcpu}/${mb}"]
    if tonumber(vcpu) + max(var.stage_resources["qc"].vcpu, var.stage_resources["plan"].vcpu) <= var.fargate_max_vcpus
  ])
  # An override window's bounds. 5 Mb: ~620 shards on a whole genome, well inside a Standard execution's
  # 25,000-event history (a shard is ~10 events). 250 Mb: one region per GRCh38 chromosome (chr1 is 248.9 Mb).
  variant_region_window_min = 5000000
  variant_region_window_max = 250000000
  # v1.3 the large route: the per-run parallelism's maxima, from the Fargate Spot cap (so a large run stays on
  # Spot prices rather than spilling onto on-demand). QC runs alongside the chunks.
  large                   = var.large_route_resources
  align_concurrency_max   = floor((var.fargate_spot_max_vcpus - local.large["qc"].vcpu) / local.large["chunk"].vcpu)
  calling_concurrency_max = floor(var.fargate_spot_max_vcpus / var.stage_resources["variants"].vcpu)
  merge_threads_max       = local.large["merge"].vcpu
  large_input_bytes       = floor(var.large_input_gb * 1000000000)
  # v1.5 the DeepVariant split: parts at once on Fargate Spot, and DEEPVARIANT_SHARDS per parts (index = parts):
  # Step Functions has no multiply, so the state machine reads parts x the part's vCPUs from this list.
  dv_parts_max       = floor(var.fargate_spot_max_vcpus / var.dv_part_resources.vcpu)
  dv_shards_by_parts = [for parts in range(local.dv_parts_max + 1) : parts * var.dv_part_resources.vcpu]
}

locals {
  # One name for the state machine, so the web API (web_api.tf) can build its ARN without depending on it.
  pipeline_state_machine_name = "${var.project_name}-pipeline-sfn-${var.environment}"
}

resource "aws_sfn_state_machine" "geyser_pipeline_state_machine" {
  name     = local.pipeline_state_machine_name
  role_arn = aws_iam_role.geyser_sfn_execution_role.arn
  # The definition lives in state_machine.asl.json so that tests/test_state_machine_local.py can run
  # the very same file in Step Functions Local (Docker, no AWS). Only these values are filled in.
  # jsonencode(jsondecode(...)) sends it minified, the same document: the provider's request decomposer reads the
  # escaped definition with a 64 KB bufio.Scanner, and the indented file passed that in v1.5 ("token too long").
  # Minified and escaped it was ~55 KB with comments by v3.0 (#209), so the "Comment" keys are removed on the
  # way (the regex below, at any depth: states, Parallel branches, Map iterators): ~41 KB. The source file keeps
  # every comment; the console doesn't show them. tests/test_consistency.py keeps the rendered definition under
  # 56 KB and checks it equals the source with only the Comment keys removed (every Comment is followed by
  # another key, so the regex takes its trailing comma too).
  definition = jsonencode(jsondecode(replace(templatefile("${path.module}/state_machine.asl.json", {
    job_definition             = aws_batch_job_definition.geyser_app_job_def.name
    job_queue                  = aws_batch_job_queue.geyser_queue.name
    deepvariant_job_definition = aws_batch_job_definition.geyser_deepvariant_job_def.name
    giraffe_job_definition     = aws_batch_job_definition.geyser_giraffe_job_def.name
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
    align_sizes                = jsonencode(local.align_override_sizes)
    variant_region_window_min  = local.variant_region_window_min
    variant_region_window_max  = local.variant_region_window_max
    # The hosted site (publish.tf, infrastructure/web/): the Publish state's switch, its job definition, and the
    # site bucket and distribution it passes per run.
    publish_enabled        = var.publish_enabled
    publish_job_definition = aws_batch_job_definition.geyser_publish_job_def.name
    site_bucket            = var.web_site_bucket
    site_distribution_id   = var.web_site_distribution_id
    # v1.3 the large route (genome_scale.tf) and the per-run parallelism.
    large_job_definition     = aws_batch_job_definition.geyser_app_large_job_def.name
    large_input_bytes        = local.large_input_bytes
    genome_chunk_count       = var.genome_chunk_count
    genome_align_concurrency = var.genome_align_concurrency
    genome_merge_threads     = var.genome_merge_threads
    align_concurrency_max    = local.align_concurrency_max
    calling_concurrency_max  = local.calling_concurrency_max
    merge_threads_max        = local.merge_threads_max
    preflight_vcpu           = local.large["preflight"].vcpu
    preflight_memory_mb      = local.large["preflight"].memory_mb
    qc_large_vcpu            = local.large["qc"].vcpu
    qc_large_memory_mb       = local.large["qc"].memory_mb
    chunk_vcpu               = local.large["chunk"].vcpu
    chunk_memory_mb          = local.large["chunk"].memory_mb
    align_merge_vcpu         = local.large["merge"].vcpu
    align_merge_memory_mb    = local.large["merge"].memory_mb
    # v1.5 the DeepVariant split (deepvariant_cpu.tf).
    deepvariant_cpu_job_definition = aws_batch_job_definition.geyser_deepvariant_cpu_job_def.name
    deepvariant_split_parts        = var.deepvariant_split_parts
    dv_parts_max                   = local.dv_parts_max
    dv_shards_by_parts             = jsonencode(local.dv_shards_by_parts)
    dv_part_timeout_seconds        = var.dv_part_timeout_seconds
    deepvariant_timeout_seconds    = var.deepvariant_timeout_seconds
    # The optional AlphaGenome annotation (v3.0, alphagenome.tf).
    annotate_job_definition = aws_batch_job_definition.geyser_annotate_job_def.name
    annotate_plan_vcpu      = var.annotate_plan_resources.vcpu
    annotate_plan_memory_mb = var.annotate_plan_resources.memory_mb
  }), "/\"Comment\":\\s*\"(?:[^\"\\\\]|\\\\.)*\",\\s*/", "")))

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
    precondition {
      # The defaults go through the same checks as an override (Run_Setting_Rules), so they must pass them.
      condition     = var.variant_region_window >= local.variant_region_window_min && var.variant_region_window <= local.variant_region_window_max
      error_message = "variant_region_window must be within the per-run override bounds (5 Mb to 250 Mb), or every run would fail its own settings check."
    }
    # v1.3: the parallelism defaults go through the per-run checks too, so they must fit the derived maxima.
    precondition {
      condition     = local.align_concurrency_max >= 1 && var.genome_align_concurrency <= local.align_concurrency_max
      error_message = "genome_align_concurrency must fit with QC in fargate_spot_max_vcpus: at most (fargate_spot_max_vcpus - qc vCPU) / chunk vCPU, and at least one chunk must fit."
    }
    precondition {
      condition     = var.variants_max_concurrency <= local.calling_concurrency_max
      error_message = "variants_max_concurrency (calling_concurrency's default) must be at most fargate_spot_max_vcpus / the variants vCPU."
    }
    precondition {
      condition     = var.genome_merge_threads <= local.merge_threads_max
      error_message = "genome_merge_threads must be at most the merge job's vCPU (large_route_resources.merge)."
    }
    precondition {
      # The pre-flight runs alongside QC, before the chunks.
      condition     = local.large["preflight"].vcpu + local.large["qc"].vcpu <= var.fargate_spot_max_vcpus
      error_message = "The pre-flight and QC run at the same time: their vCPUs together must fit in fargate_spot_max_vcpus."
    }
    precondition {
      # v1.5: the default goes through the per-run checks too (1 to dv_parts_max).
      condition     = var.deepvariant_split_parts <= local.dv_parts_max
      error_message = "deepvariant_split_parts must be at most fargate_spot_max_vcpus / dv_part_resources.vcpu (and one part must fit)."
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
