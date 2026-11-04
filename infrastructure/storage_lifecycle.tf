# infrastructure/storage_lifecycle.tf
#
# Expire what the data lake can do without (#62). Retention is set by break-even (the plan's "lifecycle
# rule by break-even"): keep an object while storing it costs less than recomputing it once,
#   keep_days = recompute_cost / (size_GiB x S3 price per GiB-day)
# (scripts/lifecycle_break_even.py computes it from real runs). raw_reads/ and reference/ (can't be
# recomputed: they are the inputs) and the small final outputs (qc_reports/, variants/, stats/, runs/:
# about 6 MB per run, $0.0002 a month) are never expired by this file.
#   alignments/    CRAM + .crai (or an old BAM): recomputable from the raw reads by the align job.
#                  Break-even on the six real chr20 runs (Nov 2026): 178-248 days at on-demand Fargate
#                  prices (53-74 on Spot), the same for a whole genome (both scale with depth x length),
#                  so var.alignment_expiry_days = 180 (0 turns the rule off). An expired CRAM only costs
#                  a re-align: variants and DeepVariant read it, reconcile.py still counts the sample
#                  complete while its VCF is there.
#   intermediate/  scatter-gather parts (app/tasks.py INTERMEDIATE_PREFIX; tests/test_consistency.py
#                  checks the two agree). Break-even doesn't apply: nothing reads a part after the merge
#                  (the VCF holds the same records; a rerun writes its own), so it has no reuse value, and
#                  7 days cost ~$0.00003 per run. Kept 7 days as a debugging window only. The job role
#                  can't delete objects, so this is what clears a run's parts, earlier runs' included.
#   decompressed/  uncompressed FASTQs from the old four-stage pipeline; nothing writes here since the
#                  three-stage change (#84), so this only clears what is left.
#   raw_reads/     never expired. Large objects only (the user's decision, 4 Nov 2026) are tiered (v1.3): each
#                  object of at least large_input_gb (5,000,000,000 bytes, the large route's threshold) moves to
#                  Glacier Instant Retrieval after raw_reads_tier_days (30), still readable at once by a re-run. A 30x
#                  genome's two ~26 GB mates: $1.27 a month in Standard, $0.27 in Glacier IR. Smaller uploads (chr20
#                  samples, smoke tests) stay in Standard: Glacier IR's per-object minimums and 90-day minimum
#                  aren't worth it for them. The filter is per object, so a pair of mates each under 5 GB stays in
#                  Standard even when together they took the large route.
#   intermediate/chunks/, intermediate/aligned/
#                  the large route's chunks (~130 GB per genome): expire after genome_chunk_expiry_days (3),
#                  sooner than the rest of intermediate/ (S3 applies the earliest of overlapping expirations).
# Plus: unfinished multipart uploads anywhere in the bucket are aborted, so a failed large upload
# doesn't keep billing for its stored parts (they never become an object, so nothing visible is lost).
# The bucket is not versioned: an expired object is gone, with no noncurrent version left behind.

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

  # Last, so the plan shows one added rule: the provider compares rules by position.
  rule {
    id     = "expire-alignments"
    status = var.alignment_expiry_days > 0 ? "Enabled" : "Disabled"
    filter {
      prefix = "alignments/"
    }
    expiration {
      days = max(var.alignment_expiry_days, 1) # S3 needs a day count even on a disabled rule
    }
  }

  # v1.3, appended (rules compare by position, so the plan shows these three as added, the others unchanged).
  rule {
    id     = "tier-raw-reads"
    status = var.raw_reads_tier_days > 0 ? "Enabled" : "Disabled"
    filter {
      and {
        prefix = "raw_reads/"
        # S3 matches sizes strictly greater than this, so one byte under the threshold makes it "at least
        # large_input_gb": the same objects the route calls large (step_functions.tf, local.large_input_bytes).
        object_size_greater_than = local.large_input_bytes - 1
      }
    }
    transition {
      days          = max(var.raw_reads_tier_days, 1)
      storage_class = "GLACIER_IR"
    }
  }

  rule {
    id     = "expire-genome-chunks"
    status = "Enabled"
    filter {
      prefix = "intermediate/chunks/"
    }
    expiration {
      days = var.genome_chunk_expiry_days
    }
  }

  rule {
    id     = "expire-genome-aligned-chunks"
    status = "Enabled"
    filter {
      prefix = "intermediate/aligned/"
    }
    expiration {
      days = var.genome_chunk_expiry_days
    }
  }
}
