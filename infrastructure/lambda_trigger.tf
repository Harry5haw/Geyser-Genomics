# infrastructure/lambda_trigger.tf

# --- Data source to package the Lambda function code ---
# This creates a zip archive from our Python script.
# Terraform will automatically re-package this if the source file changes.
data "archive_file" "sfn_trigger_lambda_zip" {
  type        = "zip"
  source_file = "${path.module}/../lambda/trigger/index.py"
  output_path = "${path.module}/../.terraform/lambda_zips/sfn_trigger.zip"
}

# --- IAM Role and Policy for the Lambda function ---
# This role allows the Lambda function to be invoked and to write logs.
resource "aws_iam_role" "sfn_trigger_lambda_role" {
  name = "${var.project_name}-sfn-trigger-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Action = "sts:AssumeRole",
        Effect = "Allow",
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Project = var.project_name
  }
}

# Attach the basic Lambda execution policy for CloudWatch Logs
resource "aws_iam_role_policy_attachment" "sfn_trigger_lambda_basic_execution" {
  role       = aws_iam_role.sfn_trigger_lambda_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# --- Custom IAM Policy to allow starting the Step Function ---
# This policy grants the specific, least-privilege permission required.
resource "aws_iam_policy" "sfn_trigger_lambda_start_execution_policy" {
  name        = "${var.project_name}-sfn-start-execution-policy"
  description = "Allows Lambda to start the main Step Function execution"

  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Action   = "states:StartExecution",
        Effect   = "Allow",
        Resource = aws_sfn_state_machine.genomics_pipeline_state_machine.id
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "sfn_trigger_lambda_start_execution" {
  role       = aws_iam_role.sfn_trigger_lambda_role.name
  policy_arn = aws_iam_policy.sfn_trigger_lambda_start_execution_policy.arn
}


# --- Lambda Function Resource ---
resource "aws_lambda_function" "sfn_trigger" {
  function_name = "${var.project_name}-sfn-trigger"
  role          = aws_iam_role.sfn_trigger_lambda_role.arn

  filename         = data.archive_file.sfn_trigger_lambda_zip.output_path
  source_code_hash = data.archive_file.sfn_trigger_lambda_zip.output_base64sha256

  handler = "index.handler"
  runtime = "python3.11"
  timeout = 30

  environment {
    variables = {
      STATE_MACHINE_ARN = aws_sfn_state_machine.genomics_pipeline_state_machine.id
    }
  }

  tags = {
    Project = var.project_name
  }
}

# --- S3 Bucket Notification Trigger ---
# This resource configures the S3 bucket to invoke our Lambda on object creation.
resource "aws_s3_bucket_notification" "data_lake_upload_trigger" {
  bucket = aws_s3_bucket.data_lake.id

  lambda_function {
    lambda_function_arn = aws_lambda_function.sfn_trigger.arn
    events              = ["s3:ObjectCreated:*"]

    # CORRECTED: Filter prefix now matches the application code's expectation.
    filter_prefix = "raw_reads/"
    filter_suffix = ".gz"
  }

  depends_on = [aws_lambda_permission.allow_s3_to_invoke_lambda]
}


# --- Lambda Permission ---
# This grants the S3 service principal permission to invoke the Lambda function.
resource "aws_lambda_permission" "allow_s3_to_invoke_lambda" {
  statement_id  = "AllowS3ToInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.sfn_trigger.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = aws_s3_bucket.data_lake.arn
}
