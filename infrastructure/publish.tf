# infrastructure/publish.tf: the Publish job (W2/W3), run by the state machine's Publish state at the end of every
# run when publish_enabled. It renders the sample's pages and the index into the site bucket (infrastructure/web/)
# and invalidates them on CloudFront.
#
# Its own image, geyser-publish (app-publish/Dockerfile, T3's W2: FROM geyser-app plus web/ and Django), so the
# pipeline image stays slim. Its own job definition and task role, so only this job can write the site.
# T3's contract: WORKDIR /geyser/web; `python manage.py publish <srr_id>` (the site from $SITE_BUCKET); exit 0 or
# non-zero; it republishes index.html, the sample's pages, 404.html, the hashed assets and csp-hashes.json.

resource "aws_ecr_repository" "geyser_publish" {
  name         = "${var.project_name}-publish"
  force_delete = true
  tags         = { Name = "${var.project_name}-publish-ecr-repo" }
}

# ECR storage is $0.10/GB-month: keep the five newest images.
resource "aws_ecr_lifecycle_policy" "geyser_publish" {
  repository = aws_ecr_repository.geyser_publish.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 5 most recent publish images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 }
      action       = { type = "expire" }
    }]
  })
}

resource "aws_iam_role" "geyser_publish_task_role" {
  name = "${var.project_name}-publish-task-role-${var.environment}"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17",
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" } }]
  })
  tags = { Name = "${var.project_name}-publish-task-role", Environment = var.environment, ManagedBy = "Terraform" }
}

# What a page shows, read-only. variants_deepvariant/ and variants_pangenome/ hold the sample's calls when DeepVariant
# or pangenome-aware DeepVariant was the run's caller (the run record says which); the publisher fails loud on a 403
# rather than publish a page without its calls. No raw_reads/ or alignments/ objects: the publisher takes their
# presence from the listing. truth/ stays unreadable (the publisher reads that as "no truth"). T3's contract.
resource "aws_iam_role_policy" "geyser_publish_read_data_lake" {
  name = "${var.project_name}-publish-read-data-lake-${var.environment}"
  role = aws_iam_role.geyser_publish_task_role.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Sid      = "ListDataLake"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.data_lake.arn
      },
      {
        Sid    = "ReadWhatPagesShow"
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = [
          for prefix in ["qc_reports", "variants", "variants_deepvariant", "variants_pangenome", "stats", "runs", "metrics"] :
          "${aws_s3_bucket.data_lake.arn}/${prefix}/*"
        ]
      }
    ]
  })
}

# Writing the site and invalidating its pages: only once the site exists (infrastructure/web/'s outputs).
resource "aws_iam_role_policy" "geyser_publish_write_site" {
  count = var.publish_enabled ? 1 : 0
  name  = "${var.project_name}-publish-write-site-${var.environment}"
  role  = aws_iam_role.geyser_publish_task_role.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        # PutObject only: no delete, no read or list (T3's contract).
        Sid      = "WriteSite"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "arn:aws:s3:::${var.web_site_bucket}/*"
      },
      {
        # Three paths per publish: /, /index.html, /samples/<id>/* (free tier: 1,000 paths a month).
        Sid      = "InvalidatePages"
        Effect   = "Allow"
        Action   = ["cloudfront:CreateInvalidation"]
        Resource = "arn:aws:cloudfront::${data.aws_caller_identity.current.account_id}:distribution/${var.web_site_distribution_id}"
      }
    ]
  })

  lifecycle {
    precondition {
      condition     = var.web_site_bucket != "" && var.web_site_distribution_id != ""
      error_message = "publish_enabled needs web_site_bucket and web_site_distribution_id (infrastructure/web/'s outputs)."
    }
  }
}

resource "aws_batch_job_definition" "geyser_publish_job_def" {
  name                  = "${var.project_name}-publish-job"
  type                  = "container"
  platform_capabilities = ["FARGATE"]
  propagate_tags        = true # the Stage tag reaches the Fargate task (#64)
  container_properties = jsonencode({
    image                        = "${aws_ecr_repository.geyser_publish.repository_url}:${var.publish_image_version}"
    executionRoleArn             = aws_iam_role.geyser_batch_execution_role.arn # image pull and logs, as every job
    jobRoleArn                   = aws_iam_role.geyser_publish_task_role.arn
    fargatePlatformConfiguration = { platformVersion = "LATEST" }
    networkConfiguration         = { assignPublicIp = "ENABLED" } # no NAT
    resourceRequirements = [
      { type = "VCPU", value = tostring(var.publish_resources.vcpu) },
      { type = "MEMORY", value = tostring(var.publish_resources.memory_mb) }
    ]
    environment = [
      { name = "BUCKET_NAME", value = aws_s3_bucket.data_lake.bucket },
      { name = "STORAGE_BACKEND", value = "s3" }, # the web UI defaults to local storage
      { name = "AWS_REGION", value = data.aws_region.current.region },
      # SITE_BUCKET and CLOUDFRONT_DISTRIBUTION_ID come per run from the Publish state, so switching publishing on
      # (phase 2) changes only the state machine, never this job definition (a new revision).
      # The pages' API (status polling, downloads), without the function URL's trailing slash.
      { name = "GEYSER_API_BASE", value = trimsuffix(aws_lambda_function_url.geyser_web_api.function_url, "/") },
      { name = "APP_VERSION", value = var.publish_image_version }
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.geyser_batch_jobs.name
        "awslogs-region"        = data.aws_region.current.region
        "awslogs-stream-prefix" = "geyser"
      }
    }
  })

  retry_strategy {
    attempts = 3
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
  tags = { Name = "${var.project_name}-PublishJobDef" }
}
