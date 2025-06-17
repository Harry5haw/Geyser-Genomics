# This block tells Terraform that we will be using the AWS provider
# and specifies the version we want to use, which is a best practice.
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

# This block configures the AWS provider itself, telling it which
# region to create our resources in by default.
provider "aws" {
  region = "eu-west-2" # London
}

# This is our first "resource" block. It defines a single piece of
# infrastructure we want to create - in this case, an S3 bucket.
resource "aws_s3_bucket" "data_lake" {
  # We need to give our S3 bucket a globally unique name.
  # We will use a random suffix to ensure it doesn't conflict with anyone else's.
  # Replace "harry-genomeflow-data-lake" with your own unique prefix if you like.
  bucket = "harry-genomeflow-data-lake-${random_id.bucket_suffix.hex}"

  tags = {
    Name    = "GenomeFlow Data Lake"
    Project = "GenomeFlow FYP"
  }
}

# This is a helper resource that generates a random string.
# We use this to ensure our S3 bucket name is always unique.
resource "random_id" "bucket_suffix" {
  byte_length = 8
}

# This block creates the "raw_reads" folder inside our data lake bucket.
resource "aws_s3_object" "raw_reads_folder" {
  # We reference the bucket we created above using its resource type and name.
  bucket = aws_s3_bucket.data_lake.id
  # The "key" is the full path/filename of the object. Ending with a slash
  # makes it a folder.
  key    = "raw_reads/"
}

# This block creates the "reference" folder.
resource "aws_s3_object" "reference_folder" {
  bucket = aws_s3_bucket.data_lake.id
  key    = "reference/"
}

# This block creates the "decompressed" folder for intermediate files.
resource "aws_s3_object" "decompressed_folder" {
  bucket = aws_s3_bucket.data_lake.id
  key    = "decompressed/"
}

# This block creates the "qc_reports" folder.
resource "aws_s3_object" "qc_reports_folder" {
  bucket = aws_s3_bucket.data_lake.id
  key    = "qc_reports/"
}

# This block creates the "alignments" folder.
resource "aws_s3_object" "alignments_folder" {
  bucket = aws_s3_bucket.data_lake.id
  key    = "alignments/"
}

# This block creates the "variants" folder.
resource "aws_s3_object" "variants_folder" {
  bucket = aws_s3_bucket.data_lake.id
  key    = "variants/"
}

# --- ECR (Elastic Container Registry) ---
# This block creates a private repository to store our custom Docker image.

resource "aws_ecr_repository" "genomeflow_app" {
  name                 = "genomeflow-app" # The name of our repository
  image_tag_mutability = "MUTABLE"      # Allows us to overwrite image tags like 'latest'

  image_scanning_configuration {
    scan_on_push = true # A good security practice: scans image for vulnerabilities on push
  }

  tags = {
    Name    = "GenomeFlow Application Repository"
    Project = "TerraFlow Genomics FYP"
  }
}

# --- AWS Batch Compute Environment ---
# This defines the "computers" that will run our jobs. We are using
# Fargate, which is serverless, so we don't have to manage any servers.
resource "aws_batch_compute_environment" "genomeflow_fargate" {
  compute_environment_name = "genomeflow-fargate-env"
  type                     = "MANAGED"
  service_role             = aws_iam_role.aws_batch_service_role.arn # We will create this IAM role next

  compute_resources {
    type        = "FARGATE_SPOT" # Use Fargate Spot for cost savings
    max_vcpus   = 16             # The maximum number of CPUs to use across all jobs
    subnets     = ["subnet-0a26a1982d7f4fa5b"] # We need to tell it where to run
    security_group_ids = ["sg-0552846c7dfc98b7b"] # And what firewall rules to use
  }

  tags = {
    Project = "TerraFlow Genomics FYP"
  }
}

# --- IAM Role for AWS Batch ---
# AWS Batch needs permissions to operate. This standard role grants them.
resource "aws_iam_role" "aws_batch_service_role" {
  name = "AWSBatchServiceRoleForGenomeFlow"

  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Action = "sts:AssumeRole",
        Effect = "Allow",
        Principal = {
          Service = "batch.amazonaws.com"
        }
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "aws_batch_service_role_policy" {
  role       = aws_iam_role.aws_batch_service_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBatchServiceRole"
}

# --- AWS Batch Job Queue ---
# This creates the "waiting line" for our jobs.
resource "aws_batch_job_queue" "genomeflow_queue" {
  name     = "genomeflow-job-queue"
  priority = 1
  state    = "ENABLED"

  compute_environment_order {
    order                = 1
    compute_environment = aws_batch_compute_environment.genomeflow_fargate.arn
  }

  tags = {
    Project = "TerraFlow Genomics FYP"
  }
}

