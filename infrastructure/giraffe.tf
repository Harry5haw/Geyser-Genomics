# infrastructure/giraffe.tf: vg giraffe as the align stage's aligner in the cloud (#146).
# Design and sizing: docs/design/giraffe-align.md (whole genome) and the PR for this file (chr20).
#
# The giraffe image (app-giraffe/Dockerfile: geyser-app plus vg and kmc, ~106 MB compressed) has its own
# repository and job definition, on the same Fargate queue as every other stage. A run picks it with
# aligner = "giraffe" in its execution input (state_machine.asl.json, Choose_Aligner); without it, align
# runs bwa-mem2 in geyser-app as before. The job reads pangenome/<giraffe_graph> and the .hapl beside it
# (the key the pangenome-aware DeepVariant caller uses too, so one upload serves both).
#
# Size: the align stage's (stage_resources["align"], 4 vCPU / 8 GiB by default), checked below against
# kmc's memory. HG003 chr20 35x locally: whole stage 564 s at 16 threads, peak 6.0 GiB with kmc -m8
# (kmc alone 7.4 GiB at -m8; giraffe 1.5 GiB; sort/markdup 2.3 GiB), work space ~8 GB, inside Fargate's
# free 20 GiB. A whole genome does not fit Fargate's 200 GiB disk as the stage stands (see the PR).

resource "aws_ecr_repository" "geyser_giraffe" {
  name         = "${var.project_name}-giraffe"
  force_delete = true
  tags         = { Name = "${var.project_name}-giraffe-ecr-repo" }

  # Basic scanning (free): known CVEs of every pushed image, in the ECR console (#64).
  image_scanning_configuration {
    scan_on_push = true
  }
}

# Each image is ~106 MB (no layers shared across repositories): five is ~0.5 GB, about $0.05/month.
resource "aws_ecr_lifecycle_policy" "geyser_giraffe" {
  repository = aws_ecr_repository.geyser_giraffe.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 5 most recent giraffe images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 }
      action       = { type = "expire" }
    }]
  })
}

resource "aws_batch_job_definition" "geyser_giraffe_job_def" {
  # The job's tags (Stage, per state) and the default tags reach the task: Cost Explorer by stage (#64).
  propagate_tags        = true
  name                  = "${var.project_name}-giraffe-job"
  type                  = "container"
  platform_capabilities = ["FARGATE"]
  container_properties = jsonencode(merge({
    image            = "${aws_ecr_repository.geyser_giraffe.repository_url}:${var.giraffe_image_version}"
    executionRoleArn = aws_iam_role.geyser_batch_execution_role.arn
    jobRoleArn       = aws_iam_role.geyser_batch_task_role.arn
    fargatePlatformConfiguration = {
      platformVersion = "LATEST"
    }
    # Public subnet, no NAT: as the app job (main.tf).
    networkConfiguration = {
      assignPublicIp = "ENABLED"
    }
    # Defaults only: the state machine sends the align stage's size.
    resourceRequirements = [
      { type = "VCPU", value = tostring(var.stage_resources["align"].vcpu) },
      { type = "MEMORY", value = tostring(var.stage_resources["align"].memory_mb) }
    ]
    environment = concat([
      { name = "BUCKET_NAME", value = aws_s3_bucket.data_lake.bucket },
      { name = "APP_VERSION", value = var.giraffe_image_version },
      # The image sets ALIGNER=giraffe already; said here too, so the job definition reads as what it runs.
      { name = "ALIGNER", value = "giraffe" },
      # kmc -m: a cap, not a reservation. 4 (the app default) keeps kmc + Python well inside 8 GiB.
      { name = "KMC_MEMORY_GB", value = tostring(var.giraffe_kmc_memory_gb) },
      ],
      # Empty: the app's default, <reference stem>.gbz (pangenome/chr20.gbz for chr20.fa).
      var.giraffe_graph == "" ? [] : [{ name = "GIRAFFE_GRAPH", value = var.giraffe_graph }]
    )
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.geyser_batch_jobs.name
        "awslogs-region"        = data.aws_region.current.region
        "awslogs-stream-prefix" = "giraffe"
      }
    }
    },
    # As the app job: Fargate's free 20 GiB unless job_disk_gb asks for more (billed).
    var.job_disk_gb > 20 ? { ephemeralStorage = { sizeInGiB = var.job_disk_gb } } : {}
  ))

  # Same rule as the app job (main.tf): retry infrastructure failures only, never the application's.
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

  lifecycle {
    precondition {
      # kmc's cap plus ~1 GiB for Python, samtools and the page cache it can't drop.
      condition     = (var.giraffe_kmc_memory_gb + 1) * 1024 <= var.stage_resources["align"].memory_mb
      error_message = "giraffe_kmc_memory_gb + 1 GiB must fit in the align stage's memory (stage_resources[\"align\"].memory_mb)."
    }
  }
  tags = { Name = "${var.project_name}-GiraffeJobDef" }
}
