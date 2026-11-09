# infrastructure/logs_insights.tf
#
# Saved CloudWatch Logs Insights queries (observability plan O5): the questions asked of the logs after a run,
# saved under CloudWatch > Logs Insights > Queries in the folder "<project>-<environment>". Choose the time range
# in the console; a saved query stores none. Free (Logs Insights bills only the data each query scans).
#
# Two sources:
# - the Batch job logs (resilience.tf): the app's stage lines, e.g.
#   "--- 'CallVariantsShard' for 'S1': Success in 42.10s ---" (app/tasks.py timed_stage);
# - the Batch job events at RUNNING (batch_wait.tf): job name (<Stage>-[<index>-]<execution name>), queue,
#   createdAt/startedAt and the earlier attempts' statusReason. Every job, any image, including those
#   killed before the app could log.

# Every query_string drops "\r": a CRLF working copy (core.autocrlf, or a file written on Windows) would
# otherwise deploy carriage returns and show 5 in-place updates in every plan from an LF copy (#258).

locals {
  query_folder = "${var.project_name}-${var.environment}"
}

resource "aws_cloudwatch_query_definition" "run_timeline" {
  # Replace RUN-NAME with the execution name (or any part of it, e.g. the sample) before running.
  name            = "${local.query_folder}/One run's timeline (replace RUN-NAME)"
  log_group_names = [aws_cloudwatch_log_group.batch_job_events.name]
  query_string = replace(<<-EOT
    fields detail.jobName as job, (detail.startedAt - detail.createdAt) / 1000 as wait_s, detail.attempts.0.statusReason as earlier_attempt
    | filter detail.jobName like "RUN-NAME"
    | parse detail.jobQueue "job-queue/*" as queue
    | fields fromMillis(detail.startedAt) as started
    | sort detail.startedAt asc
    | display started, job, wait_s, queue, earlier_attempt
    | limit 1000
  EOT
  , "\r", "")
}

resource "aws_cloudwatch_query_definition" "failures_by_stage" {
  # Stages that failed inside the app. A job killed from outside (timeout, Spot, out of memory) logs no
  # Failure line: see "Spot and host interruptions" and the Step Functions console.
  name            = "${local.query_folder}/Failures by stage (choose last 7 days)"
  log_group_names = [aws_cloudwatch_log_group.geyser_batch_jobs.name]
  query_string = replace(<<-EOT
    fields @timestamp, @message
    | filter @message like /^--- '[A-Za-z]+' for '.+': Failure in /
    | parse @message /^--- '(?<stage>[A-Za-z]+)' for '(?<sample>.+)': Failure in (?<seconds>[0-9.]+)s ---/
    | stats count(*) as failures, latest(sample) as last_sample, max(@timestamp) as last_failure by stage
    | sort failures desc
  EOT
  , "\r", "")
}

resource "aws_cloudwatch_query_definition" "spot_interruptions" {
  # A job started again after its instance went away: EC2 Spot reclaims ("Host EC2 (instance ...) terminated")
  # and Fargate Spot interruptions. Retried by the job definitions' retryStrategy (resilience.tf).
  name            = "${local.query_folder}/Spot and host interruptions"
  log_group_names = [aws_cloudwatch_log_group.batch_job_events.name]
  query_string = replace(<<-EOT
    fields @timestamp, detail.jobName as job, concat(detail.attempts.0.statusReason, " | ", detail.attempts.1.statusReason, " | ", detail.attempts.2.statusReason) as reasons
    | filter reasons like /Host EC2 \(instance|Spot Task was interrupted|SpotInterruption/
    | parse detail.jobQueue "job-queue/*" as queue
    | sort @timestamp desc
    | display @timestamp, job, queue, reasons
    | limit 200
  EOT
  , "\r", "")
}

resource "aws_cloudwatch_query_definition" "deepvariant_fallbacks" {
  # A failed DeepVariant falls back to bcftools (state machine: DeepVariant's Catch -> Prepare_Plan_Command),
  # so a run with both DeepVariant and bcftools calling jobs is a fallback. Jobs are grouped by the execution
  # name's "<UTC time>-<suffix>" tail: some job names carry the sample twice, some a part index instead.
  name            = "${local.query_folder}/DeepVariant fallbacks to bcftools"
  log_group_names = [aws_cloudwatch_log_group.batch_job_events.name]
  query_string = replace(<<-EOT
    fields @timestamp, detail.jobName as job
    | parse job /^(?<stage>DeepVariant-Examples|DeepVariantCPU|DeepVariant|CallVariantsShard|CallVariants|PlanRegions)-.*-(?<run_id>[0-9]{8}T[0-9]{6}Z-[A-Za-z0-9]+)$/
    | filter ispresent(run_id)
    | fields strcontains(stage, "DeepVariant") as is_deepvariant, strcontains(stage, "CallVariants") as is_bcftools
    | stats sum(is_deepvariant) as deepvariant_jobs, sum(is_bcftools) as bcftools_jobs, min(@timestamp) as first_job, latest(job) as last_job by run_id
    | filter deepvariant_jobs > 0 and bcftools_jobs > 0
    | sort first_job desc
  EOT
  , "\r", "")
}

resource "aws_cloudwatch_query_definition" "slowest_calling_shards" {
  # bcftools calling shards (scatter-gather, #108). Open the log stream for the shard's region (its "$ " lines).
  name            = "${local.query_folder}/Slowest calling shards"
  log_group_names = [aws_cloudwatch_log_group.geyser_batch_jobs.name]
  query_string = replace(<<-EOT
    fields @timestamp, @logStream
    | filter @message like /^--- 'CallVariantsShard' for '.+': (Success|Failure) in /
    | parse @message /^--- 'CallVariantsShard' for '(?<sample>.+)': (?<status>Success|Failure) in (?<seconds>[0-9.]+)s ---/
    | fields seconds / 60 as minutes
    | sort minutes desc
    | display @timestamp, sample, status, minutes, @logStream
    | limit 50
  EOT
  , "\r", "")
}
