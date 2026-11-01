"""Stage timing metrics.

* ``CloudWatchMetrics`` - publishes to the ``GeyserGenomics`` namespace (read by the dashboard scripts).
* ``LocalMetrics``      - appends one JSON line per stage run to a file, for offline runs.

Select with ``METRICS_BACKEND=cloudwatch|local|none``. The default follows STORAGE_BACKEND:
S3 storage reports to CloudWatch, local storage writes ``<LOCAL_STORAGE_DIR>/metrics/metrics.jsonl``.

Publishing is best effort: a metrics outage must never fail a pipeline stage.
"""

import json
import os
import time

METRIC_NAMESPACE = "GeyserGenomics"


class NullMetrics:
    def record(self, task_name: str, sample_id: str, status: str, duration_seconds: float) -> None:
        pass


class LocalMetrics:
    def __init__(self, path: str):
        self.path = path

    def record(self, task_name, sample_id, status, duration_seconds):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        entry = {
            "timestamp": time.time(),
            "task": task_name,
            "sample_id": sample_id,
            "status": status,
            "duration_seconds": round(duration_seconds, 3),
        }
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")


class CloudWatchMetrics:
    """Emits the same metric shape the original tasks.py did, so existing dashboards keep working."""

    def __init__(self, region: str):
        import boto3

        self._client = boto3.client("cloudwatch", region_name=region)

    def record(self, task_name, sample_id, status, duration_seconds):
        data = [{
            "MetricName": "Duration",
            "Dimensions": [
                {"Name": "TaskName", "Value": task_name},
                {"Name": "SampleId", "Value": sample_id},
                {"Name": "Status", "Value": status},
            ],
            "Value": duration_seconds,
            "Unit": "Seconds",
        }]
        if status == "Failure":
            data.append({
                "MetricName": "FailureCount",
                "Dimensions": [
                    {"Name": "TaskName", "Value": task_name},
                    {"Name": "SampleId", "Value": sample_id},
                ],
                "Value": 1,
                "Unit": "Count",
            })
        self._client.put_metric_data(Namespace=METRIC_NAMESPACE, MetricData=data)


def metrics_from_env(env=None):
    env = os.environ if env is None else env
    storage_backend = env.get("STORAGE_BACKEND", "s3").lower()
    default = "cloudwatch" if storage_backend == "s3" else "local"
    backend = env.get("METRICS_BACKEND", default).lower()
    if backend == "none":
        return NullMetrics()
    if backend == "local":
        root = env.get("LOCAL_STORAGE_DIR", "local_bucket")
        return LocalMetrics(env.get("METRICS_FILE", os.path.join(root, "metrics", "metrics.jsonl")))
    if backend == "cloudwatch":
        return CloudWatchMetrics(env.get("AWS_REGION", "eu-west-2"))
    raise RuntimeError(f"Unknown METRICS_BACKEND {backend!r} (expected 'cloudwatch', 'local' or 'none')")
