terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7.0"
    }
  }
}

provider "aws" {
  region = "eu-west-2"
}

################################################################################
# DATA SOURCES
################################################################################
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

data "aws_security_group" "default" {
  name   = "default"
  vpc_id = data.aws_vpc.default.id
}

################################################################################
# STORAGE
################################################################################
resource "aws_s3_bucket" "data_lake" {
  bucket = "harry-genomeflow-data-lake-${random_id.bucket_suffix.hex}"
  lifecycle { prevent_destroy = true }
}

resource "random_id" "bucket_suffix" { byte_length = 8 }

resource "aws_ecr_repository" "genomeflow_app" {
  name         = "genomeflow-app"
  force_delete = true
}

################################################################################
# IAM ROLES AND POLICIES
################################################################################
resource "aws_iam_role" "aws_batch_service_role" {
  name               = "AWSBatchServiceRoleForGenomeFlow"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17",
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "batch.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy_attachment" "aws_batch_service_role_policy" {
  role       = aws_iam_role.aws_batch_service_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBatchServiceRole"
}

resource "aws_iam_role" "ecs_task_execution_role" {
  name               = "BatchJobExecutionRoleForGenomeFlow"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17",
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_task_execution_role_policy" {
  role       = aws_iam_role.ecs_task_execution_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_policy" "s3_access_policy" {
  name   = "GenomeFlowS3AccessPolicy"
  policy = jsonencode({
    Version   = "2012-10-17",
    Statement = [{
      Action   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
      Effect   = "Allow",
      Resource = [aws_s3_bucket.data_lake.arn, "${aws_s3_bucket.data_lake.arn}/*"]
    }]
  })
}

resource "aws_iam_role_policy_attachment" "s3_access" {
  role       = aws_iam_role.ecs_task_execution_role.name
  policy_arn = aws_iam_policy.s3_access_policy.arn
}

################################################################################
# AWS BATCH
################################################################################
resource "aws_batch_compute_environment" "genomeflow_fargate" {
  compute_environment_name = "genomeflow-fargate-env"
  type                     = "MANAGED"
  service_role             = aws_iam_role.aws_batch_service_role.arn
  compute_resources {
    type               = "FARGATE"
    max_vcpus          = 16
    subnets            = data.aws_subnets.default.ids
    security_group_ids = [data.aws_security_group.default.id]
    #assign_public_ip   = "ENABLED" # This is invalid, we will comment it out
  }
}

resource "aws_batch_job_queue" "genomeflow_queue" {
  name     = "genomeflow-job-queue"
  priority = 1
  state    = "ENABLED"
  compute_environment_order {
    order               = 1
    compute_environment = aws_batch_compute_environment.genomeflow_fargate.arn
  }
}

resource "aws_batch_job_definition" "genomeflow_job_def" {
  name = "genomeflow-job-definition"
  type = "container"
  platform_capabilities = ["FARGATE"]
  container_properties = jsonencode({
    image            = aws_ecr_repository.genomeflow_app.repository_url
    executionRoleArn = aws_iam_role.ecs_task_execution_role.arn
    fargatePlatformConfiguration = { platformVersion = "LATEST" }
    resourceRequirements = [
      { type = "VCPU", value = "1" },
      { type = "MEMORY", value = "4096" }
    ]
  })
}
