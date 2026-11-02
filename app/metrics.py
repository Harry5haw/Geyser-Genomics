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
  run at the same time, so the container can peak higher, up to their sum;
* ``container_peak_memory_mib``: the container's own peak (cgroup ``memory.peak``), when the kernel
  exposes it. This is the number an out-of-memory kill is judged on. It covers the container's
  lifetime, which is one stage in Batch and in run_local.py.
Peaks are high-water marks of the process, so they are exact when the process runs one stage (always
in Batch and run_local.py). ``getrusage`` is Unix-only: on Windows there is no usage, only Duration.
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
CGROUP_PEAK_FILES = (
    "/sys/fs/cgroup/memory.peak",  # cgroup v2, kernel 5.19+
    "/sys/fs/cgroup/memory/memory.max_usage_in_bytes",  # cgroup v1
)
# usage key -> (CloudWatch metric name, unit)
USAGE_METRICS = {
    "cpu_seconds": ("CpuSeconds", "Seconds"),
    "peak_memory_mib": ("PeakMemory", "Megabytes"),
    "container_peak_memory_mib": ("ContainerPeakMemory", "Megabytes"),
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


def container_peak_memory_mib(paths=CGROUP_PEAK_FILES):
    """The container's peak memory from its cgroup, or None where no file gives it."""
    for path in paths:
        try:
            with open(path, encoding="ascii") as fh:
                return round(int(fh.read().strip()) / 2**20, 1)
        except (OSError, ValueError):
            continue
    return None


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
    usage = {
        "cpu_seconds": round(_cpu_seconds() - snapshot["cpu_seconds"], 2),
        "peak_memory_mib": round(peak, 1),
    }
    container_peak = container_peak_memory_mib()
    if container_peak is not None:
        usage["container_peak_memory_mib"] = container_peak
    return usage


class NullMetrics:
    def record(self, task_name: str, sample_id: str, status: str, duration_seconds: float, usage=None) -> None:
        pass


class LocalMetrics:
    def __init__(self, path: str):
        self.path = path

    def record(self, task_name, sample_id, status, duration_seconds, usage=None):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        entry = {
            "timestamp": time.time(),
            "task": task_name,
            "sample_id": sample_id,
            "status": status,
            "duration_seconds": round(duration_seconds, 3),
        }
        entry.update({k: v for k, v in (usage or {}).items() if k in USAGE_METRICS})
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")


class CloudWatchMetrics:
    """Emits the same metric shape the original tasks.py did, so existing dashboards keep working."""

    def __init__(self, region: str):
        import boto3

        self._client = boto3.client("cloudwatch", region_name=region)

    def record(self, task_name, sample_id, status, duration_seconds, usage=None):
        dimensions = [
            {"Name": "TaskName", "Value": task_name},
            {"Name": "SampleId", "Value": sample_id},
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
                    "Dimensions": [
                        {"Name": "TaskName", "Value": task_name},
                        {"Name": "SampleId", "Value": sample_id},
                    ],
                    "Value": 1,
                    "Unit": "Count",
                }
            )
        self._client.put_metric_data(Namespace=METRIC_NAMESPACE, MetricData=data)


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
