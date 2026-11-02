"""CloudWatch dashboard and alarm definitions for Geyser Genomics (pure builders, no AWS calls).

The deploy scripts (deploy_dashboard.py, runtime_deploy_pipeline_dashboard.py, deploy_alarms.py) build
their JSON here and only touch AWS when run without --dry-run.

Metric shapes (see app/metrics.py):
  Duration      dims TaskName, SampleId, Status   (every stage run)
  FailureCount  dims TaskName, SampleId           (only when a stage fails)

Two metric schemas are supported:
  per-sample  what app/metrics.py publishes today. Each sample is its own metric, so CloudWatch cannot
              compute percentiles across runs; durations are shown as mean and max of the per-sample
              series (SEARCH).
  aggregated  the same metrics without the SampleId dimension (a proposed change, not made yet). One
              series per stage gives real p50/p95 durations.

Result quality (mapped %, duplicate %, mean depth, variant counts, Ti/Tv) is read from the Batch job logs
with Logs Insights widgets, not from custom metrics; see the "quality" section below.

Alarms are observability signals shown on the dashboard, not an email system. There is one: the trigger
Lambda failing, the only failure that leaves no trace in the pipeline's own metrics (no run starts).
Stage and run failures are already visible in the dashboard widgets. Email is optional and off by default.

No account IDs or ARNs live in this file: ARNs and the account ID are always arguments.
"""

import json
import re
from dataclasses import dataclass
from urllib.parse import quote

DEFAULT_REGION = "eu-west-2"
DEFAULT_NAMESPACE = "GeyserGenomics"  # app/metrics.py METRIC_NAMESPACE
SCHEMAS = ("per-sample", "aggregated")
GRID_WIDTH = 24
# Expression/metric ids: CloudWatch requires a lowercase first letter, then letters, digits, underscores.
_ID_RE = re.compile(r"[^a-zA-Z0-9_]")


@dataclass(frozen=True)
class Stage:
    task_name: str  # the TaskName dimension value
    records_failures: bool = True  # False for sub-steps that only ever publish Success

    @property
    def metric_id(self) -> str:
        cleaned = _ID_RE.sub("_", self.task_name).lower()
        return cleaned if cleaned[0].isalpha() else f"s_{cleaned}"


def caller_stages(prefix: str) -> tuple:
    """A DeepVariant-style caller: one timed stage plus three step metrics (Success only)."""
    return (Stage(prefix),) + tuple(
        Stage(f"{prefix}.{step}", records_failures=False)
        for step in ("MakeExamples", "CallVariants", "PostprocessVariants")
    )


# To add a stage or caller, add it here: dashboards, alarms and tests pick it up.
STAGE_GROUPS = {
    "core": (Stage("QualityControl"), Stage("Align"), Stage("CallVariants")),
    "deepvariant": caller_stages("DeepVariant"),
    "pangenome": caller_stages("PangenomeDeepVariant"),
    # Scatter-gather calling (#108): one CallVariantsShard per region; the merge is the core CallVariants.
    "shards": (Stage("CallVariantsShard"),),
    # The quality gate after variant calling (#144): its run time, and its failures (GATE_MODE=fail).
    "gate": (Stage("QualityGate"),),
}
GROUP_TITLES = {
    "core": "Core pipeline (bcftools)",
    "deepvariant": "DeepVariant",
    "pangenome": "Pangenome-aware DeepVariant",
    "shards": "Variant calling shards (per region)",
    "gate": "Quality gate",
}


def resolve_groups(names=None) -> dict:
    names = list(STAGE_GROUPS) if not names else names
    unknown = [n for n in names if n not in STAGE_GROUPS]
    if unknown:
        raise ValueError(f"Unknown stage group(s) {unknown}; choose from {sorted(STAGE_GROUPS)}")
    return {n: STAGE_GROUPS[n] for n in names}


def _check_schema(schema: str) -> None:
    if schema not in SCHEMAS:
        raise ValueError(f"Unknown schema {schema!r}; choose from {SCHEMAS}")


# ---------------------------------------------------------------------------------------------- metrics


def _search(namespace: str, dims: str, metric: str, task_name: str, stat: str, period: int, extra: str = "") -> str:
    # Name="value" is an exact match in SEARCH, so "DeepVariant" does not also match "DeepVariant.MakeExamples".
    return (
        f"SEARCH('{{{namespace},{dims}}} MetricName=\"{metric}\" TaskName=\"{task_name}\"{extra}', '{stat}', {period})"
    )


def duration_metrics(stages, schema: str, namespace: str, period: int) -> list:
    """Metric rows for a duration widget: two lines per stage (p50/p95, or mean/max for per-sample)."""
    _check_schema(schema)
    rows = []
    for stage in stages:
        if schema == "aggregated":
            for stat in ("p50", "p95"):
                rows.append(
                    [
                        namespace,
                        "Duration",
                        "TaskName",
                        stage.task_name,
                        "Status",
                        "Success",
                        {
                            "id": f"{stage.metric_id}_{stat}",
                            "stat": stat,
                            "period": period,
                            "label": f"{stage.task_name} {stat}",
                        },
                    ]
                )
        else:
            for func, stat, word in (("AVG", "Average", "mean"), ("MAX", "Maximum", "max")):
                search = _search(
                    namespace,
                    "SampleId,Status,TaskName",
                    "Duration",
                    stage.task_name,
                    stat,
                    period,
                    extra=' Status="Success"',
                )
                rows.append(
                    [
                        {
                            "expression": f"{func}({search})",
                            "id": f"{stage.metric_id}_{word}",
                            "label": f"{stage.task_name} {word}",
                        }
                    ]
                )
    return rows


def failure_metrics(stages, schema: str, namespace: str, period: int) -> list:
    """Metric rows counting FailureCount per stage (only stages that can record failures)."""
    _check_schema(schema)
    rows = []
    for stage in stages:
        if not stage.records_failures:
            continue
        if schema == "aggregated":
            rows.append(
                [
                    namespace,
                    "FailureCount",
                    "TaskName",
                    stage.task_name,
                    {"id": f"{stage.metric_id}_failures", "stat": "Sum", "period": period, "label": stage.task_name},
                ]
            )
        else:
            search = _search(namespace, "SampleId,TaskName", "FailureCount", stage.task_name, "Sum", period)
            rows.append(
                [{"expression": f"SUM({search})", "id": f"{stage.metric_id}_failures", "label": stage.task_name}]
            )
    return rows


def stage_runs_metrics(stage: Stage, schema: str, namespace: str, period: int) -> list:
    """Runs per period counted from one stage's Duration samples (fallback when no state machine ARN)."""
    _check_schema(schema)
    if schema == "aggregated":
        return [
            [
                namespace,
                "Duration",
                "TaskName",
                stage.task_name,
                "Status",
                "Success",
                {"id": "runs_ok", "stat": "SampleCount", "period": period, "label": "Succeeded"},
            ],
            [
                namespace,
                "Duration",
                "TaskName",
                stage.task_name,
                "Status",
                "Failure",
                {"id": "runs_failed", "stat": "SampleCount", "period": period, "label": "Failed"},
            ],
        ]
    search = _search(namespace, "SampleId,Status,TaskName", "Duration", stage.task_name, "SampleCount", period)
    return [[{"expression": f"SUM({search})", "id": "runs", "label": f"{stage.task_name} runs"}]]


# ---------------------------------------------------------------------------------------------- widgets


def _metric_widget(
    title, metrics, region, *, view="timeSeries", stacked=False, period=3600, y_label=None, width=12, height=6
):
    props = {"title": title, "region": region, "view": view, "stacked": stacked, "period": period, "metrics": metrics}
    if y_label and view == "timeSeries":
        props["yAxis"] = {"left": {"min": 0, "label": y_label, "showUnits": False}}
    return {"type": "metric", "width": width, "height": height, "properties": props}


def _text_widget(markdown, width=GRID_WIDTH, height=2):
    return {"type": "text", "width": width, "height": height, "properties": {"markdown": markdown}}


def _layout(widgets) -> list:
    """Place widgets left to right, wrapping rows at the 24-column grid width."""
    x = y = row_height = 0
    for widget in widgets:
        if x + widget["width"] > GRID_WIDTH:
            x, y, row_height = 0, y + row_height, 0
        widget["x"], widget["y"] = x, y
        x += widget["width"]
        row_height = max(row_height, widget["height"])
    return widgets


def pipeline_widgets(region: str, state_machine_arn: str) -> list:
    """Whole-pipeline widgets from the free AWS/States metrics of one state machine."""
    dim = ["StateMachineArn", state_machine_arn]
    runs = [
        ["AWS/States", name, *dim, {"id": name.lower(), "stat": "Sum", "label": label}]
        for name, label in (
            ("ExecutionsStarted", "Started"),
            ("ExecutionsSucceeded", "Succeeded"),
            ("ExecutionsFailed", "Failed"),
            ("ExecutionsTimedOut", "Timed out"),
            ("ExecutionsAborted", "Aborted"),
        )
    ]
    # ExecutionTime is in milliseconds and is a single series, so percentiles are real here. It has no
    # status dimension: failed runs count too, so early failures pull the percentiles down.
    times = []
    for stat in ("p50", "p95"):
        times.append(["AWS/States", "ExecutionTime", *dim, {"id": f"ms_{stat}", "stat": stat, "visible": False}])
        times.append([{"expression": f"ms_{stat}/1000", "id": f"s_{stat}", "label": f"End-to-end {stat} (all runs)"}])
    return [
        _metric_widget("Pipeline runs per day (Step Functions)", runs, region, view="bar", period=86400),
        _metric_widget(
            "End-to-end pipeline time, seconds (all runs, failed ones included)",
            times,
            region,
            period=3600,
            y_label="seconds",
        ),
    ]


def alarm_widget(alarm_arns) -> dict:
    """OK/ALARM status of the given alarms (the widget needs full ARNs, not names)."""
    return {
        "type": "alarm",
        "width": 6,
        "height": 4,
        "properties": {"title": "Alarm status", "alarms": list(alarm_arns)},
    }


DEFAULT_SFN_LOG_GROUP = "/aws/vendedlogs/states/geyser-sfn-dev"  # infrastructure/step_functions.tf
# Terraform-managed, with retention (infrastructure/resilience.tf, #104). Logs from before that
# change are in the AWS-created groups /aws/batch/job and /aws/lambda/geyser-sfn-trigger.
BATCH_LOG_GROUP = "/aws/batch/geyser-dev"
TRIGGER_LOG_GROUP = "/aws/lambda/geyser-sfn-trigger-dev"


def _log_group_url(region: str, name: str) -> str:
    # The CloudWatch console double-encodes log group names: "/" -> "%2F" -> "$252F".
    return (
        f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}"
        f"#logsV2:log-groups/log-group/{quote(name, safe='').replace('%', '$25')}"
    )


def console_links(
    region: str = DEFAULT_REGION,
    *,
    state_machine_arn: str = None,
    bucket: str = None,
    trigger_function_name: str = None,
    sfn_log_group: str = DEFAULT_SFN_LOG_GROUP,
    batch_log_group: str = None,
) -> list:
    """(label, url) pairs for the AWS console pages that matter when looking into a run.
    Built only from names and the ARNs passed in; nothing account-specific is stored here."""
    function = trigger_function_name or DEFAULT_TRIGGER_FUNCTION
    console = f"https://{region}.console.aws.amazon.com"
    links = []
    if state_machine_arn:
        links.append(
            (
                "Pipeline runs (Step Functions)",
                f"{console}/states/home?region={region}#/statemachines/view/{quote(state_machine_arn, safe='')}",
            )
        )
    links += [
        ("Batch jobs", f"{console}/batch/home?region={region}#jobs"),
        ("Trigger Lambda", f"{console}/lambda/home?region={region}#/functions/{function}?tab=monitoring"),
        ("Batch job logs", _log_group_url(region, batch_log_group or BATCH_LOG_GROUP)),
        ("Step Functions error logs", _log_group_url(region, sfn_log_group)),
        ("Trigger Lambda logs", _log_group_url(region, TRIGGER_LOG_GROUP)),
        ("Logs Insights", f"{console}/cloudwatch/home?region={region}#logsV2:logs-insights"),
        ("Alarms", f"{console}/cloudwatch/home?region={region}#alarmsV2:"),
    ]
    if bucket:
        s3 = f"https://s3.console.aws.amazon.com/s3/buckets/{bucket}?region={region}"
        links.append(("Data lake (S3)", s3))
        # One JSON per sample: inputs, reference MD5, tool versions, stage timings, headline stats (#67).
        links.append(("Run records (S3 runs/)", f"{s3}&prefix=runs/&showversions=false"))
    links.append(("Budgets", "https://console.aws.amazon.com/billing/home#/budgets"))
    return links


# ---------------------------------------------------------------------------------------------- quality

# Result quality (#58) comes from the Batch job logs, not from custom metrics: every align and variants
# job prints one headline line per stats file (app/tasks.py _log_summary), e.g.
#   flagstat: in_total=10000, duplicates=478, mapped=10000, mapped_pct=100.0, ...
#   mosdepth: length=64444167, bases=1428300, mean_depth=0.02
#   bcftools stats: records=10, snps=10, indels=0, tstv=0.25
# and timed_stage's "--- Starting '<Task>' for sample '<id>' ---" names the sample in the same log
# stream. Logs Insights joins the two per stream, then the align and variants jobs per sample. That
# costs a Logs Insights scan of the Batch log group each time the widget loads (tens of KB per run),
# instead of $0.30 a month per custom metric (more with a SampleId dimension, see #54).
# tests/test_monitoring.py renders real lines through _log_summary and checks every pattern below.

# Logs Insights regex: named groups are (?<name>...). Field names are prefixed x_ so the stats aliases
# below can reuse the plain names (Logs Insights rejects redefining a field).  keeps a key from
# matching inside a longer one (duplicates= inside a future primary_duplicates=).
# Duplicate % is flagstat's duplicates over in_total (all records, secondary and supplementary
# included), so it reads slightly below markdup's own primary-read rate on data with many of those.
QUALITY_PARSE = (
    ("starting", "\"--- Starting '*' for sample '*' ---\" as x_task, x_sample"),
    ("reads", r"/^flagstat: in_total=(?<x_reads>\d+)/"),
    ("dups", r"/^flagstat: .*\bduplicates=(?<x_dups>\d+)/"),
    ("mapped", r"/^flagstat: .*\bmapped_pct=(?<x_mapped>[\d.]+)/"),
    ("depth", r"/^mosdepth: .*\bmean_depth=(?<x_depth>[\d.]+)/"),
    ("variants", r"/^bcftools stats: records=(?<x_vars>\d+), snps=(?<x_snps>\d+), indels=(?<x_indels>\d+)/"),
    ("tstv", r"/^bcftools stats: .*\btstv=(?<x_tstv>[\d.]+)/"),
)
QUALITY_COLUMNS = (
    "sample",
    "last_run",
    "reads",
    "mapped_pct",
    "dup_pct",
    "mean_depth",
    "variants",
    "snps",
    "indels",
    "tstv",
)


@dataclass(frozen=True)
class QualityThresholds:
    """When a sample is listed under "outside thresholds": low mapping, high duplication, no variants,
    aligned without variant stats, or Ti/Tv out of range. Ti/Tv is only judged with enough SNPs:
    whole-genome germline calls sit around 2.0-2.1, but a handful of SNPs (synthetic data) says nothing."""

    min_mapped_pct: float = 90.0
    max_dup_pct: float = 30.0
    tstv_range: tuple = (1.8, 2.4)
    min_snps_for_tstv: int = 1000

    def filter_clause(self) -> str:
        low, high = self.tstv_range
        return (
            f"mapped_pct < {self.min_mapped_pct} or dup_pct > {self.max_dup_pct} or variants = 0 "
            # Aligned but no variant stats: variant calling failed or hasn't finished (bcftools stats is
            # best effort, so a failed stats run also shows here).
            "or (ispresent(reads) and not ispresent(variants)) "
            f"or (snps >= {self.min_snps_for_tstv} and (tstv < {low} or tstv > {high}))"
        )


DEFAULT_THRESHOLDS = QualityThresholds()


def quality_query(thresholds: QualityThresholds = None) -> str:
    """One row per sample (core pipeline: align + bcftools variants), newest first. With thresholds,
    only the samples outside them.

    Two stats steps: per log stream (one Batch job), then per sample (its align and variants jobs).
    Duplicate % is worked out per job first, so it is always one run's own figure. A sample run more
    than once in the window shows the highest value of each column across those runs, which can come
    from different runs: the run record (runs/<id>.provenance.json) has each run's own. A job whose
    start line is outside the time range has no sample name and is left out."""
    lines = [
        "fields @timestamp, @message",
        "| filter @message like /^--- Starting '(Align|CallVariants)' for sample /"
        " or @message like /^(flagstat|mosdepth|bcftools stats): /",
    ]
    lines += [f"| parse @message {pattern}" for _, pattern in QUALITY_PARSE]
    lines += [
        "| stats latest(x_sample) as sample, max(@timestamp) as t, max(x_reads) as rd, max(x_dups) as dp,"
        " max(x_mapped) as mp, max(x_depth) as dpt, max(x_vars) as v, max(x_snps) as sn,"
        " max(x_indels) as ind, max(x_tstv) as tt by @logStream",
        "| filter ispresent(sample)",
        "| fields 100 * dp / rd as dpp",
        "| stats max(t) as last_run, max(rd) as reads, max(mp) as mapped_pct, max(dpp) as dup_raw,"
        " max(dpt) as mean_depth, max(v) as variants, max(sn) as snps, max(ind) as indels, max(tt) as tstv"
        " by sample",
        "| fields round(dup_raw, 2) as dup_pct",
    ]
    if thresholds is not None:
        lines.append(f"| filter {thresholds.filter_clause()}")
    lines += [f"| display {', '.join(QUALITY_COLUMNS)}", "| sort last_run desc", "| limit 100"]
    return "\n".join(lines)


def quality_trend_query() -> str:
    """Daily mean mapped % and duplicate % across all alignments, and how many there were."""
    parse = dict(QUALITY_PARSE)
    return "\n".join(
        [
            "fields @timestamp, @message",
            "| filter @message like /^flagstat: /",
            *(f"| parse @message {parse[name]}" for name in ("reads", "dups", "mapped")),
            "| fields 100 * x_dups / x_reads as x_dup_pct",
            "| stats avg(x_mapped) as mapped_pct, avg(x_dup_pct) as dup_pct, count(*) as alignments by bin(1d)",
        ]
    )


def _log_widget(title, query, region, log_group, *, view="table", width=GRID_WIDTH, height=6):
    return {
        "type": "log",
        "width": width,
        "height": height,
        "properties": {"title": title, "region": region, "view": view, "query": f"SOURCE '{log_group}' | {query}"},
    }


def quality_widgets(
    region: str = DEFAULT_REGION, log_group: str = BATCH_LOG_GROUP, thresholds: QualityThresholds = DEFAULT_THRESHOLDS
) -> list:
    """Result quality from the Batch job logs: a per-sample table, the samples outside thresholds, a trend."""
    low, high = thresholds.tstv_range
    return [
        _text_widget(
            "## Result quality (core pipeline)\n"
            "From the align and variants job logs (samtools flagstat, mosdepth, bcftools stats). Mean depth is "
            "over the whole reference, so a small sample on a large reference shows a tiny depth. Runs from "
            "images before the QC statistics (#102) have no values, and runs before the Terraform-managed log "
            "group (#104) are in `/aws/batch/job`, which these widgets don't read. Each run's full record, with tool "
            "versions, is `runs/<id>.provenance.json` in the data lake (assembled from the parts in "
            "`runs/<id>.provenance/`; `scripts/reconcile.py` reads the parts, so it also sees a failed run's close). "
            f"Thresholds: mapped < "
            f"{thresholds.min_mapped_pct:g}%, duplicates > {thresholds.max_dup_pct:g}%, no variants, or Ti/Tv "
            f"outside {low:g}-{high:g} with at least {thresholds.min_snps_for_tstv:,} SNPs.",
            height=2,
        ),
        _log_widget("Result quality per sample (newest first)", quality_query(), region, log_group, height=7),
        _log_widget(
            "Samples outside quality thresholds", quality_query(thresholds), region, log_group, width=12, height=6
        ),
        _log_widget(
            "Mapped % and duplicate % per day (mean across alignments)",
            quality_trend_query(),
            region,
            log_group,
            view="timeSeries",
            width=12,
            height=6,
        ),
    ]


def storage_widgets(region: str, bucket: str) -> list:
    """Data lake size and object count (#62), from the free daily S3 storage metrics (AWS/S3, one
    datapoint a day per bucket and storage class, about a day behind). The lifecycle rules in
    infrastructure/storage_lifecycle.tf expire intermediate/ and decompressed/; this shows the effect."""
    size = [
        [
            "AWS/S3",
            "BucketSizeBytes",
            "BucketName",
            bucket,
            "StorageType",
            "StandardStorage",
            {"id": "bytes", "stat": "Average", "visible": False},
        ],
        [{"expression": "bytes / 1000000000", "id": "gb", "label": "Data lake size, GB (S3 Standard)"}],
    ]
    count = [
        [
            "AWS/S3",
            "NumberOfObjects",
            "BucketName",
            bucket,
            "StorageType",
            "AllStorageTypes",
            {"id": "objects", "stat": "Average", "label": "Objects"},
        ],
    ]
    return [
        _metric_widget("Data lake size, GB (daily)", size, region, period=86400, y_label="GB"),
        _metric_widget("Data lake objects (daily)", count, region, period=86400, y_label="objects"),
    ]


def build_dashboard(
    region: str = DEFAULT_REGION,
    namespace: str = DEFAULT_NAMESPACE,
    *,
    state_machine_arn: str = None,
    schema: str = "per-sample",
    groups=None,
    lookback: str = "-P14D",
    alarm_arns=None,
    links=None,
    quality: bool = True,
    batch_log_group: str = BATCH_LOG_GROUP,
    thresholds: QualityThresholds = DEFAULT_THRESHOLDS,
    bucket: str = None,
) -> dict:
    """The full monitoring dashboard: alarm status, runs per day, end-to-end time, failures, result
    quality (from the Batch job logs), data lake storage (with a bucket name), stage durations."""
    _check_schema(schema)
    groups = resolve_groups(groups)
    duration_note = (
        "p50 and p95 of each stage"
        if schema == "aggregated"
        else "mean and max across samples (p50/p95 need the aggregated schema; see README). "
        "SEARCH reads at most 500 samples per widget, so over 500 samples in the window "
        "these and the failure counts cover only some of them"
    )
    links = (
        console_links(region, state_machine_arn=state_machine_arn, batch_log_group=batch_log_group)
        if links is None
        else links
    )
    link_line = " · ".join(f"[{label}]({url})" for label, url in links)
    widgets = [
        _text_widget(
            "# Geyser Genomics pipeline\n"
            f"{link_line}\n\n"
            f"Generated by `scripts/deploy_dashboard.py` (schema: **{schema}**). Edits made in the console are "
            f"overwritten on the next deploy. Stage durations show {duration_note}; successful runs only.",
            width=GRID_WIDTH - 6 if alarm_arns else GRID_WIDTH,
            height=4,
        )
    ]
    if alarm_arns:
        widgets.append(alarm_widget(alarm_arns))
    if state_machine_arn:
        widgets += pipeline_widgets(region, state_machine_arn)
    else:
        first_stage = next(iter(groups.values()))[0]
        widgets.append(
            _metric_widget(
                f"Runs per day (from {first_stage.task_name} metrics)",
                stage_runs_metrics(first_stage, schema, namespace, 86400),
                region,
                view="bar",
                period=86400,
                width=GRID_WIDTH,
            )
        )

    all_stages = [stage for stages in groups.values() for stage in stages]
    widgets.append(
        _metric_widget(
            "Failures by stage (per day)",
            failure_metrics(all_stages, schema, namespace, 86400),
            region,
            view="bar",
            stacked=True,
            period=86400,
            width=GRID_WIDTH,
        )
    )
    if quality:
        widgets += quality_widgets(region, batch_log_group, thresholds)
    if bucket:
        widgets += storage_widgets(region, bucket)
    for name, stages in groups.items():
        widgets.append(
            _metric_widget(
                f"Stage duration, seconds: {GROUP_TITLES.get(name, name)}",
                duration_metrics(stages, schema, namespace, 3600),
                region,
                period=3600,
                y_label="seconds",
            )
        )
    return {"start": lookback, "periodOverride": "inherit", "widgets": _layout(widgets)}


def build_pipeline_runtime_dashboard(region: str, state_machine_arn: str, lookback: str = "-P14D") -> dict:
    """Step Functions widgets only (runs per day and end-to-end time)."""
    if not state_machine_arn:
        raise ValueError("state_machine_arn is required")
    return {
        "start": lookback,
        "periodOverride": "inherit",
        "widgets": _layout(pipeline_widgets(region, state_machine_arn)),
    }


# ---------------------------------------------------------------------------------------------- alarms

DEFAULT_TRIGGER_FUNCTION = "geyser-sfn-trigger"  # infrastructure/lambda_trigger.tf: "${var.project_name}-sfn-trigger"
DEFAULT_ALARM_PREFIX = "geyser-dev"


def alarm_names(prefix: str = DEFAULT_ALARM_PREFIX) -> list:
    return [f"{prefix}-trigger-lambda-errors"]


def alarm_arn(region: str, account_id: str, name: str) -> str:
    return ":".join(["arn", "aws", "cloudwatch", region, account_id, "alarm", name])


def build_alarms(
    sns_topic_arn: str = None,
    *,
    trigger_function_name: str = DEFAULT_TRIGGER_FUNCTION,
    prefix: str = DEFAULT_ALARM_PREFIX,
    period: int = 300,
) -> list:
    """put_metric_alarm keyword arguments. Without sns_topic_arn the alarms have no actions (no email);
    their state still shows on the dashboard and in the CloudWatch console."""
    if not trigger_function_name:
        raise ValueError("trigger_function_name is required")
    (name,) = alarm_names(prefix)
    return [
        {
            "AlarmName": name,
            "AlarmDescription": (
                f"The Geyser trigger Lambda {trigger_function_name} failed, so an upload to "
                "raw_reads/ did not start a pipeline run. Check its CloudWatch logs."
            ),
            "ActionsEnabled": bool(sns_topic_arn),
            "AlarmActions": [sns_topic_arn] if sns_topic_arn else [],
            "Namespace": "AWS/Lambda",
            "MetricName": "Errors",
            "Dimensions": [{"Name": "FunctionName", "Value": trigger_function_name}],
            "Statistic": "Sum",
            "Period": period,
            "EvaluationPeriods": 1,
            "DatapointsToAlarm": 1,
            "Threshold": 1.0,
            "ComparisonOperator": "GreaterThanOrEqualToThreshold",
            # Errors has no data while nothing is uploaded; that is healthy, not missing.
            "TreatMissingData": "notBreaching",
        }
    ]


# ---------------------------------------------------------------------------------------------- helpers

ARN_PATTERNS = {
    "sns": re.compile(r"^arn:aws[a-z-]*:sns:[a-z0-9-]+:\d{12}:[\w-]+$"),
    "states": re.compile(r"^arn:aws[a-z-]*:states:[a-z0-9-]+:\d{12}:stateMachine:[\w-]+$"),
}


def check_arn(value: str, service: str, option: str) -> str:
    if value and not ARN_PATTERNS[service].match(value):
        raise SystemExit(f"FATAL: {option} does not look like an {service} ARN: {value!r}")
    return value


def to_json(body) -> str:
    return json.dumps(body, indent=2)
