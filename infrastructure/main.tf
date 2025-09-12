# Final verification test for CI/CD

##N
################################################################################
# NETWORKING
################################################################################
# ... (all the networking resources from before - no changes here)
resource "aws_vpc" "main" {
  cidr_block = "10.0.0.0/16"
  tags       = { Name = "genomeflow-vpc" }
}
resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.1.0/24"
  map_public_ip_on_launch = true
  tags                    = { Name = "genomeflow-public-subnet" }
}
resource "aws_subnet" "private" {
  vpc_id     = aws_vpc.main.id
  cidr_block = "10.0.2.0/24"
  tags       = { Name = "genomeflow-private-subnet" }
}
resource "aws_internet_gateway" "gw" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "genomeflow-igw" }
}
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.gw.id
  }
  tags = { Name = "genomeflow-public-rt" }
}
resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}
resource "aws_eip" "nat" {
  domain     = "vpc"
  depends_on = [aws_internet_gateway.gw]
}
resource "aws_nat_gateway" "nat" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public.id
  tags          = { Name = "genomeflow-nat-gw" }
  depends_on    = [aws_internet_gateway.gw]
}
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.nat.id
  }
  tags = { Name = "genomeflow-private-rt" }
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
  bucket = "teraflow-data-lake-${random_id.bucket_suffix.hex}"
  tags   = { Name = "TerraFlow-DataLake" }
}

resource "aws_ecr_repository" "genomeflow_app" {
  name         = "genomeflow-app"
  force_delete = true
  tags         = { Name = "genomeflow-ecr-repo" }
}
################################################################################
# IAM ROLES AND POLICIES
################################################################################

resource "aws_iam_role" "aws_batch_service_role" {
  name               = "TerraFlow-AWSBatchServiceRole"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17",
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "batch.amazonaws.com" } }]
  })
  tags = { Name = "TerraFlow-BatchServiceRole" }
}

resource "aws_iam_role_policy_attachment" "aws_batch_service_role_policy" {
  role       = aws_iam_role.aws_batch_service_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBatchServiceRole"
}

resource "aws_iam_role" "ecs_task_execution_role" {
  name               = "TerraFlow-BatchJobExecutionRole"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17",
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" } }]
  })
  tags = { Name = "TerraFlow-JobExecutionRole" }
}

resource "aws_iam_role_policy_attachment" "ecs_task_execution_role_policy" {
  role       = aws_iam_role.ecs_task_execution_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# IAM Policy that grants our jobs access to the S3 bucket
resource "aws_iam_policy" "s3_access_policy" {
  name = "TerraFlowS3AccessPolicy"
  policy = jsonencode({
    Version   = "2012-10-17",
    Statement = [{
      Action   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
      Effect   = "Allow",
      Resource = [aws_s3_bucket.data_lake.arn, "${aws_s3_bucket.data_lake.arn}/*"]
    }]
  })
}

# Attach the S3 access policy to our job execution role
resource "aws_iam_role_policy_attachment" "s3_access" {
  role       = aws_iam_role.ecs_task_execution_role.name
  policy_arn = aws_iam_policy.s3_access_policy.arn
}
################################################################################
# AWS BATCH INFRASTRUCTURE
################################################################################

resource "aws_batch_compute_environment" "genomeflow_fargate" {
  compute_environment_name = "teraflow-fargate-env"
  type                     = "MANAGED"
  service_role             = aws_iam_role.aws_batch_service_role.arn
  compute_resources {
    type               = "FARGATE"
    max_vcpus          = 16
    subnets            = [aws_subnet.private.id]
    security_group_ids = [aws_vpc.main.default_security_group_id]
  }
  tags = { Name = "TerraFlow-ComputeEnv" }
}

resource "aws_batch_job_queue" "genomeflow_queue" {
  name     = "teraflow-job-queue"
  priority = 1
  state    = "ENABLED"
  compute_environment_order {
    order               = 1
    compute_environment = aws_batch_compute_environment.genomeflow_fargate.arn
  }
  tags = { Name = "TerraFlow-JobQueue" }
}

resource "aws_batch_job_definition" "genomeflow_app_job_def" {
  name                  = "teraflow-app-job"
  type                  = "container"
  platform_capabilities = ["FARGATE"]
  container_properties = jsonencode({
    image            ="${aws_ecr_repository.genomeflow_app.repository_url}:v1.3"
    executionRoleArn = aws_iam_role.ecs_task_execution_role.arn
    jobRoleArn       = aws_iam_role.ecs_task_execution_role.arn
    fargatePlatformConfiguration = {
      platformVersion = "LATEST"
    }
    resourceRequirements = [
      { type = "VCPU", value = "2" },
      { type = "MEMORY", value = "4096" }
    ]
    # THIS IS THE CRITICAL ADDITION:
    # Inject the S3 bucket name as an environment variable.
    environment = [
      { name = "BUCKET_NAME", value = aws_s3_bucket.data_lake.bucket }
    ]
  })
  tags = { Name = "TerraFlow-AppJobDef" }
}
################################################################################
# OUTPUTS
###############################################################################
#Gone in rebuild
################################################################################
