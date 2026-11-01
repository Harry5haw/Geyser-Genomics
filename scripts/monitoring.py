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

Failure emails: one email per incident (see FAILURE_NOTIFICATIONS). A failed stage is already emailed
by the state machine's Notify_Failure step, so there are deliberately no ExecutionsFailed or per-stage
FailureCount alarms (they would send a second and third email for the same failure). The only alarm
covers what Notify_Failure cannot see: the trigger Lambda failing, so that no run starts at all.

No account IDs or ARNs live in this file: the state machine and SNS topic ARNs are always arguments.
"""

import json
import re
from dataclasses import dataclass

DEFAULT_REGION = "eu-west-2"
DEFAULT_NAMESPACE = "GeyserGenomics"  # app/metrics.py METRIC_NAMESPACE
SCHEMAS = ("per-sample", "aggregated")
GRID_WIDTH = 24
# Expression/metric ids: CloudWatch requires a lowercase first letter, then letters, digits, underscores.
_ID_RE = re.compile(r"[^a-zA-Z0-9_]")


@dataclass(frozen=True)
class Stage:
    task_name: str              # the TaskName dimension value
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
    "core": (Stage("Decompress"), Stage("QualityControl"), Stage("Align"), Stage("CallVariants")),
    "deepvariant": caller_stages("DeepVariant"),
    "pangenome": caller_stages("PangenomeDeepVariant"),
}
GROUP_TITLES = {
    "core": "Core pipeline (bcftools)",
    "deepvariant": "DeepVariant",
    "pangenome": "Pangenome-aware DeepVariant",
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
    return (f"SEARCH('{{{namespace},{dims}}} MetricName=\"{metric}\" TaskName=\"{task_name}\"{extra}', "
            f"'{stat}', {period})")


def duration_metrics(stages, schema: str, namespace: str, period: int) -> list:
    """Metric rows for a duration widget: two lines per stage (p50/p95, or mean/max for per-sample)."""
    _check_schema(schema)
    rows = []
    for stage in stages:
        if schema == "aggregated":
            for stat in ("p50", "p95"):
                rows.append([namespace, "Duration", "TaskName", stage.task_name, "Status", "Success",
                             {"id": f"{stage.metric_id}_{stat}", "stat": stat, "period": period,
                              "label": f"{stage.task_name} {stat}"}])
        else:
            for func, stat, word in (("AVG", "Average", "mean"), ("MAX", "Maximum", "max")):
                search = _search(namespace, "SampleId,Status,TaskName", "Duration", stage.task_name, stat,
                                 period, extra=' Status="Success"')
                rows.append([{"expression": f"{func}({search})", "id": f"{stage.metric_id}_{word}",
                              "label": f"{stage.task_name} {word}"}])
    return rows


def failure_metrics(stages, schema: str, namespace: str, period: int) -> list:
    """Metric rows counting FailureCount per stage (only stages that can record failures)."""
    _check_schema(schema)
    rows = []
    for stage in stages:
        if not stage.records_failures:
            continue
        if schema == "aggregated":
            rows.append([namespace, "FailureCount", "TaskName", stage.task_name,
                         {"id": f"{stage.metric_id}_failures", "stat": "Sum", "period": period,
                          "label": stage.task_name}])
        else:
            search = _search(namespace, "SampleId,TaskName", "FailureCount", stage.task_name, "Sum", period)
            rows.append([{"expression": f"SUM({search})", "id": f"{stage.metric_id}_failures",
                          "label": stage.task_name}])
    return rows


def stage_runs_metrics(stage: Stage, schema: str, namespace: str, period: int) -> list:
    """Runs per period counted from one stage's Duration samples (fallback when no state machine ARN)."""
    _check_schema(schema)
    if schema == "aggregated":
        return [[namespace, "Duration", "TaskName", stage.task_name, "Status", "Success",
                 {"id": "runs_ok", "stat": "SampleCount", "period": period, "label": "Succeeded"}],
                [namespace, "Duration", "TaskName", stage.task_name, "Status", "Failure",
                 {"id": "runs_failed", "stat": "SampleCount", "period": period, "label": "Failed"}]]
    search = _search(namespace, "SampleId,Status,TaskName", "Duration", stage.task_name, "SampleCount", period)
    return [[{"expression": f"SUM({search})", "id": "runs", "label": f"{stage.task_name} runs"}]]


# ---------------------------------------------------------------------------------------------- widgets


def _metric_widget(title, metrics, region, *, view="timeSeries", stacked=False, period=3600, y_label=None,
                   width=12, height=6):
    props = {"title": title, "region": region, "view": view, "stacked": stacked, "period": period,
             "metrics": metrics}
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
    runs = [["AWS/States", name, *dim, {"id": name.lower(), "stat": "Sum", "label": label}]
            for name, label in (("ExecutionsStarted", "Started"), ("ExecutionsSucceeded", "Succeeded"),
                                ("ExecutionsFailed", "Failed"), ("ExecutionsTimedOut", "Timed out"),
                                ("ExecutionsAborted", "Aborted"))]
    # ExecutionTime is in milliseconds and is a single series, so percentiles are real here.
    times = []
    for stat in ("p50", "p95"):
        times.append(["AWS/States", "ExecutionTime", *dim, {"id": f"ms_{stat}", "stat": stat, "visible": False}])
        times.append([{"expression": f"ms_{stat}/1000", "id": f"s_{stat}", "label": f"End-to-end {stat}"}])
    return [
        _metric_widget("Pipeline runs per day (Step Functions)", runs, region, view="bar", period=86400),
        _metric_widget("End-to-end pipeline time, seconds (Step Functions ExecutionTime)", times, region,
                       period=3600, y_label="seconds"),
    ]


def build_dashboard(region: str = DEFAULT_REGION, namespace: str = DEFAULT_NAMESPACE, *,
                    state_machine_arn: str = None, schema: str = "per-sample", groups=None,
                    lookback: str = "-P14D") -> dict:
    """The full monitoring dashboard: stage durations, failures, runs per day, end-to-end time."""
    _check_schema(schema)
    groups = resolve_groups(groups)
    duration_note = ("p50 and p95 of each stage" if schema == "aggregated" else
                     "mean and max across samples (p50/p95 need the aggregated schema; see README)")
    widgets = [_text_widget(
        "# Geyser Genomics pipeline\n"
        f"Generated by `scripts/deploy_dashboard.py` (schema: **{schema}**). Edits made in the console are "
        f"overwritten on the next deploy. Stage durations show {duration_note}; successful runs only."
    )]
    if state_machine_arn:
        widgets += pipeline_widgets(region, state_machine_arn)
    else:
        first_stage = next(iter(groups.values()))[0]
        widgets.append(_metric_widget(f"Runs per day (from {first_stage.task_name} metrics)",
                                      stage_runs_metrics(first_stage, schema, namespace, 86400),
                                      region, view="bar", period=86400, width=GRID_WIDTH))

    all_stages = [stage for stages in groups.values() for stage in stages]
    widgets.append(_metric_widget("Failures by stage (per day)",
                                  failure_metrics(all_stages, schema, namespace, 86400),
                                  region, view="bar", stacked=True, period=86400, width=GRID_WIDTH))
    for name, stages in groups.items():
        widgets.append(_metric_widget(f"Stage duration, seconds: {GROUP_TITLES.get(name, name)}",
                                      duration_metrics(stages, schema, namespace, 3600),
                                      region, period=3600, y_label="seconds"))
    return {"start": lookback, "periodOverride": "inherit", "widgets": _layout(widgets)}


def build_pipeline_runtime_dashboard(region: str, state_machine_arn: str, lookback: str = "-P14D") -> dict:
    """Step Functions widgets only (runs per day and end-to-end time)."""
    if not state_machine_arn:
        raise ValueError("state_machine_arn is required")
    return {"start": lookback, "periodOverride": "inherit",
            "widgets": _layout(pipeline_widgets(region, state_machine_arn))}


# ---------------------------------------------------------------------------------------------- alarms

DEFAULT_TRIGGER_FUNCTION = "geyser-sfn-trigger"  # infrastructure/lambda_trigger.tf: "${var.project_name}-sfn-trigger"

# Who emails for each kind of failure. Kept here so the README, the PR and the code agree.
FAILURE_NOTIFICATIONS = (
    ("A stage fails (caught by the state machine)", "Notify_Failure step in the state machine (exists)"),
    ("The trigger Lambda errors, so no run starts", "Alarm <prefix>-trigger-lambda-errors (deploy_alarms.py)"),
    ("Notify_Failure's own publish fails, or a run times out / is aborted",
     "Not covered yet: needs an EventBridge rule (future infrastructure branch)"),
)


def build_alarms(sns_topic_arn: str, *, trigger_function_name: str = DEFAULT_TRIGGER_FUNCTION,
                 prefix: str = "geyser-dev", period: int = 300) -> list:
    """put_metric_alarm keyword arguments for the alarms that fill gaps Notify_Failure leaves.

    Alarms email only when they change from OK to ALARM, so a burst of failures (S3 retries the
    Lambda twice) sends one email, not one per error.
    """
    if not sns_topic_arn:
        raise ValueError("sns_topic_arn is required")
    if not trigger_function_name:
        raise ValueError("trigger_function_name is required")
    return [{
        "AlarmName": f"{prefix}-trigger-lambda-errors",
        "AlarmDescription": (f"The Geyser trigger Lambda {trigger_function_name} failed, so an upload to "
                             "raw_reads/ did not start a pipeline run. Check its CloudWatch logs."),
        "ActionsEnabled": True,
        "AlarmActions": [sns_topic_arn],
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
    }]


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
