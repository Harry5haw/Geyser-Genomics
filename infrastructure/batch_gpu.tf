# infrastructure/batch_gpu.tf

# This IAM role and instance profile will be attached to the EC2 instances
# that AWS Batch launches. It gives them the necessary permissions to run the
# ECS agent, which communicates with the Batch and ECS services.
resource "aws_iam_role" "geyser_ec2_instance_role" {
  name = "${var.project_name}-ec2-instance-role-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Action = "sts:AssumeRole",
        Effect = "Allow",
        Principal = {
          Service = "ec2.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Project     = var.project_name
    Environment = var.environment
  }
}

resource "aws_iam_role_policy_attachment" "geyser_ec2_instance_role_attachment" {
  role       = aws_iam_role.geyser_ec2_instance_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role"
}

resource "aws_iam_instance_profile" "geyser_ec2_instance_profile" {
  name = "${var.project_name}-ec2-instance-profile-${var.environment}"
  role = aws_iam_role.geyser_ec2_instance_role.name
}

# A dedicated security group for the GPU compute instances.
# Allows all outbound traffic so instances can pull from ECR and communicate with AWS APIs.
resource "aws_security_group" "batch_gpu_compute" {
  name        = "${var.project_name}-gpu-compute-sg-${var.environment}"
  description = "Security group for Geyser GPU AWS Batch compute environment"
  vpc_id      = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name        = "${var.project_name}-gpu-compute-sg-${var.environment}"
    Project     = var.project_name
    Environment = var.environment
  }
}

# The managed EC2 compute environment for running GPU jobs.
resource "aws_batch_compute_environment" "gpu_env" {
  name         = "${var.project_name}-gpu-env-${var.environment}" # provider 6: was compute_environment_name
  type         = "MANAGED"
  state        = "ENABLED"
  service_role = aws_iam_role.geyser_batch_service_role.arn

  compute_resources {
    type                = "EC2"
    allocation_strategy = "BEST_FIT_PROGRESSIVE"

    instance_role = aws_iam_instance_profile.geyser_ec2_instance_profile.arn

    # 24 GB-class GPUs only (G2): call_variants at its default batch size needs more than 10 GB.
    instance_type = var.gpu_instance_types

    # Scales to zero between runs. Spend-rate cap (#73): with the default 4, at most one
    # g6.xlarge/g5.xlarge runs at a time. Changes in place.
    min_vcpus = 0
    max_vcpus = var.gpu_max_vcpus

    security_group_ids = [
      aws_security_group.batch_gpu_compute.id
    ]

    # The public subnet (map_public_ip_on_launch), like the Fargate jobs: the private subnet has no
    # route out with the NAT gateway off, so instances there could not pull from ECR or reach S3 (#43).
    subnets = [aws_subnet.public.id]

    # ECS-optimised AL2 AMI with the NVIDIA driver, and a root volume big enough for the image.
    ec2_configuration {
      image_type = "ECS_AL2_NVIDIA"
    }
    launch_template {
      launch_template_id = aws_launch_template.gpu.id
      version            = aws_launch_template.gpu.latest_version
    }
  }

  tags = {
    Project     = var.project_name
    Environment = var.environment
  }
}




# Root volume for the GPU instances: the default 30 GB can't hold the unpacked DeepVariant image.
resource "aws_launch_template" "gpu" {
  name = "${var.project_name}-gpu-${var.environment}"

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = var.gpu_disk_gb
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  metadata_options {
    http_tokens                 = "required" # IMDSv2 only
    http_put_response_hop_limit = 2          # containers on the instance
  }

  tags = {
    Project     = var.project_name
    Environment = var.environment
  }
}

# The job queue dedicated to GPU tasks.
resource "aws_batch_job_queue" "gpu_queue" {
  name     = "${var.project_name}-gpu-queue-${var.environment}" # Note: Job queue names cannot have underscores
  state    = "ENABLED"
  priority = 10 # Higher priority than the Fargate queue (1)

  # FIXED: Using the modern 'compute_environment_order' block to match your existing style.
  compute_environment_order {
    order               = 1
    compute_environment = aws_batch_compute_environment.gpu_env.arn
  }

  tags = {
    Project     = var.project_name
    Environment = var.environment
  }
}
