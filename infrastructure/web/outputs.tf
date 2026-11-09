# What the main stack's web variables take (infrastructure/web_api.tf).

output "site_origin" {
  description = "The site's origin: the main stack's web_origin."
  value       = "https://${aws_cloudfront_distribution.site.domain_name}"
}

output "site_bucket" {
  description = "The site bucket: the main stack's web_site_bucket."
  value       = aws_s3_bucket.site.bucket
}

output "distribution_id" {
  description = "The distribution: the main stack's web_site_distribution_id."
  value       = aws_cloudfront_distribution.site.id
}
