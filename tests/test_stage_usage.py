"""Per-stage resource use (#59): measured by the job (app/metrics.py), recorded by timed_stage (tasks.py),
read back by query_pipeline_duration --stages (scripts/stage_breakdown.py). Nothing reaches AWS."""

import json
import subprocess
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

import metrics
import provenance
import query_pipeline_duration
import stage_breakdown as sb
import tasks
from test_stage_breakdown import SFN_ARN, paged_sfn, task_event

# ---------- measuring (app/metrics.py) ----------


class FakeResource:
    """Stands in for the resource module: getrusage values set per test, so this runs on Windows too."""

    RUSAGE_SELF, RUSAGE_CHILDREN = 0, -1

    def __init__(self):
        self.values = {0: (1.0, 0.5, 100 * 1024), -1: (2.0, 1.0, 900 * 1024)}  # utime, stime, maxrss KiB

    def getrusage(self, who):
        utime, stime, maxrss = self.values[who]
        return SimpleNamespace(ru_utime=utime, ru_stime=stime, ru_maxrss=maxrss)


@pytest.fixture
def fake_resource(monkeypatch):
    fake = FakeResource()
    monkeypatch.setattr(metrics, "resource", fake)
    monkeypatch.setattr(metrics.sys, "platform", "linux")
    return fake


def test_usage_is_cpu_since_the_snapshot_and_the_largest_process_peak(fake_resource):
    snapshot = metrics.usage_snapshot()
    assert snapshot == {"cpu_seconds": 4.5}
    fake_resource.values[-1] = (12.0, 3.0, 2048 * 1024)  # the stage's tools: 12 s user, 3 s system, 2 GiB
    assert metrics.usage_since(snapshot) == {"cpu_seconds": 12.0, "peak_memory_mib": 2048.0}  # 3 s -> 15 s


def test_macos_maxrss_is_in_bytes(fake_resource, monkeypatch):
    monkeypatch.setattr(metrics.sys, "platform", "darwin")
    fake_resource.values[-1] = (0, 0, 512 * 2**20)
    assert metrics.usage_since(metrics.usage_snapshot())["peak_memory_mib"] == 512.0


def test_no_usage_without_getrusage(monkeypatch):
    monkeypatch.setattr(metrics, "resource", None)
    assert metrics.usage_snapshot() is None and metrics.usage_since(None) is None


@pytest.mark.skipif(sys.platform == "win32", reason="getrusage is Unix-only")
def test_real_getrusage_counts_a_finished_childs_cpu_and_memory():
    # Only the child can account for these: it burns ~1 s of CPU while this process waits, and touches
    # more memory than this process has ever held, so peak_memory_mib must be the child's.
    import resource

    own_peak_mib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    child_mib = int(own_peak_mib) + 200
    code = (
        "import time\n"
        "end = time.process_time() + 1.0\n"
        "while time.process_time() < end: pass\n"
        f"b = b'x' * ({child_mib} * 2**20)\n"  # written, so every page is resident
    )
    snapshot = metrics.usage_snapshot()
    subprocess.run([sys.executable, "-c", code], check=True)
    usage = metrics.usage_since(snapshot)
    assert usage["cpu_seconds"] >= 0.9, usage
    assert usage["peak_memory_mib"] >= child_mib, (usage, own_peak_mib)


# ---------- recording (metrics backends, timed_stage, run record) ----------

USAGE = {"cpu_seconds": 15.0, "peak_memory_mib": 2048.0}


def test_local_metrics_store_the_usage(tmp_path):
    path = tmp_path / "metrics.jsonl"
    metrics.LocalMetrics(str(path)).record("Align", "S1", "Success", 1.0, usage={**USAGE, "other": 1})
    row = json.loads(path.read_text())
    assert {k: row[k] for k in USAGE} == USAGE and "other" not in row


def test_cloudwatch_sends_usage_next_to_duration():
    from botocore.stub import ANY, Stubber

    m = metrics.CloudWatchMetrics("eu-west-2")
    stub = Stubber(m._client)
    sent = {}
    stub.add_response("put_metric_data", {}, {"Namespace": "GeyserGenomics", "MetricData": ANY})
    original = m._client.put_metric_data

    def capture(**kwargs):
        sent.update(kwargs)
        return original(**kwargs)

    m._client.put_metric_data = capture
    with stub:
        m.record("Align", "S1", "Success", 1.5, usage={"cpu_seconds": 15.0, "peak_memory_mib": 2048.0})
    names = [(d["MetricName"], d["Value"], d["Unit"]) for d in sent["MetricData"]]
    assert names == [("Duration", 1.5, "Seconds"), ("CpuSeconds", 15.0, "Seconds"), ("PeakMemory", 2048.0, "Megabytes")]
    assert all(d["Dimensions"] == sent["MetricData"][0]["Dimensions"] for d in sent["MetricData"])


def test_timed_stage_logs_records_and_stores_the_usage(local_env, monkeypatch, capsys):
    monkeypatch.setattr(tasks, "usage_since", lambda snapshot: dict(USAGE))
    recorded = {}

    class Recorder:
        def record(self, task_name, sample_id, status, duration, usage=None):
            recorded.update(task=task_name, usage=usage)

    monkeypatch.setattr(tasks, "_metrics", Recorder())
    assert tasks.timed_stage("Example")(lambda sample_id: "done")("S1") == "done"
    line = next(ln for ln in capsys.readouterr().out.splitlines() if ln.startswith(metrics.USAGE_LOG_PREFIX))
    assert sb.parse_usage_line(line) == USAGE
    assert recorded == {"task": "Example", "usage": USAGE}
    part = provenance._read_json(tasks.storage(), f"{provenance.part_prefix('S1')}Example.json")
    assert part["resources"] == USAGE
    assert provenance.read_record(tasks.storage(), "S1")["stages"]["Example"]["resources"] == USAGE


def test_a_measuring_error_does_not_fail_the_stage(local_env, monkeypatch, capsys):
    def broken(snapshot):
        raise OSError("no /proc")

    monkeypatch.setattr(tasks, "usage_since", broken)
    assert tasks.timed_stage("Example")(lambda sample_id: "done")("S1") == "done"
    out = capsys.readouterr().out
    assert "could not measure resource use" in out and metrics.USAGE_LOG_PREFIX not in out


# ---------- reading back (stage_breakdown, query_pipeline_duration --stages) ----------


def job(stream="geyser/default/abc", group="/aws/batch/geyser-dev"):
    return {
        "JobId": "j1",
        "Status": "SUCCEEDED",
        "CreatedAt": 1_790_000_000_000,
        "StartedAt": 1_790_000_010_000,
        "StoppedAt": 1_790_000_110_000,  # 100 s
        "Container": {
            "ResourceRequirements": [{"Type": "VCPU", "Value": "4"}, {"Type": "MEMORY", "Value": "8192"}],
            "LogStreamName": stream,
            "LogConfiguration": {"LogDriver": "awslogs", "Options": {"awslogs-group": group}},
        },
    }


def test_parse_usage_line_ignores_other_lines_and_fields():
    assert sb.parse_usage_line("--- 'Align' for 'S1': Success in 29.30s ---") is None
    assert sb.parse_usage_line(metrics.USAGE_LOG_PREFIX + "not json") is None
    line = metrics.USAGE_LOG_PREFIX + json.dumps({"task": "Align", "cpu_seconds": 200, "peak_memory_mib": "big"})
    assert sb.parse_usage_line(line) == {"cpu_seconds": 200}


def test_stage_usage_reads_the_jobs_own_stream_across_pages():
    logs = mock.Mock()
    line = metrics.USAGE_LOG_PREFIX + json.dumps(USAGE)
    logs.filter_log_events.side_effect = [{"events": [], "nextToken": "t"}, {"events": [{"message": line}]}]
    assert sb.stage_usage(logs, job()) == USAGE
    first, second = (c.kwargs for c in logs.filter_log_events.call_args_list)
    assert first["logGroupName"] == "/aws/batch/geyser-dev" and first["logStreamNames"] == ["geyser/default/abc"]
    assert first["startTime"] == 1_790_000_110_000 - sb.USAGE_SEARCH_WINDOW_MS  # only the stream's tail
    assert second["nextToken"] == "t"


def test_stage_usage_is_none_without_client_stream_or_logs():
    assert sb.stage_usage(None, job()) is None
    logs = mock.Mock()
    assert sb.stage_usage(logs, job(stream=None)) is None
    logs.filter_log_events.side_effect = RuntimeError("ResourceNotFoundException")
    assert sb.stage_usage(logs, job()) is None


def test_job_log_group_defaults_to_batchs_own():
    assert sb.job_log_group({"container": {}}) == sb.DEFAULT_JOB_LOG_GROUP


def test_shares_of_paid_cpu_and_memory():
    row = sb.add_usage(sb.stage_row("Align_Genome", job()), USAGE)
    assert row["cpu_use"] == round(15.0 / (100 * 4), 3)
    assert row["memory_use"] == 0.25  # 2 GiB of 8
    row = sb.add_usage(sb.stage_row("Align_Genome", job()), None)
    assert row["cpu_seconds"] is None and row["cpu_use"] is None and row["memory_use"] is None


def test_query_cli_shows_usage_columns(tmp_path, capsys):
    from datetime import UTC, datetime, timedelta

    events = [
        {"type": "ExecutionStarted"},
        {"type": "TaskStateEntered", "stateEnteredEventDetails": {"name": "Align_Genome"}},
        task_event("TaskSubmitted", {"JobId": "j1"}),
        task_event("TaskSucceeded", job()),
        {"type": "ExecutionSucceeded"},
    ]
    sfn = paged_sfn(events, page_size=100)
    start = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    sfn.list_executions.return_value = {"executions": [{"executionArn": "exec-1", "name": "run-1"}]}
    sfn.describe_execution.return_value = {
        "startDate": start,
        "stopDate": start + timedelta(minutes=2),
        "status": "SUCCEEDED",
        "input": json.dumps({"srr_id": "S1"}),
    }
    batch = mock.Mock()
    batch.describe_jobs.return_value = {"jobs": []}
    logs = mock.Mock()
    logs.filter_log_events.return_value = {"events": [{"message": metrics.USAGE_LOG_PREFIX + json.dumps(USAGE)}]}
    out_csv = tmp_path / "stages.csv"
    query_pipeline_duration.main(
        ["--state-machine-arn", SFN_ARN, "--stages-csv", str(out_csv)],
        sfn_client=sfn,
        batch_client=batch,
        logs_client=logs,
    )
    out = capsys.readouterr().out
    assert "PeakMiB" in out and "Mem%" in out
    align = next(ln for ln in out.splitlines() if ln.strip().startswith("Align_Genome"))
    assert "15.0" in align and "4%" in align and "2048" in align and "25%" in align
    import csv

    header, row = list(csv.reader(out_csv.open(newline="")))
    values = dict(zip(header, row, strict=True))
    assert values["CpuSeconds"] == "15.0" and values["PeakMemoryMiB"] == "2048.0" and values["MemoryUse"] == "0.25"


def test_cloudwatch_sends_gpu_usage_with_its_units():
    # DeepVariant's step metrics on a GPU (#139)
    from botocore.stub import ANY, Stubber

    m = metrics.CloudWatchMetrics("eu-west-2")
    stub = Stubber(m._client)
    sent = {}
    stub.add_response("put_metric_data", {}, {"Namespace": "GeyserGenomics", "MetricData": ANY})
    original = m._client.put_metric_data
    m._client.put_metric_data = lambda **kwargs: sent.update(kwargs) or original(**kwargs)
    usage = {"gpu_utilization_mean_pct": 90.0, "gpu_utilization_peak_pct": 100.0, "gpu_memory_peak_mib": 9600.0}
    with stub:
        m.record("DeepVariant.CallVariants", "S1", "Success", 20.5, usage=usage)
    names = [(d["MetricName"], d["Value"], d["Unit"]) for d in sent["MetricData"]]
    assert names == [
        ("Duration", 20.5, "Seconds"),
        ("GpuUtilization", 90.0, "Percent"),
        ("GpuUtilizationPeak", 100.0, "Percent"),
        ("GpuPeakMemory", 9600.0, "Megabytes"),
    ]
