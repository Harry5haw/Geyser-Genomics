# infrastructure/deepvariant.tf: DeepVariant as the variants caller in the cloud (G2: #43, #44).
# Design and sizing: docs/design/cloud-deepvariant.md.
#
# The DeepVariant image (app-deepvariant/Dockerfile, ~9.8 GB compressed) has its own repository and
# runs on the GPU Batch environment (batch_gpu.tf), on EC2 because Fargate has no GPUs. The state
# machine picks it when the run's input has caller = "deepvariant" and falls back to bcftools if it
# fails (state_machine.asl.json, Choose_Caller).

resource "aws_ecr_repository" "geyser_deepvariant" {
  name         = "${var.project_name}-deepvariant"
  force_delete = true
  tags         = { Name = "${var.project_name}-deepvariant-ecr-repo" }
}

# ECR storage is $0.10/GB-month, and each image is ~9.8 GB: keep only the two newest (~$2/month).
resource "aws_ecr_lifecycle_policy" "geyser_deepvariant" {
  repository = aws_ecr_repository.geyser_deepvariant.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 2 most recent DeepVariant images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 2 }
      action       = { type = "expire" }
    }]
  })
}

resource "aws_batch_job_definition" "geyser_deepvariant_job_def" {
  name                  = "${var.project_name}-deepvariant-job"
  type                  = "container"
  platform_capabilities = ["EC2"]
  container_properties = jsonencode({
    image            = "${aws_ecr_repository.geyser_deepvariant.repository_url}:${var.deepvariant_image_version}"
    command          = ["/opt/geyser-venv/bin/python", "deepvariant_task.py", "--help"] # set per run by the state machine
    executionRoleArn = aws_iam_role.geyser_batch_execution_role.arn
    jobRoleArn       = aws_iam_role.geyser_batch_task_role.arn
    resourceRequirements = [
      { type = "GPU", value = "1" },
      { type = "VCPU", value = tostring(var.deepvariant_resources.vcpu) },
      { type = "MEMORY", value = tostring(var.deepvariant_resources.memory_mb) },
    ]
    environment = [
      { name = "BUCKET_NAME", value = aws_s3_bucket.data_lake.bucket },
      { name = "APP_VERSION", value = var.deepvariant_image_version },
      # make_examples parallelism = the job's vCPUs (app-deepvariant/README.md).
      { name = "DEEPVARIANT_SHARDS", value = tostring(var.deepvariant_resources.vcpu) },
      # The app's own limits (deepvariant_task.py, feature/dv-robustness): the run stops itself, with the
      # reason in the log, before the state machine's 3600 s attempt limit kills the container; a run
      # that prints nothing for 15 minutes is a hang (GPU out of memory), not progress.
      { name = "DEEPVARIANT_TIMEOUT_SECONDS", value = "3300" },
      { name = "DEEPVARIANT_IDLE_TIMEOUT_SECONDS", value = "900" },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.geyser_batch_jobs.name
        "awslogs-region"        = data.aws_region.current.region
        "awslogs-stream-prefix" = "deepvariant"
      }
    }
  })

  # Same rule as the app job (main.tf): retry infrastructure failures only, never the application's.
  retry_strategy {
    attempts = 3
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

  lifecycle {
    precondition {
      condition     = var.deepvariant_resources.vcpu <= var.gpu_max_vcpus
      error_message = "deepvariant_resources.vcpu must fit in gpu_max_vcpus, or the job can never be placed."
    }
    precondition {
      # Every type the environment may pick must hold the job, with >= 2 GiB left for the ECS agent and OS.
      condition     = var.deepvariant_resources.vcpu <= local.gpu_min_instance_vcpu && var.deepvariant_resources.memory_mb <= local.gpu_min_instance_mib - 2048
      error_message = "deepvariant_resources must fit the smallest gpu_instance_types (its vCPUs, and its memory less 2 GiB)."
    }
  }
  tags = { Name = "${var.project_name}-DeepVariantJobDef" }
}
