# infrastructure/storage_lifecycle.tf
#
# Expire the data lake's disposable intermediates (#62). Only these prefixes, decided with the user on
# 2 Nov 2026; raw_reads/, reference/ and every output prefix (qc_reports/, alignments/, variants/,
# stats/, runs/) are never expired by this file.
#   intermediate/  scatter-gather parts (app/tasks.py INTERMEDIATE_PREFIX; tests/test_consistency.py
#                  checks the two agree). The job role can't delete objects, so this is what clears a
#                  run's parts, including those an earlier run left behind.
#   decompressed/  uncompressed FASTQs from the old four-stage pipeline; nothing writes here since the
#                  three-stage change (#84), so this only clears what is left.
# Plus: unfinished multipart uploads anywhere in the bucket are aborted, so a failed large upload
# doesn't keep billing for its stored parts (they never become an object, so nothing visible is lost).

resource "aws_s3_bucket_lifecycle_configuration" "data_lake" {
  bucket = aws_s3_bucket.data_lake.id

  rule {
    id     = "expire-intermediate"
    status = "Enabled"
    filter {
      prefix = "intermediate/"
    }
    expiration {
      days = var.intermediate_expiry_days
    }
  }

  rule {
    id     = "expire-decompressed"
    status = "Enabled"
    filter {
      prefix = "decompressed/"
    }
    expiration {
      days = var.intermediate_expiry_days
    }
  }

  rule {
    id     = "abort-incomplete-multipart-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = var.intermediate_expiry_days
    }
  }
}
