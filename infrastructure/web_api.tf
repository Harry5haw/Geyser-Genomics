# infrastructure/web_api.tf: the hosted web UI's API (lambda/web_api/index.py), in the main stack.
#
# The site itself (a private S3 bucket behind CloudFront, with a response-headers policy) is in its own stack,
# infrastructure/web/, which only the user plans and applies: CloudFront is outside geyser-claude's
# permissions boundary, which can't even read it, so in this state it would break every Claude plan at refresh.
# Everything here is regional and inside the boundary. Two steps:
#   1. this stack with the defaults (no web_origin, publishing off): the API exists, with no browser origin
#      allowed yet;
#   2. after the user applies infrastructure/web/: web_origin, web_site_bucket, web_site_distribution_id and
#      publish_enabled = true in tfvars, then plan again (CORS on the data lake, the publish policy, the
#      function URL's CORS and the state machine's switch).
#
# The upload token is an SSM SecureString the user puts in (like the AlphaGenome key): referenced by name only,
# read by the function at cold start.

locals {
  # The state machine's ARN from its name, not the resource: the state machine depends on the Publish job definition,
  # which carries this API's URL, so referencing the resource here would be a dependency cycle.
  pipeline_state_machine_arn     = "arn:aws:states:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:stateMachine:${local.pipeline_state_machine_name}"
  web_upload_token_parameter_arn = "arn:aws:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter/${trimprefix(var.web_upload_token_parameter, "/")}"
  web_origins                    = var.web_origin == "" ? [] : [var.web_origin]
  # The prefixes of the output files the API may presign a GET for (PUBLIC_FILES and TOKEN_FILES in the function;
  # tests/test_web_api.py checks this list).
  web_download_prefixes = ["qc_reports", "alignments", "variants", "variants_deepvariant", "variants_pangenome", "stats"]
}

data "archive_file" "geyser_web_api_zip" {
  type        = "zip"
  source_file = "${path.module}/../lambda/web_api/index.py"
  output_path = "${path.module}/../.terraform/lambda_zips/geyser_web_api.zip"
}

resource "aws_cloudwatch_log_group" "geyser_web_api" {
  name              = "/aws/lambda/${var.project_name}-web-api-${var.environment}"
  retention_in_days = var.log_retention_days
  tags              = { Name = "${var.project_name}-web-api-logs", Environment = var.environment, ManagedBy = "Terraform" }
}

resource "aws_iam_role" "geyser_web_api" {
  name = "${var.project_name}-web-api-role-${var.environment}"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17",
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" } }]
  })
  tags = { Name = "${var.project_name}-web-api-role", Environment = var.environment, ManagedBy = "Terraform" }
}

resource "aws_iam_role_policy_attachment" "geyser_web_api_logs" {
  role       = aws_iam_role.geyser_web_api.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Least privilege: presigned URLs act with the function's own permissions, so these are exactly what a
# browser can do through it.
resource "aws_iam_role_policy" "geyser_web_api" {
  name = "${var.project_name}-web-api-${var.environment}"
  role = aws_iam_role.geyser_web_api.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        # Multipart uploads of raw reads only (Create/UploadPart/Complete are s3:PutObject); the S3 trigger
        # starts the run when an upload completes.
        Sid      = "UploadRawReads"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:AbortMultipartUpload"]
        Resource = "${aws_s3_bucket.data_lake.arn}/raw_reads/*"
      },
      {
        # Downloads of a sample's outputs (HeadObject is s3:GetObject too): the function's PUBLIC_FILES and, with
        # the token, TOKEN_FILES (alignments/). Never raw_reads/ or reference/; of runs/, only the stored records.
        Sid    = "DownloadOutputs"
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = concat(
          [for prefix in local.web_download_prefixes : "${aws_s3_bucket.data_lake.arn}/${prefix}/*"],
          ["${aws_s3_bucket.data_lake.arn}/runs/*.provenance.json"]
        )
      },
      {
        # A missing output is a 404 (not a 403) for HeadObject only with s3:ListBucket.
        Sid      = "MissingOutputIs404"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.data_lake.arn
      },
      {
        Sid      = "RunStatus"
        Effect   = "Allow"
        Action   = ["states:ListExecutions"]
        Resource = local.pipeline_state_machine_arn
      },
      {
        # The upload token, one parameter (default key aws/ssm: no kms:Decrypt needed).
        Sid      = "ReadUploadToken"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = local.web_upload_token_parameter_arn
      }
    ]
  })
}

resource "aws_lambda_function" "geyser_web_api" {
  function_name    = "${var.project_name}-web-api-${var.environment}"
  role             = aws_iam_role.geyser_web_api.arn
  filename         = data.archive_file.geyser_web_api_zip.output_path
  source_code_hash = data.archive_file.geyser_web_api_zip.output_base64sha256
  handler          = "index.handler"
  runtime          = "python3.11" # as the trigger; boto3 comes with the runtime
  timeout          = 15
  memory_size      = 256
  # Rate limiting: at most this many requests at once; more get 429 (TooManyRequests) from the function URL.
  # The account's limit is 1000 (4 Nov 2026), so reserving a few leaves the rest of the account untouched.
  reserved_concurrent_executions = var.web_api_reserved_concurrency

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.geyser_web_api.name
  }

  environment {
    variables = {
      BUCKET_NAME            = aws_s3_bucket.data_lake.bucket
      STATE_MACHINE_ARN      = local.pipeline_state_machine_arn
      UPLOAD_TOKEN_PARAMETER = var.web_upload_token_parameter
      MAX_UPLOAD_BYTES       = tostring(var.web_max_upload_gb * 1024 * 1024 * 1024)
    }
  }

  tags = { Name = "${var.project_name}-web-api", Environment = var.environment, ManagedBy = "Terraform" }
}

# Public HTTPS endpoint, auth NONE: uploads check the token in code; status and downloads are the public
# showcase. Browsers may call it only from the site's origin (CORS), once web_origin is set.
resource "aws_lambda_function_url" "geyser_web_api" {
  function_name      = aws_lambda_function.geyser_web_api.function_name
  authorization_type = "NONE"

  dynamic "cors" {
    for_each = var.web_origin == "" ? [] : [var.web_origin]
    content {
      allow_origins  = [cors.value]
      allow_methods  = ["GET", "POST"]
      allow_headers  = ["authorization", "content-type"]
      expose_headers = ["location"]
      max_age        = 3600
    }
  }
}

# A function URL with auth NONE needs both permissions (since Oct 2025): InvokeFunctionUrl for the URL, and
# InvokeFunction limited to calls that come through the URL.
resource "aws_lambda_permission" "geyser_web_api_url" {
  statement_id           = "FunctionUrlPublic"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.geyser_web_api.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "geyser_web_api_invoke_via_url" {
  statement_id             = "FunctionUrlInvoke"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.geyser_web_api.function_name
  principal                = "*"
  invoked_via_function_url = true
}

# The browser PUTs upload parts straight to the data lake and must read each part's ETag to complete the
# upload. A separate CORS resource: the bucket itself doesn't change. Only once web_origin is set.
resource "aws_s3_bucket_cors_configuration" "data_lake_web_uploads" {
  count  = var.web_origin == "" ? 0 : 1
  bucket = aws_s3_bucket.data_lake.id

  cors_rule {
    allowed_methods = ["PUT"]
    allowed_origins = local.web_origins
    allowed_headers = ["content-type"]
    expose_headers  = ["ETag"]
    max_age_seconds = 3600
  }
}

# The Publish job has its own image, job definition and role: publish.tf.

output "web_api_url" {
  description = "The web API's function URL (the site's API origin, for infrastructure/web/'s api_origin)."
  value       = aws_lambda_function_url.geyser_web_api.function_url
}
