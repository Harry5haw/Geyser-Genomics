# infrastructure/genome_scale.tf: v1.4 Big inputs, the large route's job definition.
#
# Whole genomes run on the existing Fargate Spot environments (no new compute environment): a pre-flight, the
# chunks aligned in parallel, a streaming merge (state_machine.asl.json, Large_QC_and_Align). They need more disk
# than the app job definition's free 20 GiB, and Batch's ContainerOverrides can't set ephemeral storage, so this is
# the app job definition again with large_disk_gb. Same image, roles, network, logs and retry rules.

resource "aws_batch_job_definition" "geyser_app_large_job_def" {
  name                  = "${var.project_name}-app-large-job"
  type                  = "container"
  platform_capabilities = ["FARGATE"]
  container_properties = jsonencode({
    image                        = "${aws_ecr_repository.geyser_app.repository_url}:${var.image_version}"
    executionRoleArn             = aws_iam_role.geyser_batch_execution_role.arn
    jobRoleArn                   = aws_iam_role.geyser_batch_task_role.arn
    fargatePlatformConfiguration = { platformVersion = "LATEST" }
    networkConfiguration         = { assignPublicIp = "ENABLED" } # no NAT: the public IP reaches ECR and S3
    # Defaults only: the state machine sets each job's size (var.large_route_resources).
    resourceRequirements = [
      { type = "VCPU", value = "8" },
      { type = "MEMORY", value = "16384" }
    ]
    environment = [
      { name = "BUCKET_NAME", value = aws_s3_bucket.data_lake.bucket },
      { name = "APP_VERSION", value = var.image_version }
    ]
    ephemeralStorage = { sizeInGiB = var.large_disk_gb }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.geyser_batch_jobs.name
        "awslogs-region"        = data.aws_region.current.region
        "awslogs-stream-prefix" = "geyser"
      }
    }
  })

  # As the app job definition: retry infrastructure failures only (a Fargate Spot interruption included), never
  # the application's own errors.
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
  tags = { Name = "${var.project_name}-AppLargeJobDef" }
}
