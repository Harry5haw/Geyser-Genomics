# infrastructure/alphagenome.tf: the optional AlphaGenome annotation (3.0), run by the Annotate state after
# the quality gate when a run asks for it (annotate = alphagenome in the input, or S3 metadata annotate on the
# upload). Its own job definition, the only one that maps the API key, with its own execution role, the only
# one that may read it.
#
# The key lives in SSM Parameter Store (SecureString, default key aws/ssm, standard tier: free). Terraform
# never creates, reads or stores it: the parameter is referenced by name only (no data source, no resource),
# which also keeps every plan working for geyser-claude, whose boundary has no ssm access. The user puts the
# key in with their own identity:
#   AWS_PROFILE=default aws ssm put-parameter --name /geyser/alphagenome/api-key --type SecureString --value <key>
# Until then, an annotate job fails to start ("unable to pull secrets"), and the state machine notes that
# without failing the run.

locals {
  # The parameter's ARN from its name: parameter names start with /, which the ARN doesn't repeat.
  alphagenome_api_key_parameter_arn = "arn:aws:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter/${trimprefix(var.alphagenome_api_key_parameter, "/")}"
}

# ECS reads a job's secrets with the execution role (not the job role) when the container starts. A separate
# execution role, so no other job definition can map the key.
resource "aws_iam_role" "geyser_annotate_execution_role" {
  name = "${var.project_name}-AnnotateExecutionRole"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17",
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" } }]
  })
  tags = { Name = "${var.project_name}-AnnotateExecutionRole" }
}

# Image pull from ECR and the awslogs log stream, as for the other jobs.
resource "aws_iam_role_policy_attachment" "geyser_annotate_execution_role_policy" {
  role       = aws_iam_role.geyser_annotate_execution_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# The one parameter, read only. ECS calls ssm:GetParameters (plural) for secrets. No kms:Decrypt: with the
# default aws/ssm key, SSM decrypts for the caller. A customer-managed key would need kms:Decrypt on it here.
resource "aws_iam_role_policy" "geyser_annotate_read_api_key" {
  name = "${var.project_name}-annotate-read-alphagenome-key"
  role = aws_iam_role.geyser_annotate_execution_role.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Sid      = "ReadAlphaGenomeApiKey"
      Effect   = "Allow"
      Action   = ["ssm:GetParameters"]
      Resource = local.alphagenome_api_key_parameter_arn
    }]
  })
}

# Its own image (app-annotate/Dockerfile, #207): the geyser-app image plus the alphagenome client, ~500 MB of
# dependencies kept out of the slim pipeline image. The same pattern as geyser-deepvariant.
resource "aws_ecr_repository" "geyser_annotate" {
  name         = "${var.project_name}-annotate"
  force_delete = true
  tags         = { Name = "${var.project_name}-annotate-ecr-repo" }

  # Basic scanning (free): known CVEs of every pushed image, in the ECR console (#64).
  image_scanning_configuration {
    scan_on_push = true
  }
}

# ECR storage is $0.10/GB-month: keep the five newest images (a few rollbacks' worth).
resource "aws_ecr_lifecycle_policy" "geyser_annotate" {
  repository = aws_ecr_repository.geyser_annotate.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 5 most recent annotate images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 }
      action       = { type = "expire" }
    }]
  })
}

resource "aws_batch_job_definition" "geyser_annotate_job_def" {
  name                  = "${var.project_name}-annotate-job"
  type                  = "container"
  platform_capabilities = ["FARGATE"]
  propagate_tags        = true # the Stage tag (annotate) reaches the Fargate task (#149)
  container_properties = jsonencode({
    # The annotate image (the app image plus the alphagenome client).
    image            = "${aws_ecr_repository.geyser_annotate.repository_url}:${var.annotate_image_version}"
    executionRoleArn = aws_iam_role.geyser_annotate_execution_role.arn
    # The app's own role (S3 data lake, CloudWatch metrics), as for every stage.
    jobRoleArn                   = aws_iam_role.geyser_batch_task_role.arn
    fargatePlatformConfiguration = { platformVersion = "LATEST" }
    networkConfiguration         = { assignPublicIp = "ENABLED" } # no NAT: the public IP reaches ECR, S3 and the API
    # The plan job's size; Annotate overrides it with the plan's (#207: 1 vCPU / 2 GiB or 2 / 3 GiB).
    resourceRequirements = [
      { type = "VCPU", value = tostring(var.annotate_plan_resources.vcpu) },
      { type = "MEMORY", value = tostring(var.annotate_plan_resources.memory_mb) }
    ]
    # Fargate's disk can't be overridden per job: fixed at the plan's largest need (disk_gib advisory, #207).
    ephemeralStorage = { sizeInGiB = var.annotate_disk_gb }
    environment = [
      { name = "BUCKET_NAME", value = aws_s3_bucket.data_lake.bucket },
      { name = "APP_VERSION", value = var.annotate_image_version }
    ]
    # The API key, into the container's environment at start. Never in the job definition itself, the state
    # machine, tfvars or any output: only the parameter's ARN is.
    secrets = [
      { name = "ALPHAGENOME_API_KEY", valueFrom = local.alphagenome_api_key_parameter_arn }
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

  # As the app job definition: retry infrastructure failures only, never the application's own errors.
  # A missing parameter is a ResourceInitializationError and is retried too (three short attempts),
  # then the state machine notes the failure.
  retry_strategy {
    attempts = 3
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
  tags = { Name = "${var.project_name}-AnnotateJobDef" }
}
