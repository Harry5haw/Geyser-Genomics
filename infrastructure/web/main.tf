# infrastructure/web/main.tf
#
# The hosted web UI's static site (W3 of the web plan): a private S3 bucket behind CloudFront, in its own small
# stack and state. Only the user plans and applies it:
#   terraform -chdir=infrastructure/web init -backend-config=backend-web.hcl
#   AWS_PROFILE=default terraform -chdir=infrastructure/web plan -var-file=../terraform.tfvars -out=web.tfplan
#
# Why separate from infrastructure/: CloudFront is a global service outside the geyser-claude permissions
# boundary, which can't even read it. In the dev state, every geyser-claude plan would fail refreshing these
# resources (the same reason as infrastructure/account/). The API, the data lake's CORS rule and the Publish
# state are in the main stack (web_api.tf); they take this stack's outputs as variables.
#
# Cost: CloudFront's always-free tier covers 1 TB out and 10 M requests a month; the bucket holds a few MB of
# HTML. Nothing here bills by the hour: $0 when nobody visits.

terraform {
  required_version = ">= 1.13.3, < 2.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.69.0" # the same pin as infrastructure/providers.tf
    }
  }

  # Same state bucket and lock table as the main stack, its own key (backend-web.hcl).
  backend "s3" {
    bucket         = "geyser-tfstate-488543428961-eu-west-2"
    region         = "eu-west-2"
    dynamodb_table = "geyser-tf-lock"
    encrypt        = true
  }
}

provider "aws" {
  region = "eu-west-2" # the bucket's region; CloudFront resources are global whatever the provider region

  default_tags {
    tags = { Project = var.project_name, Environment = var.environment, ManagedBy = "Terraform", Stack = "web" }
  }
}

data "aws_caller_identity" "current" {}

# AWS's managed cache policy: caches by path only, honours the objects' Cache-Control (the publisher sets a
# year for hashed assets and a short TTL for HTML), compresses with Brotli/gzip.
data "aws_cloudfront_cache_policy" "caching_optimized" {
  name = "Managed-CachingOptimized"
}

locals {
  site_bucket_name = "${var.project_name}-site-${var.environment}-${data.aws_caller_identity.current.account_id}"
  # Content-Security-Policy: exactly the policy the published pages were tested with (T3's W2: Lighthouse 100, no
  # console errors), plus the API and, for the upload page's presigned PUTs, the data lake in connect-src.
  # Everything else is 'none' (default-src): nothing inline except the hash-allowed speculation rules and inlined
  # CSS (csp-hashes.json in the site root: script_hashes, style_hashes); workers (the 3D view) from 'self' only;
  # no forms (the upload page uses fetch).
  csp = join("; ", [
    "default-src 'none'",
    "script-src ${join(" ", concat(["'self'"], [for h in var.script_hashes : "'${h}'"]))}",
    "style-src ${join(" ", concat(["'self'"], [for h in var.style_hashes : "'${h}'"]))}",
    "img-src 'self' data:",
    "connect-src ${join(" ", compact(["'self'", var.api_origin, var.data_lake_origin]))}",
    "worker-src 'self'",
    "manifest-src 'self'",
    "base-uri 'none'",
    "form-action 'none'",
    "frame-ancestors 'none'",
  ])
}

# --- the site bucket: private, reachable only through CloudFront (OAC) ------------------------------------------

resource "aws_s3_bucket" "site" {
  bucket = local.site_bucket_name
  tags   = { Name = "${var.project_name}-site" }
}

resource "aws_s3_bucket_public_access_block" "site" {
  bucket                  = aws_s3_bucket.site.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "site" {
  bucket = aws_s3_bucket.site.id
  rule {
    object_ownership = "BucketOwnerEnforced" # no ACLs at all
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "site" {
  bucket = aws_s3_bucket.site.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256" # SSE-S3: OAC reads it with no KMS key policy to manage
    }
  }
}

resource "aws_cloudfront_origin_access_control" "site" {
  name                              = "${var.project_name}-site-${var.environment}"
  description                       = "CloudFront reads the private site bucket (SigV4)."
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# Only this distribution may read the bucket, and only over TLS.
resource "aws_s3_bucket_policy" "site" {
  bucket = aws_s3_bucket.site.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Sid       = "CloudFrontReadsTheSite"
        Effect    = "Allow"
        Principal = { Service = "cloudfront.amazonaws.com" }
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.site.arn}/*"
        Condition = { StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.site.arn } }
      },
      {
        Sid       = "TlsOnly"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.site.arn, "${aws_s3_bucket.site.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      }
    ]
  })
  depends_on = [aws_s3_bucket_public_access_block.site]
}

# --- security headers on every response ---------------------------------------------------------------------------

resource "aws_cloudfront_response_headers_policy" "site" {
  name    = "${var.project_name}-site-security-${var.environment}"
  comment = "HSTS, a strict CSP (hash slots for the inline speculation rules and critical CSS), nosniff, no referrer, no framing."

  security_headers_config {
    strict_transport_security {
      access_control_max_age_sec = 63072000 # two years
      include_subdomains         = true
      preload                    = false # *.cloudfront.net isn't ours to preload
      override                   = true
    }
    content_security_policy {
      content_security_policy = local.csp
      override                = true
    }
    content_type_options {
      override = true
    }
    referrer_policy {
      referrer_policy = "no-referrer" # presigned URLs and sample IDs never leak to another site
      override        = true
    }
    frame_options {
      frame_option = "DENY"
      override     = true
    }
  }
}

# --- clean URLs: /samples/S1/ -> /samples/S1/index.html (S3 has no directory index through OAC) -----------------

resource "aws_cloudfront_function" "directory_index" {
  name    = "${var.project_name}-site-index-${var.environment}"
  runtime = "cloudfront-js-2.0"
  comment = "Append index.html to directory paths."
  publish = true
  code    = <<-JS
    function handler(event) {
      var request = event.request;
      if (request.uri.endsWith("/")) {
        request.uri += "index.html";
      } else if (request.uri.split("/").pop().indexOf(".") === -1) {
        request.uri += "/index.html";
      }
      return request;
    }
  JS
}

# --- the distribution ------------------------------------------------------------------------------------------------

resource "aws_cloudfront_distribution" "site" {
  enabled             = true
  comment             = "Geyser Genomics: the hosted web UI (static, pre-rendered)"
  default_root_object = "index.html"
  is_ipv6_enabled     = true
  http_version        = "http2and3"
  price_class         = var.price_class # PriceClass_100: Europe and North America edges, London included

  origin {
    origin_id                = "site"
    domain_name              = aws_s3_bucket.site.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.site.id
  }

  default_cache_behavior {
    target_origin_id           = "site"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = data.aws_cloudfront_cache_policy.caching_optimized.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.site.id

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.directory_index.arn
    }
  }

  # Through OAC a missing object is a 403 (no s3:ListBucket): show the site's 404 page as a 404.
  custom_error_response {
    error_code            = 403
    response_code         = 404
    response_page_path    = "/404.html"
    error_caching_min_ttl = 60
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  # The default *.cloudfront.net certificate: no ACM certificate to manage (a custom domain needs one in
  # us-east-1 later).
  viewer_certificate {
    cloudfront_default_certificate = true
  }

  tags = { Name = "${var.project_name}-site" }
}
