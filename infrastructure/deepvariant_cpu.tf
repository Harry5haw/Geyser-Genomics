# infrastructure/deepvariant_cpu.tf: the DeepVariant split's CPU part jobs (v1.5; app half #228, infra #236).
# Design: docs/design/deepvariant-split.md (the jobs' contract) and docs/design/deepvariant-split-infra.md.
#
# In split mode (the run setting dv_parts > 1), make_examples, which is CPU-only and 84% of the stage, runs as
# dv_parts jobs in parallel on the Fargate queue (Spot first, like the bcftools shards). The GPU job (deepvariant.tf)
# then runs only call_variants and postprocess_variants on their examples (--from-examples). The image is
# app-deepvariant/Dockerfile built with --build-arg DV_TAG=1.10.0: the same DeepVariant version as the GPU image,
# which the GPU job checks before any GPU work.

resource "aws_ecr_repository" "geyser_deepvariant_cpu" {
  name         = "${var.project_name}-deepvariant-cpu"
  force_delete = true
  tags         = { Name = "${var.project_name}-deepvariant-cpu-ecr-repo" }

  # Basic scanning (free): known CVEs of every pushed image, in the ECR console.
  image_scanning_configuration {
    scan_on_push = true
  }
}

# ECR storage is $0.10/GB-month, and each image is several GB: keep only the two newest.
resource "aws_ecr_lifecycle_policy" "geyser_deepvariant_cpu" {
  repository = aws_ecr_repository.geyser_deepvariant_cpu.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 2 most recent DeepVariant CPU images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 2 }
      action       = { type = "expire" }
    }]
  })
}

resource "aws_batch_job_definition" "geyser_deepvariant_cpu_job_def" {
  name                  = "${var.project_name}-deepvariant-cpu-job"
  type                  = "container"
  platform_capabilities = ["FARGATE"]
  container_properties = jsonencode({
    image                        = "${aws_ecr_repository.geyser_deepvariant_cpu.repository_url}:${var.deepvariant_cpu_image_version}"
    command                      = ["/opt/geyser-venv/bin/python", "deepvariant_task.py", "--help"] # set per run by the state machine
    executionRoleArn             = aws_iam_role.geyser_batch_execution_role.arn
    jobRoleArn                   = aws_iam_role.geyser_batch_task_role.arn # reads the CRAM, writes intermediate/dv/
    fargatePlatformConfiguration = { platformVersion = "LATEST" }
    networkConfiguration         = { assignPublicIp = "ENABLED" } # no NAT: the public IP reaches ECR and S3
    resourceRequirements = [
      { type = "VCPU", value = tostring(var.dv_part_resources.vcpu) },
      { type = "MEMORY", value = tostring(var.dv_part_resources.memory_mb) }
    ]
    # Each part downloads the whole CRAM (~17 GB for a 30x genome), the reference and its share of the examples.
    ephemeralStorage = { sizeInGiB = var.dv_part_disk_gb }
    # The same DEEPVARIANT_* as the GPU job definition (the GPU job refuses examples made with another
    # DEEPVARIANT_MODEL_TYPE or DEEPVARIANT_EXTRA_ARGS: neither job definition sets them, a consistency test pins
    # it). DEEPVARIANT_SHARDS is the state machine's, per run: parts x this job's vCPUs.
    environment = [
      { name = "BUCKET_NAME", value = aws_s3_bucket.data_lake.bucket },
      { name = "APP_VERSION", value = var.deepvariant_cpu_image_version },
      # The app stops itself, with the reason in the log, before the state machine's attempt limit kills it.
      { name = "DEEPVARIANT_TIMEOUT_SECONDS", value = tostring(var.dv_part_timeout_seconds - 300) },
      { name = "DEEPVARIANT_IDLE_TIMEOUT_SECONDS", value = "900" },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.geyser_batch_jobs.name
        "awslogs-region"        = data.aws_region.current.region
        "awslogs-stream-prefix" = "deepvariant-cpu"
      }
    }
  })

  # As the other job definitions: retry infrastructure failures only (a Fargate Spot interruption included), never
  # the application's own errors. 5 attempts, not 3: a genome part runs ~1 h on Fargate Spot, and 4 Nov 2026 saw
  # ~20 interruptions on ~20-minute chunks, so 3 could run out and send the whole sample to the bcftools fallback.
  retry_strategy {
    attempts = 5
    evaluate_on_exit {
      on_status_reason = "Host EC2*"
      action           = "RETRY"
    }
    evaluate_on_exit {
      on_status_reason = "Your Spot Task was interrupted*"
      action           = "RETRY"
    }
    evaluate_on_exit {
      on_status_reason = "Task failed to start*"
      action           = "RETRY"
    }
    evaluate_on_exit {
      on_status_reason = "ResourceInitializationError*"
      action           = "RETRY"
    }
    evaluate_on_exit {
      on_status_reason = "*"
      action           = "EXIT"
    }
  }
  tags = { Name = "${var.project_name}-DeepVariantCpuJobDef" }
}
