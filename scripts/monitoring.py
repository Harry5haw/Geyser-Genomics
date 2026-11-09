"""CloudWatch dashboard and alarm definitions for Geyser Genomics (pure builders, no AWS calls).

The deploy scripts (deploy_dashboard.py, runtime_deploy_pipeline_dashboard.py, deploy_alarms.py) build
their JSON here and only touch AWS when run without --dry-run.

Metric shapes (see app/metrics.py):
  Duration      dims TaskName, Status   (every stage run)
  FailureCount  dims TaskName           (only when a stage fails)

Two metric schemas are supported:
  aggregated  what app/metrics.py publishes (the default). One series per stage gives real p50/p95 durations.
  per-sample  the shape published before Nov 2026, with a SampleId dimension. Each sample was its own metric,
              so CloudWatch could not compute percentiles across runs; durations are shown as mean and max of
              the per-sample series (SEARCH). Kept to read data recorded by older images (CloudWatch keeps
              1-hour points for 455 days).

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
# The hosted site's web API Lambda (infrastructure/web_api.tf: "${var.project_name}-web-api-${var.environment}").
DEFAULT_WEB_FUNCTION = "geyser-web-api-dev"
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
    # v1.3's large route: the pre-flight split and one AlignChunk per chunk; the merge is the core Align.
    "genome": (Stage("Preflight"), Stage("AlignChunk")),
    "deepvariant": caller_stages("DeepVariant"),
    # v1.5: make_examples split over CPU part jobs (no run-record part); the GPU job is the DeepVariant stage.
    "dv_split": (Stage("DeepVariantExamples"),),
    "pangenome": caller_stages("PangenomeDeepVariant"),
    # Scatter-gather calling (#108): one CallVariantsShard per region; the merge is the core CallVariants.
    "shards": (Stage("CallVariantsShard"),),
    # The quality gate after variant calling (#144): its run time, and its failures (GATE_MODE=fail).
    "gate": (Stage("QualityGate"),),
    # The hosted site's Publish job at the end of every run (#213, #257): web/'s manage.py publish.
    "publish": (Stage("Publish"),),
    # v3.0 (#207/#209, O8): the opt-in AlphaGenome annotation, an extra stage (a failure never fails the run).
    "annotate": (Stage("Annotate"),),
}
GROUP_TITLES = {
    "core": "Core pipeline (bcftools)",
    "genome": "Large route (pre-flight and align chunks)",
    "dv_split": "DeepVariant make_examples parts (CPU)",
    "deepvariant": "DeepVariant",
    "pangenome": "Pangenome-aware DeepVariant",
    "shards": "Variant calling shards (per region)",
    "gate": "Quality gate",
    "publish": "Publish (hosted site)",
    "annotate": "AlphaGenome annotation (opt-in)",
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
# Batch "Job State Change" events at RUNNING (infrastructure/batch_wait.tf, #138).
BATCH_EVENTS_LOG_GROUP = "/aws/events/geyser-batch-jobs-dev"
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


# O11: the quality gate's key values (app/tasks.py GATE_VALUES_METRIC / GATE_VALUE_CHECKS), one series per check.
GATE_VALUES_METRIC = "QualityGateValue"
GATE_VALUE_CHECKS = ("mapped_pct", "duplicate_pct", "mean_depth", "breadth_10x_pct", "tstv")


def gate_value_widgets(region: str = DEFAULT_REGION, namespace: str = DEFAULT_NAMESPACE) -> list:
    """The gate's measured values per run (one point per gate run), so a change of image or reference that
    shifts quality shows as a step. Percentages on one widget; depth and Ti/Tv (other scales) on another."""

    def rows(checks):
        return [
            [namespace, GATE_VALUES_METRIC, "Check", c, {"id": f"gate_{c}", "stat": "Average", "label": c}]
            for c in checks
        ]

    return [
        _metric_widget(
            "Quality gate: mapped %, duplicate %, breadth at 10x % (per run)",
            rows(("mapped_pct", "duplicate_pct", "breadth_10x_pct")),
            region,
            period=3600,
            y_label="%",
        ),
        _metric_widget(
            "Quality gate: mean depth and Ti/Tv (per run)",
            rows(("mean_depth", "tstv")),
            region,
            period=3600,
        ),
    ]


# O8 (v3.0): the AlphaGenome annotation as COUNTS only (app/tasks.py ANNOTATE_CALLS_METRIC, record_counts by
# TaskName). Its duration and failures are the Annotate stage group's. No widget ever reads annotation outputs or
# their logs: AlphaGenome's terms forbid publishing model outputs, and the API key (SSM) is never logged.
ANNOTATE_CALLS_METRIC = "AnnotateApiCalls"


def annotate_widgets(region: str = DEFAULT_REGION, namespace: str = DEFAULT_NAMESPACE) -> list:
    """How many AlphaGenome API calls the annotations made per day (a cost and quota signal), next to a note on what
    this row never shows."""
    return [
        _text_widget(
            "## AlphaGenome annotation (opt-in, v3.0)\n"
            "Counts only: API calls per day here, and the Annotate stage's duration and failures in its stage group. "
            "Scores, tracks and genes are never shown on the dashboard (AlphaGenome's output terms); the API key is "
            "an SSM SecureString mapped into the annotate job, never logged.",
            height=2,
        ),
        _metric_widget(
            "AlphaGenome API calls (per day)",
            [
                [
                    namespace,
                    ANNOTATE_CALLS_METRIC,
                    "TaskName",
                    "Annotate",
                    {"id": "ag_calls", "stat": "Sum", "label": "calls"},
                ]
            ],
            region,
            view="bar",
            period=86400,
            y_label="calls",
        ),
    ]


# The stage is the job name's first word: the state machine names jobs <Stage>-<sample>-<execution>
# (QualityControl, AlignGenome, PlanRegions, CallVariantsShard, CallVariants; DeepVariant on the GPU queue).
QUEUE_WAIT_PARSE = (
    r"| parse job /^(?<stage>[A-Za-z]+)-/",
    r"| parse q /job-queue\/(?<queue>[^\/]+)$/",
)


def queue_wait_query() -> str:
    """Per stage and queue: how many jobs, and their mean and max wait from submission to RUNNING
    (scheduling, capacity, image pull), from the Batch RUNNING events in BATCH_EVENTS_LOG_GROUP."""
    return "\n".join(
        [
            "fields detail.jobName as job, detail.jobQueue as q, (detail.startedAt - detail.createdAt) / 1000 as wait_s",
            *QUEUE_WAIT_PARSE,
            "| stats count(*) as jobs, avg(wait_s) as mean_wait_s, max(wait_s) as max_wait_s by stage, queue",
            "| sort max_wait_s desc",
        ]
    )


def queue_wait_trend_query() -> str:
    """Hourly mean and max wait across all jobs (the table splits it by stage and queue)."""
    return "\n".join(
        [
            "fields (detail.startedAt - detail.createdAt) / 1000 as wait_s",
            "| stats avg(wait_s) as mean_wait_s, max(wait_s) as max_wait_s by bin(1h)",
        ]
    )


def queue_wait_widgets(region: str = DEFAULT_REGION, log_group: str = BATCH_EVENTS_LOG_GROUP) -> list:
    """Batch wait before a job starts (#138), next to the stage durations: on the GPU queue a job can wait
    minutes for an instance, which the stage durations (run time only) don't show."""
    return [
        _log_widget(
            "Batch wait before a job runs, seconds (submit -> RUNNING), by stage and queue",
            queue_wait_query(),
            region,
            log_group,
            width=12,
        ),
        _log_widget(
            "Batch wait before a job runs, seconds (hourly mean and max)",
            queue_wait_trend_query(),
            region,
            log_group,
            view="timeSeries",
            width=12,
        ),
    ]


# The job-log line every stage writes after updating the run record (app/provenance.py RECORD_LOG_PREFIX).
RUN_RECORD_PREFIX = "run-record: "
RUN_RECORD_FIELDS = (
    "task",
    "sample_id",
    "run_name",
    "status",
    "route",
    "caller_outcome",
    "infra_retries",
    "app_version",
    "reference",
    "gate",
)


def _run_record_parse() -> list:
    """Logs Insights lines that pull the run-record line's fields out (strings, numbers or null)."""
    lines = [f"filter @message like /^{RUN_RECORD_PREFIX.strip()}/"]
    for field in RUN_RECORD_FIELDS:
        lines.append(f'| parse @message /"{field}": "?(?<r_{field}>[^",}}]*)"?/')
    return lines


def recent_runs_query() -> str:
    """One row per run: its latest record line (the fullest view), newest first. Shows the route, whose calls
    the VCF holds (a DeepVariant run that fell back to bcftools reads fallback_bcftools), the reference, the
    image, the gate verdict and the attempts lost to Spot."""
    return "\n".join(
        [
            *_run_record_parse(),
            "| stats latest(r_sample_id) as sample, latest(r_status) as status, latest(r_route) as route, "
            "latest(r_caller_outcome) as calls, latest(r_reference) as reference, latest(r_gate) as gate, "
            "max(r_infra_retries) as spot_retries, latest(r_app_version) as image, latest(r_task) as last_stage, "
            "max(@timestamp) as updated by r_run_name",
            "| sort updated desc",
            "| limit 50",
        ]
    )


def caller_outcome_query() -> str:
    """Runs per caller outcome (bcftools, deepvariant, pangenome, fallback_bcftools), from each run's latest line."""
    return "\n".join(
        [
            *_run_record_parse(),
            "| stats latest(r_caller_outcome) as outcome by r_run_name",
            "| stats count(*) as runs by outcome",
        ]
    )


def spot_retry_query() -> str:
    """Jobs that started again after a lost attempt: Batch retries only infrastructure failures (Spot
    interruptions, a host going away), and the RUNNING event of a retry lists the earlier attempt's reason."""
    return "\n".join(
        [
            "fields detail.jobName as job, detail.attempts.0.statusReason as reason",
            "| filter ispresent(detail.attempts.0.statusReason)",
            *QUEUE_WAIT_PARSE[:1],
            "| stats count(*) as retried_jobs by stage, reason",
            "| sort retried_jobs desc",
        ]
    )


def run_widgets(
    region: str = DEFAULT_REGION,
    batch_log_group: str = BATCH_LOG_GROUP,
    batch_events_log_group: str = BATCH_EVENTS_LOG_GROUP,
) -> list:
    """Per-run widgets from the run-record log lines (#251) and the Batch RUNNING events."""
    return [
        _log_widget("Recent runs (from the run record)", recent_runs_query(), region, batch_log_group, height=7),
        _log_widget(
            "Whose calls: runs by caller outcome (fallback_bcftools = the run's own caller failed)",
            caller_outcome_query(),
            region,
            batch_log_group,
            width=12,
        ),
        _log_widget(
            "Jobs retried after a lost attempt (Spot interruptions), by stage",
            spot_retry_query(),
            region,
            batch_events_log_group,
            width=12,
        ),
    ]


def gpu_widgets(region: str, namespace: str, schema: str) -> list:
    """DeepVariant's GPU step: utilisation and peak memory (#139). Aggregated schema only (one series per step)."""
    if schema != "aggregated":
        return []
    task = "DeepVariant.CallVariants"
    dims = ["TaskName", task, "Status", "Success"]
    return [
        _metric_widget(
            "DeepVariant call_variants on the GPU: utilisation % (mean, peak)",
            [
                [
                    namespace,
                    "GpuUtilization",
                    *dims,
                    {"id": "gpu_util_mean", "stat": "Average", "label": "mean utilisation"},
                ],
                [
                    namespace,
                    "GpuUtilizationPeak",
                    *dims,
                    {"id": "gpu_util_peak", "stat": "Maximum", "label": "peak utilisation"},
                ],
            ],
            region,
            y_label="%",
        ),
        _metric_widget(
            "DeepVariant call_variants on the GPU: peak memory, MiB",
            [
                [
                    namespace,
                    "GpuPeakMemory",
                    *dims,
                    {"id": "gpu_mem_peak", "stat": "Maximum", "label": "peak GPU memory"},
                ]
            ],
            region,
            y_label="MiB",
        ),
    ]


# Spend guardrails (O9): the live values, read from AWS Budgets and .claude/hooks/guard_aws.py on 9 Nov 2026.
# They change rarely and only by the user; update them here (and redeploy) when they do.
GUARDRAILS = {
    "checked": "9 Nov 2026",
    "cap_usd": 30,  # budget geyser-claude-project-cap (CUSTOM period, ends 1 Nov 2027: renew before)
    "kill_switch_usd": 27,  # its budget action: attaches the deny-all geyser-claude-kill-switch at actual spend
    "run_block_usd": 24,  # guard_aws.py RUN_BLOCK_USD: no new runs from here
    "gpu_block_usd": 20,  # guard_aws.py GPU_BLOCK_USD: no GPU jobs from here
    "monthly_alert_usd": 10,  # budget geyser-monthly-cost
}
BUDGETS_URL = "https://us-east-1.console.aws.amazon.com/costmanagement/home#/budgets"
COST_EXPLORER_URL = "https://us-east-1.console.aws.amazon.com/costmanagement/home#/cost-explorer"


def spend_widgets(guardrails: dict = None) -> list:
    """O9: the account's estimated charges this month (AWS/Billing, published only in us-east-1, free once
    "Receive Billing Alerts" is on; hours ahead of Budgets) next to the guardrails' lines."""
    g = guardrails or GUARDRAILS
    charges = [
        [
            "AWS/Billing",
            "EstimatedCharges",
            "Currency",
            "USD",
            {"id": "charges", "stat": "Maximum", "label": "Estimated charges this month, USD", "region": "us-east-1"},
        ]
    ]
    widget = _metric_widget(
        "Estimated charges this month, USD (us-east-1, every ~6 h)", charges, "us-east-1", period=21600, y_label="USD"
    )
    widget["properties"]["annotations"] = {
        "horizontal": [
            {"label": f"kill switch ${g['kill_switch_usd']}", "value": g["kill_switch_usd"]},
            {"label": f"no new runs ${g['run_block_usd']}", "value": g["run_block_usd"]},
            {"label": f"no GPU jobs ${g['gpu_block_usd']}", "value": g["gpu_block_usd"]},
        ]
    }
    lines = [
        "### Spend guardrails",
        "",
        f"- Project cap **${g['cap_usd']}** (budget `geyser-claude-project-cap`, ends 1 Nov 2027: renew before)",
        f"- Kill switch at **${g['kill_switch_usd']}** actual: attaches deny-all `geyser-claude-kill-switch` "
        "(Budgets action status STANDBY = armed)",
        f"- Guard hook: no new runs from **${g['run_block_usd']}**, no GPU jobs from **${g['gpu_block_usd']}**",
        f"- Monthly alert ${g['monthly_alert_usd']} (`geyser-monthly-cost`)",
        "",
        f"[Budgets and actions]({BUDGETS_URL}) · [Cost Explorer]({COST_EXPLORER_URL}). "
        f"Values checked {g['checked']}; Budgets lags hours, the estimate is the earlier hint.",
    ]
    text = _text_widget(
        "\n".join(lines),
        width=12,
        height=6,
    )
    return [widget, text]


def running_now_widgets(region: str) -> list:
    """O10: what bills while idle. Running vCPUs from the free AWS/Usage metrics (EC2 by instance class: G/VT
    is the GPU environment; Fargate on-demand and Spot), and every bucket's size (AWS/S3, daily)."""
    usage = (
        'SEARCH(\'{AWS/Usage,Class,Resource,Service,Type} Service="EC2" Type="Resource" Resource="vCPU"\', '
        "'Maximum', 300)"
    )
    fargate = "SEARCH('{AWS/Usage,Class,Resource,Service,Type} Service=\"Fargate\" Type=\"Resource\"', 'Maximum', 300)"
    buckets = (
        'SEARCH(\'{AWS/S3,BucketName,StorageType} MetricName="BucketSizeBytes" StorageType="StandardStorage"\', '
        "'Average', 86400)"
    )
    return [
        _metric_widget(
            "Running now: EC2 vCPUs by class (G/VT = GPU env) and Fargate tasks",
            [
                [{"expression": usage, "id": "ec2_vcpu", "label": "EC2 vCPUs"}],
                [{"expression": fargate, "id": "fargate", "label": "Fargate"}],
            ],
            region,
            period=300,
            y_label="count",
        ),
        _metric_widget(
            "S3 storage by bucket, GB (Standard, daily)",
            [
                [{"expression": buckets, "id": "bucket_bytes", "visible": False}],
                [{"expression": "bucket_bytes / 1000000000", "id": "bucket_gb", "label": "GB"}],
            ],
            region,
            period=86400,
            y_label="GB",
        ),
    ]


BENCHMARKS_URL = "https://github.com/Harry5haw/Geyser-Genomics/blob/main/docs/benchmarks/README.md"


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


def web_widgets(region: str, function_name: str = DEFAULT_WEB_FUNCTION, distribution_id: str = None) -> list:
    """The hosted site (O7): the web API Lambda's errors, throttles and duration (free AWS/Lambda metrics) and,
    with a distribution ID, CloudFront's 4xx/5xx error rates. CloudFront publishes its metrics only in us-east-1
    (dimension Region=Global), so that widget reads us-east-1 whatever the dashboard's region."""
    fn = ["FunctionName", function_name]
    calls = [
        ["AWS/Lambda", "Invocations", *fn, {"id": "invocations", "stat": "Sum", "label": "Requests"}],
        ["AWS/Lambda", "Errors", *fn, {"id": "errors", "stat": "Sum", "label": "Errors"}],
        # Throttles: requests turned away by the reserved concurrency (web_api_reserved_concurrency): the 429s.
        ["AWS/Lambda", "Throttles", *fn, {"id": "throttles", "stat": "Sum", "label": "Throttled (429)"}],
    ]
    duration = [
        ["AWS/Lambda", "Duration", *fn, {"id": f"d_{stat}", "stat": stat, "label": f"Duration {stat}"}]
        for stat in ("p50", "p95")
    ]
    widgets = [
        _text_widget(
            f"## Hosted site\nThe web API Lambda `{function_name}` (uploads, status, downloads)"
            + (", and the CloudFront distribution in front of the site." if distribution_id else ".")
            + " The Publish job's run time and failures are in the stage widgets (group *Publish*).",
        ),
        _metric_widget(f"Web API requests, errors, throttles: {function_name}", calls, region, view="bar", period=3600),
        _metric_widget("Web API duration, ms (p50, p95)", duration, region, period=3600, y_label="ms"),
    ]
    if distribution_id:
        cf = ["DistributionId", distribution_id, "Region", "Global"]
        rates = [
            ["AWS/CloudFront", "4xxErrorRate", *cf, {"id": "e4xx", "stat": "Average", "label": "4xx %"}],
            ["AWS/CloudFront", "5xxErrorRate", *cf, {"id": "e5xx", "stat": "Average", "label": "5xx %"}],
            [
                "AWS/CloudFront",
                "Requests",
                *cf,
                {"id": "requests", "stat": "Sum", "label": "Requests", "yAxis": "right"},
            ],
        ]
        widgets.append(
            _metric_widget(
                "Site (CloudFront): error rates, % (requests on the right)",
                rates,
                "us-east-1",
                period=3600,
                width=GRID_WIDTH,
            )
        )
    return widgets


def build_dashboard(
    region: str = DEFAULT_REGION,
    namespace: str = DEFAULT_NAMESPACE,
    *,
    state_machine_arn: str = None,
    schema: str = "aggregated",
    groups=None,
    lookback: str = "-P14D",
    alarm_arns=None,
    links=None,
    quality: bool = True,
    batch_log_group: str = BATCH_LOG_GROUP,
    thresholds: QualityThresholds = DEFAULT_THRESHOLDS,
    bucket: str = None,
    queue_wait: bool = True,
    batch_events_log_group: str = BATCH_EVENTS_LOG_GROUP,
    runs: bool = True,
    spend: bool = True,
    web: bool = True,
    web_function_name: str = DEFAULT_WEB_FUNCTION,
    web_distribution_id: str = None,
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
            f"overwritten on the next deploy. Stage durations show {duration_note}; successful runs only. "
            f"Accuracy (hap.py against GIAB) per configuration: [benchmark history]({BENCHMARKS_URL}).",
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
    if spend:
        widgets += spend_widgets() + running_now_widgets(region)
    if runs:
        widgets += run_widgets(region, batch_log_group, batch_events_log_group)
    if quality:
        widgets += quality_widgets(region, batch_log_group, thresholds) + gate_value_widgets(region, namespace)
    if bucket:
        widgets += storage_widgets(region, bucket)
    if web:
        widgets += web_widgets(region, web_function_name, web_distribution_id)
    if "deepvariant" in groups:
        widgets += gpu_widgets(region, namespace, schema)
    if "annotate" in groups:
        widgets += annotate_widgets(region, namespace)
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
    if queue_wait:
        widgets += queue_wait_widgets(region, batch_events_log_group)
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
