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
    monkeypatch.setattr(metrics, "container_peak_memory_mib", lambda: None)
    return fake


def test_usage_is_cpu_since_the_snapshot_and_the_largest_process_peak(fake_resource):
    snapshot = metrics.usage_snapshot()
    assert snapshot == {"cpu_seconds": 4.5}
    fake_resource.values[-1] = (12.0, 3.0, 2048 * 1024)  # the stage's tools: 12 s user, 3 s system, 2 GiB
    assert metrics.usage_since(snapshot) == {"cpu_seconds": 12.0, "peak_memory_mib": 2048.0}  # 3 s -> 15 s


def test_usage_includes_the_container_peak_when_the_cgroup_gives_one(fake_resource, monkeypatch):
    monkeypatch.setattr(metrics, "container_peak_memory_mib", lambda: 3000.5)
    assert metrics.usage_since(metrics.usage_snapshot())["container_peak_memory_mib"] == 3000.5


def test_macos_maxrss_is_in_bytes(fake_resource, monkeypatch):
    monkeypatch.setattr(metrics.sys, "platform", "darwin")
    fake_resource.values[-1] = (0, 0, 512 * 2**20)
    assert metrics.usage_since(metrics.usage_snapshot())["peak_memory_mib"] == 512.0


def test_no_usage_without_getrusage(monkeypatch):
    monkeypatch.setattr(metrics, "resource", None)
    assert metrics.usage_snapshot() is None and metrics.usage_since(None) is None


def test_container_peak_from_the_first_readable_cgroup_file(tmp_path):
    v1 = tmp_path / "max_usage_in_bytes"
    v1.write_text(f"{3 * 2**30}\n")
    bad = tmp_path / "memory.peak"
    bad.write_text("max\n")
    assert metrics.container_peak_memory_mib([str(tmp_path / "missing"), str(bad), str(v1)]) == 3072.0
    assert metrics.container_peak_memory_mib([str(tmp_path / "missing")]) is None


@pytest.mark.skipif(sys.platform == "win32", reason="getrusage is Unix-only")
def test_real_getrusage_counts_a_finished_child():
    snapshot = metrics.usage_snapshot()
    subprocess.run(
        [sys.executable, "-c", "sum(i * i for i in range(3_000_000)); b = bytearray(64 * 2**20)"], check=True
    )
    usage = metrics.usage_since(snapshot)
    assert usage["cpu_seconds"] > 0 and usage["peak_memory_mib"] >= 64


# ---------- recording (metrics backends, timed_stage, run record) ----------

USAGE = {"cpu_seconds": 15.0, "peak_memory_mib": 2048.0, "container_peak_memory_mib": 3000.5}


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
    assert row["memory_use"] == round(3000.5 / 8192, 3)  # the container peak when there is one
    row = sb.add_usage(sb.stage_row("Align_Genome", job()), {"cpu_seconds": 15.0, "peak_memory_mib": 2048.0})
    assert row["memory_use"] == 0.25
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
    assert "15.0" in align and "4%" in align and "3000" in align and "37%" in align
    import csv

    header, row = list(csv.reader(out_csv.open(newline="")))
    values = dict(zip(header, row, strict=True))
    assert values["CpuSeconds"] == "15.0" and values["ContainerPeakMemoryMiB"] == "3000.5"
