"""Per-stage queue wait, run time and estimated cost for one Step Functions execution (#59, #60).

For each Batch task in the execution history this finds the Batch job and splits its time into
  wait  createdAt -> startedAt   (queued: RUNNABLE/STARTING, incl. capacity and image pull)
  run   startedAt -> stoppedAt   (the container running)
and estimates the Fargate cost of the run from the job's vCPU and memory. It also reports the number
of attempts and, for jobs that did not succeed, why (Batch statusReason plus the container's reason and
exit code, e.g. "OutOfMemoryError: Container killed due to memory usage (exit 137)").

The job description comes from Batch describe_jobs when Batch still has it, otherwise from the copy
Step Functions keeps in the task's output (success) or cause (failure). Batch only keeps finished jobs
for a limited time, so older executions rely on the history copy.

Cost is an estimate: run seconds x (vCPU x vCPU-hour price + GB x GB-hour price), with Fargate's
1-minute minimum. Fargate also bills image pull time (part of "wait" here), so the real figure is a
little higher. GPU jobs run on EC2, priced per instance, so their cost is left blank.
Prices are arguments; the defaults are Fargate Linux/x86 on-demand list prices for eu-west-2, to be
checked against https://aws.amazon.com/fargate/pricing/ .

Resource use (#59) comes from the job itself: every stage logs one ``stage-usage: {json}`` line
(app/metrics.py) with its CPU seconds and peak memory, read here from the job's own log stream. Against
the job's vCPU and memory that gives how much of what was paid for was used (cpu_use, memory_use), the
input for right-sizing ``stage_resources``. Jobs from before the line existed show none.
"""

import json
from dataclasses import dataclass

DEFAULT_VCPU_HOUR_USD = 0.04656
DEFAULT_GB_HOUR_USD = 0.00511
# Ephemeral storage above the free 20 GiB; us-east-1 list price, check the eu-west-2 figure.
DEFAULT_STORAGE_GB_HOUR_USD = 0.000111
FARGATE_FREE_STORAGE_GB = 20
FARGATE_MINIMUM_SECONDS = 60
USAGE_LOG_PREFIX = "stage-usage: "  # app/metrics.py USAGE_LOG_PREFIX
DEFAULT_JOB_LOG_GROUP = "/aws/batch/job"  # Batch's group when a job definition names none
USAGE_FIELDS = ("cpu_seconds", "peak_memory_mib", "container_peak_memory_mib")
MAX_LOG_PAGES = 20
BATCH_TASK_EVENTS = {
    "TaskSubmitted": ("taskSubmittedEventDetails", "output"),
    "TaskSucceeded": ("taskSucceededEventDetails", "output"),
    "TaskFailed": ("taskFailedEventDetails", "cause"),
    "TaskTimedOut": ("taskTimedOutEventDetails", "cause"),
}


@dataclass(frozen=True)
class Prices:
    vcpu_hour: float = DEFAULT_VCPU_HOUR_USD
    gb_hour: float = DEFAULT_GB_HOUR_USD
    storage_gb_hour: float = DEFAULT_STORAGE_GB_HOUR_USD


DEFAULT_PRICES = Prices()


def _get(d, name, default=None):
    """Case-insensitive key lookup: Step Functions copies use PascalCase, describe_jobs camelCase."""
    if not isinstance(d, dict):
        return default
    lowered = name.lower()
    for key, value in d.items():
        if key.lower() == lowered:
            return value
    return default


def _json(text):
    try:
        value = json.loads(text) if isinstance(text, str) else text
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def history_events(sfn_client, execution_arn):
    kwargs = {"executionArn": execution_arn, "maxResults": 1000}
    while True:
        page = sfn_client.get_execution_history(**kwargs)
        yield from page.get("events", [])
        if not page.get("nextToken"):
            return
        kwargs["nextToken"] = page["nextToken"]


def batch_tasks(sfn_client, execution_arn) -> list:
    """[{"stage", "job_id", "job"}] for each Batch task state, in execution order.
    "job" is the latest job description found in the history (may be empty).

    Each event is credited to the state its previousEventId chain leads back to, not just the last
    state entered: inside a Parallel state, QC's and align's events interleave."""
    tasks, by_state, last_state = [], {}, None
    state_of = {}  # event id -> state name
    for event in history_events(sfn_client, execution_arn):
        kind = event.get("type")
        if kind == "TaskStateEntered":
            state = last_state = event.get("stateEnteredEventDetails", {}).get("name")
        else:
            state = state_of.get(event.get("previousEventId"), last_state)
        if event.get("id") is not None:
            state_of[event["id"]] = state
        if kind == "TaskStateEntered":
            continue
        if kind not in BATCH_TASK_EVENTS or state is None:
            continue
        details_key, field = BATCH_TASK_EVENTS[kind]
        details = event.get(details_key, {})
        if details.get("resourceType") != "batch":
            continue
        payload = _json(details.get(field))
        if state not in by_state:
            by_state[state] = {"stage": state, "job_id": None, "job": {}}
            tasks.append(by_state[state])
        task = by_state[state]
        task["job_id"] = task["job_id"] or _get(payload, "JobId")
        if _get(payload, "StartedAt") or _get(payload, "Status"):
            task["job"] = payload
    return tasks


def describe_jobs(batch_client, job_ids) -> dict:
    ids = [j for j in dict.fromkeys(job_ids) if j]
    found = {}
    for i in range(0, len(ids), 100):  # describe_jobs takes at most 100 IDs
        for job in batch_client.describe_jobs(jobs=ids[i : i + 100]).get("jobs", []):
            found[job["jobId"]] = job
    return found


def resources(job: dict) -> tuple:
    """(vcpu, memory_gb, gpus) from the job's container resource requirements."""
    reqs = _get(_get(job, "Container", {}), "ResourceRequirements", []) or []
    values = {str(_get(r, "Type", "")).upper(): _get(r, "Value") for r in reqs}

    def num(key):
        try:
            return float(values[key]) if values.get(key) is not None else None
        except ValueError:
            return None

    memory_mb = num("MEMORY")
    return num("VCPU"), (memory_mb / 1024 if memory_mb is not None else None), num("GPU") or 0


def ephemeral_storage_gb(job: dict):
    """The job's Fargate ephemeral storage in GiB, or None if it uses the default 20 GiB."""
    size = _get(_get(_get(job, "Container", {}), "EphemeralStorage", {}) or {}, "SizeInGiB")
    try:
        return float(size) if size is not None else None
    except (TypeError, ValueError):
        return None


def fargate_cost(run_seconds, vcpu, memory_gb, prices: Prices = DEFAULT_PRICES, storage_gb=None):
    """vCPU + memory, plus ephemeral storage beyond the free 20 GiB, for the billed time (1 min minimum)."""
    if run_seconds is None or vcpu is None or memory_gb is None:
        return None
    billed_hours = max(run_seconds, FARGATE_MINIMUM_SECONDS) / 3600
    extra_storage = max(0.0, (storage_gb or 0) - FARGATE_FREE_STORAGE_GB)
    return billed_hours * (
        vcpu * prices.vcpu_hour + memory_gb * prices.gb_hour + extra_storage * prices.storage_gb_hour
    )


def _seconds(start_ms, end_ms):
    return (end_ms - start_ms) / 1000 if start_ms and end_ms else None


def failure_reason(job: dict):
    """Why a job did not succeed, from the job and its last attempt; None for successful jobs."""
    if str(_get(job, "Status", "")).upper() in ("SUCCEEDED", ""):
        return None
    attempts = _get(job, "Attempts", []) or []
    container = _get(attempts[-1], "Container", {}) if attempts else _get(job, "Container", {})
    parts = []
    for text in (_get(job, "StatusReason"), _get(container, "Reason")):
        if text and text not in parts:
            parts.append(str(text))
    exit_code = _get(container, "ExitCode")
    reason = "; ".join(parts) or None
    if exit_code is not None:
        reason = f"{reason} (exit {exit_code})" if reason else f"exit {exit_code}"
    return reason


def job_log_group(job: dict) -> str:
    container = _get(job, "Container", {})
    options = _get(_get(container, "LogConfiguration", {}), "Options", {}) or {}
    return _get(options, "awslogs-group") or DEFAULT_JOB_LOG_GROUP


def parse_usage_line(message: str):
    """The usage dict from one ``stage-usage:`` log line, or None."""
    at = str(message).find(USAGE_LOG_PREFIX)
    if at < 0:
        return None
    value = _json(str(message)[at + len(USAGE_LOG_PREFIX) :].strip())
    return {k: value[k] for k in USAGE_FIELDS if isinstance(value.get(k), (int, float))} or None


def stage_usage(logs_client, job: dict):
    """The job's logged resource use (its last usage line), or None: no client, no stream, no line."""
    stream = _get(_get(job, "Container", {}), "LogStreamName")
    if logs_client is None or not stream:
        return None
    kwargs = {"logGroupName": job_log_group(job), "logStreamNames": [stream], "filterPattern": '"stage-usage"'}
    found = None
    try:
        for _ in range(MAX_LOG_PAGES):
            page = logs_client.filter_log_events(**kwargs)
            for event in page.get("events", []):
                found = parse_usage_line(event.get("message", "")) or found
            if not page.get("nextToken"):
                break
            kwargs["nextToken"] = page["nextToken"]
    except Exception:  # noqa: BLE001 - logs expired (30 days) or no access: no usage, the rest still shows
        return found
    return found


def _share(used, available):
    return None if used is None or not available else round(used / available, 3)


def add_usage(row: dict, usage) -> dict:
    """row plus the usage fields and cpu_use/memory_use: the share of the job's vCPU-time and memory used."""
    usage = usage or {}
    for key in USAGE_FIELDS:
        row[key] = usage.get(key)
    run, vcpu, memory_gb = row.get("run_seconds"), row.get("vcpu"), row.get("memory_gb")
    row["cpu_use"] = _share(row["cpu_seconds"], run * vcpu if run and vcpu else None)
    peak = row["container_peak_memory_mib"] or row["peak_memory_mib"]
    row["memory_use"] = _share(peak, memory_gb * 1024 if memory_gb else None)
    return row


def stage_row(stage: str, job: dict, prices: Prices = DEFAULT_PRICES) -> dict:
    created, started, stopped = (_get(job, k) for k in ("CreatedAt", "StartedAt", "StoppedAt"))
    vcpu, memory_gb, gpus = resources(job)
    run = _seconds(started, stopped)
    attempts = _get(job, "Attempts")
    return {
        "stage": stage,
        "job_id": _get(job, "JobId"),
        "status": _get(job, "Status") or "UNKNOWN",
        "wait_seconds": _seconds(created, started),
        "run_seconds": run,
        "vcpu": vcpu,
        "memory_gb": memory_gb,
        "gpus": gpus,
        "cost_usd": None if gpus else fargate_cost(run, vcpu, memory_gb, prices, ephemeral_storage_gb(job)),
        "attempts": len(attempts) if isinstance(attempts, list) else None,
        "reason": failure_reason(job),
        "log_stream": _get(_get(job, "Container", {}), "LogStreamName"),
    }


def execution_stages(
    sfn_client, batch_client, execution_arn, prices: Prices = DEFAULT_PRICES, logs_client=None
) -> list:
    """One row per Batch task. With logs_client, rows also carry the job's logged resource use."""
    tasks = batch_tasks(sfn_client, execution_arn)
    live = {}
    if batch_client is not None:
        try:
            live = describe_jobs(batch_client, [t["job_id"] for t in tasks])
        except Exception:  # noqa: BLE001 - fall back to the copies in the history
            live = {}
    rows = []
    for t in tasks:
        job = live.get(t["job_id"]) or t["job"] or {"JobId": t["job_id"]}
        rows.append(add_usage(stage_row(t["stage"], job, prices), stage_usage(logs_client, job)))
    return rows


def totals(rows: list) -> dict:
    def total(key):
        values = [r[key] for r in rows]
        return None if not values or any(v is None for v in values) else sum(values)

    return {"wait_seconds": total("wait_seconds"), "run_seconds": total("run_seconds"), "cost_usd": total("cost_usd")}
