# infrastructure/main.tf

################################################################################
# NETWORKING
#############################################################################
resource "aws_vpc" "main" {
  cidr_block = "10.0.0.0/16"
  tags       = { Name = "${var.project_name}-vpc" }
}
resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.1.0/24"
  map_public_ip_on_launch = true
  tags                    = { Name = "${var.project_name}-public-subnet" }
}
resource "aws_subnet" "private" {
  vpc_id     = aws_vpc.main.id
  cidr_block = "10.0.2.0/24"
  tags       = { Name = "${var.project_name}-private-subnet" }
}
resource "aws_internet_gateway" "gw" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.project_name}-igw" }
}
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.gw.id
  }
  tags = { Name = "${var.project_name}-public-rt" }
}
resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}
# NAT is optional (var.enable_nat_gateway, default false) because it bills
# hourly. With it off, the private subnet has no internet route; Fargate jobs
# run in the public subnet with a public IP instead.
locals {
  # One element when NAT is on, none when off; drives the private default route.
  nat_route_keys = var.enable_nat_gateway ? ["default"] : []
}
resource "aws_eip" "nat" {
  count      = var.enable_nat_gateway ? 1 : 0
  domain     = "vpc"
  depends_on = [aws_internet_gateway.gw]
}
resource "aws_nat_gateway" "nat" {
  count         = var.enable_nat_gateway ? 1 : 0
  allocation_id = aws_eip.nat[0].id
  subnet_id     = aws_subnet.public.id
  tags          = { Name = "${var.project_name}-nat-gw" }
  depends_on    = [aws_internet_gateway.gw]
}
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  # Attribute syntax (not a route block) so that an empty list actually removes
  # the default route when NAT is off, instead of leaving it unmanaged.
  route = [for _ in local.nat_route_keys : {
    cidr_block                 = "0.0.0.0/0"
    nat_gateway_id             = aws_nat_gateway.nat[0].id
    carrier_gateway_id         = null
    core_network_arn           = null
    destination_prefix_list_id = null
    egress_only_gateway_id     = null
    gateway_id                 = null
    ipv6_cidr_block            = null
    local_gateway_id           = null
    network_interface_id       = null
    transit_gateway_id         = null
    vpc_endpoint_id            = null
    vpc_peering_connection_id  = null
  }]
  tags = { Name = "${var.project_name}-private-rt" }
}
resource "aws_route_table_association" "private" {
  subnet_id      = aws_subnet.private.id
  route_table_id = aws_route_table.private.id
}
################################################################################
# STORAGE (S3 and ECR)
################################################################################

resource "random_id" "bucket_suffix" {
  byte_length = 8
}

resource "aws_s3_bucket" "data_lake" {
  bucket = "${var.project_name}-data-lake-${random_id.bucket_suffix.hex}"
  tags   = { Name = "${var.project_name}-DataLake" }
}

resource "aws_ecr_repository" "geyser_app" {
  name         = "${var.project_name}-app"
  force_delete = true # Note: In production, consider removing this for safety.
  tags         = { Name = "${var.project_name}-ecr-repo" }
}

# Keep the 20 most recent images (any tag status) and expire the rest, so
# storage cost stays bounded as CI/local builds push a tag per commit.
resource "aws_ecr_lifecycle_policy" "geyser_app" {
  repository = aws_ecr_repository.geyser_app.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 20 most recent images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 20
      }
      action = { type = "expire" }
    }]
  })
}
################################################################################
# IAM ROLES AND POLICIES
################################################################################

resource "aws_iam_role" "geyser_batch_service_role" {
  name = "${var.project_name}-AWSBatchServiceRole"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17",
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "batch.amazonaws.com" } }]
  })
  tags = { Name = "${var.project_name}-BatchServiceRole" }
}

resource "aws_iam_role_policy_attachment" "geyser_batch_service_role_policy" {
  role       = aws_iam_role.geyser_batch_service_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBatchServiceRole"
}

resource "aws_iam_role" "geyser_batch_execution_role" {
  name = "${var.project_name}-BatchJobExecutionRole"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17",
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" } }]
  })
  tags = { Name = "${var.project_name}-JobExecutionRole" }
}

resource "aws_iam_role_policy_attachment" "geyser_batch_execution_role_policy" {
  role       = aws_iam_role.geyser_batch_execution_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# IAM Policy that grants our jobs access to the S3 bucket
resource "aws_iam_policy" "geyser_s3_access_policy" {
  name = "${var.project_name}S3AccessPolicy"
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Action   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
      Effect   = "Allow",
      Resource = [aws_s3_bucket.data_lake.arn, "${aws_s3_bucket.data_lake.arn}/*"]
    }]
  })
}

# Attach the S3 access policy to our job execution role
resource "aws_iam_role_policy_attachment" "s3_access" {
  role       = aws_iam_role.geyser_batch_execution_role.name
  policy_arn = aws_iam_policy.geyser_s3_access_policy.arn
}
################################################################################
# AWS BATCH INFRASTRUCTURE
################################################################################

resource "aws_batch_compute_environment" "geyser_fargate" {
  name         = "${var.project_name}-fargate-env" # provider 6: was compute_environment_name
  type         = "MANAGED"
  service_role = aws_iam_role.geyser_batch_service_role.arn
  compute_resources {
    type      = "FARGATE"
    max_vcpus = var.fargate_max_vcpus # spend-rate cap, see docs/design/claude-guardrails.md
    # Public subnet + assignPublicIp (job definition) so tasks reach ECR, S3 and
    # CloudWatch through the internet gateway without a NAT gateway. The VPC
    # default security group allows no inbound traffic from the internet.
    subnets            = [aws_subnet.public.id]
    security_group_ids = [aws_vpc.main.default_security_group_id]
  }
  tags = { Name = "${var.project_name}-ComputeEnv" }
}

# Fargate Spot: the same tasks on spare capacity, roughly 70% cheaper, but AWS can reclaim a task with
# two minutes' notice. The job definition's retry_strategy retries "Your Spot Task was interrupted*",
# so an interrupted stage reruns from its inputs in S3. Added next to the on-demand environment, never
# instead of it: the queue tries Spot first and falls back to on-demand Fargate when Spot has no
# capacity or is at its own cap (#124). Creating or changing a compute environment is outside the
# geyser-claude boundary (it sets the spend rate), so this one is applied by the user.
resource "aws_batch_compute_environment" "geyser_fargate_spot" {
  name         = "${var.project_name}-fargate-spot-env"
  type         = "MANAGED"
  service_role = aws_iam_role.geyser_batch_service_role.arn
  compute_resources {
    type               = "FARGATE_SPOT"
    max_vcpus          = var.fargate_spot_max_vcpus # its own spend-rate cap, see docs/design/claude-guardrails.md
    subnets            = [aws_subnet.public.id]
    security_group_ids = [aws_vpc.main.default_security_group_id]
  }
  tags = { Name = "${var.project_name}-ComputeEnvSpot" }
}

resource "aws_batch_job_queue" "geyser_queue" {
  name     = "${var.project_name}-job-queue"
  priority = 1
  state    = "ENABLED"
  # Spot first; on-demand Fargate takes what Spot can't place.
  compute_environment_order {
    order               = 1
    compute_environment = aws_batch_compute_environment.geyser_fargate_spot.arn
  }
  compute_environment_order {
    order               = 2
    compute_environment = aws_batch_compute_environment.geyser_fargate.arn
  }
  tags = { Name = "${var.project_name}-JobQueue" }
}

resource "aws_batch_job_definition" "geyser_app_job_def" {
  name                  = "${var.project_name}-app-job"
  type                  = "container"
  platform_capabilities = ["FARGATE"]
  container_properties = jsonencode(merge({
    image = "${aws_ecr_repository.geyser_app.repository_url}:${var.image_version}"
    # This role is for pulling the container and basic setup.
    executionRoleArn = aws_iam_role.geyser_batch_execution_role.arn
    # MODIFIED: This is the dedicated role for the application code itself.
    jobRoleArn = aws_iam_role.geyser_batch_task_role.arn
    fargatePlatformConfiguration = {
      platformVersion = "LATEST"
    }
    # Required for Fargate in a public subnet with no NAT: without a public IP
    # the task cannot pull its image or reach S3.
    networkConfiguration = {
      assignPublicIp = "ENABLED"
    }
    # Defaults only: the state machine sets each stage's size (var.stage_resources).
    resourceRequirements = [
      { type = "VCPU", value = "2" },
      { type = "MEMORY", value = "4096" }
    ]
    environment = [
      { name = "BUCKET_NAME", value = aws_s3_bucket.data_lake.bucket },
      { name = "APP_VERSION", value = var.image_version }
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.geyser_batch_jobs.name
        "awslogs-region"        = data.aws_region.current.region
        "awslogs-stream-prefix" = "geyser"
      }
    }
    },
    # Fargate gives 20 GiB free; only ask for more when job_disk_gb says so (21-200 GiB, billed).
    var.job_disk_gb > 20 ? { ephemeralStorage = { sizeInGiB = var.job_disk_gb } } : {}
  ))

  # Retry infrastructure failures only, never the application's own errors: a corrupt upload or a
  # tool's non-zero exit fails fast. Conditions are checked in order; the first match wins.
  # onStatusReason matches from the start of the reason (only a trailing * is allowed).
  retry_strategy {
    attempts = 3
    evaluate_on_exit {
      on_status_reason = "Host EC2*" # the instance went away under the job
      action           = "RETRY"
    }
    evaluate_on_exit {
      on_status_reason = "Your Spot Task was interrupted*" # Fargate Spot (Workstream C)
      action           = "RETRY"
    }
    evaluate_on_exit {
      on_status_reason = "Task failed to start*" # e.g. capacity or ENI trouble before the container ran
      action           = "RETRY"
    }
    evaluate_on_exit {
      on_status_reason = "ResourceInitializationError*" # image pull or log setup failed
      action           = "RETRY"
    }
    evaluate_on_exit {
      on_status_reason = "*" # everything else, including every non-zero exit code
      action           = "EXIT"
    }
  }
  tags = { Name = "${var.project_name}-AppJobDef" }
}

