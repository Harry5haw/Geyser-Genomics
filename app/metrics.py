"""Stage timing metrics.

* ``CloudWatchMetrics`` - publishes to the ``GeyserGenomics`` namespace (read by the dashboard scripts).
* ``LocalMetrics``      - appends one JSON line per stage run to a file, for offline runs.

Select with ``METRICS_BACKEND=cloudwatch|local|none``. The default follows STORAGE_BACKEND:
S3 storage on AWS reports to CloudWatch, S3 storage on an S3-compatible service elsewhere
(``S3_ENDPOINT_URL``, see storage.py) records nothing, and local storage writes
``<LOCAL_STORAGE_DIR>/metrics/metrics.jsonl``. ``cloudwatch`` is boto3's CloudWatch client in
AWS_REGION: AWS, unless boto3's generic AWS_ENDPOINT_URL redirects every client.

Publishing is best effort: a metrics outage must never fail a pipeline stage.

Resource use per stage (#59): ``usage_snapshot()`` before a stage and ``usage_since()`` after it give
* ``cpu_seconds``: user + system CPU of this process and every tool it ran and waited for;
* ``peak_memory_mib``: the peak RSS of the largest single process (this one or one tool). Piped tools
  run at the same time, so the container can peak higher, up to their sum.
The container's cgroup peak (``memory.peak``) is deliberately not used: it counts the page cache, which
the stages fill with every file they stream, so it would show I/O-heavy stages near their memory limit
however little they needed. Peaks are high-water marks of the process, so they are exact when the
process runs one stage (always in Batch and run_local.py). A stage killed from outside (out of memory,
the Batch attempt timeout) never reaches the measurement and logs no usage; Batch's own status reason
says why it stopped. ``getrusage`` is Unix-only: on Windows there is no usage, only Duration.

Counts (``record_counts``): one value per kind under a single dimension, e.g. scripts/reconcile.py
``--publish-metrics`` (#O13): ``ReconcileProblems`` by ``Problem`` (``LoneMate``, ``Ignored`` ..., ``Total``).
Reconcile sends every kind each time, zeros included, so a graph shows 0 rather than a gap. ``unit`` defaults
to Count; the quality gate's values (tasks.publish_gate_values, O11: ``QualityGateValue`` by ``Check``) use
``None`` (percentages, depth and a ratio) and leave out what the gate couldn't measure. Locally they are one
JSON line each without task/sample_id/status, so the web UI's stage reader (web/samples/status.py) skips them.

DeepVariant's step metrics (``DeepVariant.CallVariants`` ...) also carry GPU utilisation and peak GPU memory
when the job had a GPU (#139; see deepvariant_task.py).
"""

import json
import os
import sys
import time

from storage import is_aws_endpoint, s3_endpoint_from_env

try:
    import resource
except ImportError:  # Windows: no getrusage (the unit tests still run there)
    resource = None

METRIC_NAMESPACE = "GeyserGenomics"
# The job log line query_pipeline_duration.py --stages reads back (scripts/stage_breakdown.py).
USAGE_LOG_PREFIX = "stage-usage: "
# usage key -> (CloudWatch metric name, unit)
USAGE_METRICS = {
    "cpu_seconds": ("CpuSeconds", "Seconds"),
    "peak_memory_mib": ("PeakMemory", "Megabytes"),
    # DeepVariant's steps on a GPU (#139, deepvariant_task.gpu_metric_usage): nvidia-smi's utilisation (share of
    # time a kernel ran) and the device's peak memory in use, sampled while the step ran.
    "gpu_utilization_mean_pct": ("GpuUtilization", "Percent"),
    "gpu_utilization_peak_pct": ("GpuUtilizationPeak", "Percent"),
    "gpu_memory_peak_mib": ("GpuPeakMemory", "Megabytes"),
}


def _cpu_seconds() -> float:
    total = 0.0
    for who in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN):
        ru = resource.getrusage(who)
        total += ru.ru_utime + ru.ru_stime
    return total


def _maxrss_mib(ru) -> float:
    # Linux reports KiB, macOS bytes.
    return ru.ru_maxrss / 2**20 if sys.platform == "darwin" else ru.ru_maxrss / 1024


def usage_snapshot():
    """The CPU time so far, to pass to usage_since(); None where getrusage doesn't exist."""
    if resource is None:
        return None
    return {"cpu_seconds": _cpu_seconds()}


def usage_since(snapshot):
    """Resource use since usage_snapshot() (see the module docstring), or None without getrusage."""
    if snapshot is None or resource is None:
        return None
    peak = max(_maxrss_mib(resource.getrusage(who)) for who in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN))
    return {
        "cpu_seconds": round(_cpu_seconds() - snapshot["cpu_seconds"], 2),
        "peak_memory_mib": round(peak, 1),
    }


class NullMetrics:
    def record(self, task_name: str, sample_id: str, status: str, duration_seconds: float, usage=None) -> None:
        pass

    def record_counts(self, metric_name: str, dimension: str, counts: dict, unit: str = "Count") -> None:
        pass


class LocalMetrics:
    def __init__(self, path: str):
        self.path = path

    def record(self, task_name, sample_id, status, duration_seconds, usage=None):
        entry = {
            "timestamp": time.time(),
            "task": task_name,
            "sample_id": sample_id,
            "status": status,
            "duration_seconds": round(duration_seconds, 3),
        }
        entry.update({k: v for k, v in (usage or {}).items() if k in USAGE_METRICS})
        self._append([entry])

    def record_counts(self, metric_name, dimension, counts, unit="Count"):
        now = time.time()
        self._append(
            [
                {
                    "timestamp": now,
                    "metric": metric_name,
                    "dimensions": {dimension: kind},
                    "value": value,
                    "unit": unit,
                }
                for kind, value in counts.items()
            ]
        )

    def _append(self, entries):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write("".join(json.dumps(e) + "\n" for e in entries))


class CloudWatchMetrics:
    """Publishes per stage, not per sample: dimensions TaskName + Status (FailureCount: TaskName only).

    SampleId was a dimension until Nov 2026 (option B, compared in #54): with it every sample was its own metric, so
    CloudWatch could not compute p50/p95 across runs and each new sample added billable metrics. The sample is
    still in the job log, the run record (runs/{id}.provenance.json) and the local metrics.jsonl.
    """

    def __init__(self, region: str):
        import boto3

        self._client = boto3.client("cloudwatch", region_name=region)

    def record(self, task_name, sample_id, status, duration_seconds, usage=None):
        dimensions = [
            {"Name": "TaskName", "Value": task_name},
            {"Name": "Status", "Value": status},
        ]
        data = [{"MetricName": "Duration", "Dimensions": dimensions, "Value": duration_seconds, "Unit": "Seconds"}]
        for key, (name, unit) in USAGE_METRICS.items():
            if (usage or {}).get(key) is not None:
                data.append({"MetricName": name, "Dimensions": dimensions, "Value": usage[key], "Unit": unit})
        if status == "Failure":
            data.append(
                {
                    "MetricName": "FailureCount",
                    "Dimensions": [{"Name": "TaskName", "Value": task_name}],
                    "Value": 1,
                    "Unit": "Count",
                }
            )
        self._client.put_metric_data(Namespace=METRIC_NAMESPACE, MetricData=data)

    def record_counts(self, metric_name, dimension, counts, unit="Count"):
        data = [
            {
                "MetricName": metric_name,
                "Dimensions": [{"Name": dimension, "Value": kind}],
                "Value": value,
                "Unit": unit,
            }
            for kind, value in counts.items()
        ]
        for start in range(0, len(data), 1000):  # PutMetricData takes at most 1,000 values per call
            self._client.put_metric_data(Namespace=METRIC_NAMESPACE, MetricData=data[start : start + 1000])


def metrics_from_env(env=None):
    env = os.environ if env is None else env
    storage_backend = env.get("STORAGE_BACKEND", "s3").lower()
    if storage_backend != "s3":
        default = "local"
    elif not is_aws_endpoint(s3_endpoint_from_env(env)):
        default = "none"  # an S3-compatible service outside AWS has no CloudWatch next to it
    else:
        default = "cloudwatch"
    backend = env.get("METRICS_BACKEND", default).lower()
    if backend == "none":
        return NullMetrics()
    if backend == "local":
        root = env.get("LOCAL_STORAGE_DIR", "local_bucket")
        return LocalMetrics(env.get("METRICS_FILE", os.path.join(root, "metrics", "metrics.jsonl")))
    if backend == "cloudwatch":
        return CloudWatchMetrics(env.get("AWS_REGION", "eu-west-2"))
    raise RuntimeError(f"Unknown METRICS_BACKEND {backend!r} (expected 'cloudwatch', 'local' or 'none')")
