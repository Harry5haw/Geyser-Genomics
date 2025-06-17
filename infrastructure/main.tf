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
