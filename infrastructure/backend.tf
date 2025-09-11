# infrastructure/backend.tf

# S3 Bucket for storing the Terraform state file
resource "aws_s3_bucket" "terraform_state" {
  bucket = "${var.project_name}-tfstate-${data.aws_caller_identity.current.account_id}-${data.aws_region.current.name}"

  # Prevent accidental deletion of the state file bucket
  lifecycle {
    prevent_destroy = true
  }

  tags = {
    Name        = "${var.project_name}-terraform-state-bucket"
    Environment = var.environment
    ManagedBy   = "Terraform"
  }
}

# Enable versioning on the S3 bucket for state history and recovery
resource "aws_s3_bucket_versioning" "terraform_state_versioning" {
  bucket = aws_s3_bucket.terraform_state.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Enable server-side encryption for the S3 bucket
resource "aws_s3_bucket_server_side_encryption_configuration" "terraform_state_encryption" {
  bucket = aws_s3_bucket.terraform_state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# DynamoDB Table for Terraform state locking
resource "aws_dynamodb_table" "terraform_state_lock" {
  name           = "${var.project_name}-tf-lock"
  billing_mode   = "PAY_PER_REQUEST"
  hash_key       = "LockID" # This attribute name is required by Terraform

  attribute {
    name = "LockID"
    type = "S"
  }

  tags = {
    Name        = "${var.project_name}-terraform-state-lock-table"
    Environment = var.environment
    ManagedBy   = "Terraform"
  }
}
