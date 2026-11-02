"""Per-stage queue wait, run time and cost (scripts/stage_breakdown.py, query_pipeline_duration --stages).
Uses fake Step Functions / Batch clients; nothing reaches AWS."""
import csv
import json
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

import query_pipeline_duration
import stage_breakdown as sb

SFN_ARN = "arn:aws:states:eu-west-2:000000000000:stateMachine:geyser-pipeline-sfn-dev"
FARGATE = [{"Type": "VCPU", "Value": "2"}, {"Type": "MEMORY", "Value": "4096"}]
T0 = 1_790_000_000_000  # epoch ms


def entered(name):
    return {"type": "TaskStateEntered", "stateEnteredEventDetails": {"name": name}}


def task_event(kind, payload, resource_type="batch"):
    details_key, field = sb.BATCH_TASK_EVENTS[kind]
    return {"type": kind, details_key: {"resourceType": resource_type, field: json.dumps(payload)}}


def sfn_job(job_id, status, created, started, stopped, reqs=FARGATE):
    """Job description as Step Functions copies it (PascalCase)."""
    return {"JobId": job_id, "Status": status, "CreatedAt": created, "StartedAt": started, "StoppedAt": stopped,
            "Container": {"ResourceRequirements": reqs}}


def history():
    return [
        {"type": "ExecutionStarted"},
        {"type": "PassStateEntered", "stateEnteredEventDetails": {"name": "Prepare_Decompress_Command"}},
        entered("Decompress_SRA"),
        {"type": "TaskScheduled", "taskScheduledEventDetails": {"resourceType": "batch"}},
        task_event("TaskSubmitted", {"JobId": "j1", "JobName": "DecompressSRA-S1-x"}),
        task_event("TaskSucceeded", sfn_job("j1", "SUCCEEDED", T0, T0 + 30_000, T0 + 630_000)),
        entered("Quality_Control"),
        task_event("TaskSubmitted", {"JobId": "j2"}),
        task_event("TaskFailed", sfn_job("j2", "FAILED", T0 + 700_000, T0 + 1_000_000, T0 + 1_010_000)),
        entered("Notify_Failure"),
        task_event("TaskSucceeded", {"MessageId": "m"}, resource_type="sns"),
        {"type": "ExecutionFailed"},
    ]


def paged_sfn(events, page_size=5):
    pages = [events[i:i + page_size] for i in range(0, len(events), page_size)]
    client = mock.Mock()
    client.get_execution_history.side_effect = [
        {"events": page, **({"nextToken": str(i + 1)} if i + 1 < len(pages) else {})} for i, page in enumerate(pages)]
    return client


def test_batch_tasks_follow_history_across_pages():
    sfn = paged_sfn(history())
    tasks = sb.batch_tasks(sfn, "exec-1")
    assert [(t["stage"], t["job_id"], t["job"]["Status"]) for t in tasks] == [
        ("Decompress_SRA", "j1", "SUCCEEDED"), ("Quality_Control", "j2", "FAILED")]
    tokens = [c.kwargs.get("nextToken") for c in sfn.get_execution_history.call_args_list]
    assert tokens == [None, "1", "2"]


def test_rows_from_history_copies_when_batch_has_forgotten_the_jobs():
    batch = mock.Mock()
    batch.describe_jobs.return_value = {"jobs": []}
    rows = sb.execution_stages(paged_sfn(history()), batch, "exec-1")
    decompress, qc = rows
    assert decompress["wait_seconds"] == 30 and decompress["run_seconds"] == 600
    assert decompress["vcpu"] == 2 and decompress["memory_gb"] == 4
    assert decompress["cost_usd"] == pytest.approx(600 / 3600 * (2 * 0.04656 + 4 * 0.00511))
    assert qc["status"] == "FAILED" and qc["wait_seconds"] == 300 and qc["run_seconds"] == 10
    assert qc["cost_usd"] == pytest.approx(60 / 3600 * (2 * 0.04656 + 4 * 0.00511))  # 1-minute minimum
    batch.describe_jobs.assert_called_once_with(jobs=["j1", "j2"])


def test_live_batch_description_preferred():
    live = {"jobId": "j1", "status": "SUCCEEDED", "createdAt": T0, "startedAt": T0 + 5_000, "stoppedAt": T0 + 65_000,
            "container": {"resourceRequirements": [{"type": "VCPU", "value": "4"}, {"type": "MEMORY", "value": "8192"}]}}
    batch = mock.Mock()
    batch.describe_jobs.return_value = {"jobs": [live]}
    decompress = sb.execution_stages(paged_sfn(history()), batch, "exec-1")[0]
    assert (decompress["wait_seconds"], decompress["run_seconds"], decompress["vcpu"]) == (5, 60, 4)


def test_batch_error_falls_back_to_history():
    batch = mock.Mock()
    batch.describe_jobs.side_effect = RuntimeError("AccessDenied")
    assert sb.execution_stages(paged_sfn(history()), batch, "exec-1")[0]["run_seconds"] == 600


def test_gpu_jobs_have_no_fargate_cost():
    job = sfn_job("g1", "SUCCEEDED", T0, T0 + 1000, T0 + 3_601_000,
                  reqs=FARGATE + [{"Type": "GPU", "Value": "1"}])
    row = sb.stage_row("DeepVariant", job)
    assert row["gpus"] == 1 and row["cost_usd"] is None and row["run_seconds"] == 3600


def test_missing_timestamps_give_none_and_totals_stay_honest():
    running = sb.stage_row("Align_Genome", {"JobId": "j3", "Status": "RUNNING", "CreatedAt": T0, "StartedAt": T0 + 1000,
                                            "Container": {"ResourceRequirements": FARGATE}})
    assert running["wait_seconds"] == 1 and running["run_seconds"] is None and running["cost_usd"] is None
    done = sb.stage_row("Decompress_SRA", sfn_job("j1", "SUCCEEDED", T0, T0 + 2000, T0 + 62_000))
    assert sb.totals([done, running]) == {"wait_seconds": 3, "run_seconds": None, "cost_usd": None}
    assert sb.totals([done])["run_seconds"] == 60


def test_custom_prices():
    assert sb.fargate_cost(3600, 1, 1, sb.Prices(vcpu_hour=1.0, gb_hour=0.5)) == pytest.approx(1.5)


def test_describe_jobs_batches_of_100():
    batch = mock.Mock()
    batch.describe_jobs.return_value = {"jobs": []}
    sb.describe_jobs(batch, [f"j{i}" for i in range(150)] + [None, "j0"])
    assert [len(c.kwargs["jobs"]) for c in batch.describe_jobs.call_args_list] == [100, 50]


def test_query_cli_stage_breakdown_and_csv(tmp_path, capsys):
    start = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    sfn = paged_sfn(history(), page_size=100)
    sfn.list_executions.return_value = {"executions": [{"executionArn": "exec-1", "name": "run-1"}]}
    sfn.describe_execution.return_value = {"startDate": start, "stopDate": start + timedelta(minutes=17),
                                           "status": "FAILED", "input": json.dumps({"srr_id": "SRR001"})}
    batch = mock.Mock()
    batch.describe_jobs.return_value = {"jobs": []}
    out_csv = tmp_path / "stages.csv"
    query_pipeline_duration.main(["--state-machine-arn", SFN_ARN, "--status", "FAILED", "--stages-csv", str(out_csv)],
                                 sfn_client=sfn, batch_client=batch)
    out = capsys.readouterr().out
    assert "Stage breakdown" in out and "Decompress_SRA" in out and "Quality_Control" in out
    assert "Estimated cost per run" in out and "Queue wait per stage" in out
    with open(out_csv, newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == query_pipeline_duration.STAGE_CSV_HEADER
    assert [r[2] for r in rows[1:]] == ["Decompress_SRA", "Quality_Control"]


def test_query_cli_without_stages_does_not_touch_batch(capsys):
    start = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    sfn = mock.Mock()
    sfn.list_executions.return_value = {"executions": [{"executionArn": "exec-1", "name": "run-1"}]}
    sfn.describe_execution.return_value = {"startDate": start, "stopDate": start + timedelta(minutes=2),
                                           "status": "SUCCEEDED", "input": "{}"}
    batch = mock.Mock()
    query_pipeline_duration.main(["--state-machine-arn", SFN_ARN], sfn_client=sfn, batch_client=batch)
    batch.describe_jobs.assert_not_called()
    sfn.get_execution_history.assert_not_called()
    assert "Stage breakdown" not in capsys.readouterr().out


def test_failure_reason_from_last_attempt():
    job = {"jobId": "j9", "status": "FAILED", "statusReason": "Essential container in task exited",
           "attempts": [{"container": {"exitCode": 1}},
                        {"container": {"exitCode": 137, "reason": "OutOfMemoryError: Container killed due to memory usage",
                                       "logStreamName": "geyser/default/abc"}}],
           "container": {"logStreamName": "geyser/default/abc", "resourceRequirements": []}}
    row = sb.stage_row("Align_Genome", job)
    assert row["attempts"] == 2
    assert row["reason"] == ("Essential container in task exited; "
                             "OutOfMemoryError: Container killed due to memory usage (exit 137)")
    assert row["log_stream"] == "geyser/default/abc"


def test_failure_reason_without_attempts_and_for_success():
    assert sb.failure_reason({"Status": "FAILED", "StatusReason": "CannotPullContainerError"}) == \
        "CannotPullContainerError"
    assert sb.failure_reason({"Status": "FAILED", "Container": {"ExitCode": 2}}) == "exit 2"
    assert sb.failure_reason({"Status": "SUCCEEDED", "StatusReason": "Essential container in task exited"}) is None
    assert sb.stage_row("X", {"Status": "SUCCEEDED"})["attempts"] is None


def test_query_cli_prints_failure_reason(capsys):
    events = history()
    failed = sfn_job("j2", "FAILED", T0 + 700_000, T0 + 1_000_000, T0 + 1_010_000)
    failed.update({"StatusReason": "Essential container in task exited",
                   "Attempts": [{"Container": {"ExitCode": 1, "Reason": None}}]})
    events[8] = task_event("TaskFailed", failed)
    start = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    sfn = paged_sfn(events, page_size=100)
    sfn.list_executions.return_value = {"executions": [{"executionArn": "exec-1", "name": "run-1"}]}
    sfn.describe_execution.return_value = {"startDate": start, "stopDate": start + timedelta(minutes=17),
                                           "status": "FAILED", "input": json.dumps({"srr_id": "SRR001"})}
    batch = mock.Mock()
    batch.describe_jobs.return_value = {"jobs": []}
    query_pipeline_duration.main(["--state-machine-arn", SFN_ARN, "--status", "FAILED", "--stages"],
                                 sfn_client=sfn, batch_client=batch)
    assert "reason: Essential container in task exited (exit 1)" in capsys.readouterr().out


def test_fargate_cost_counts_ephemeral_storage_above_the_free_20_gib():
    prices = sb.Prices(vcpu_hour=1.0, gb_hour=0.5, storage_gb_hour=0.01)
    assert sb.fargate_cost(3600, 1, 1, prices, storage_gb=20) == pytest.approx(1.5)
    assert sb.fargate_cost(3600, 1, 1, prices, storage_gb=200) == pytest.approx(1.5 + 180 * 0.01)
    job = {"Container": {"ResourceRequirements": FARGATE, "EphemeralStorage": {"SizeInGiB": 120}}}
    assert sb.ephemeral_storage_gb(job) == 120 and sb.ephemeral_storage_gb({"Container": {}}) is None


def test_parallel_branches_are_credited_by_event_chain_not_order():
    # QC and align run in a Parallel state: both enter before either submits, and their events
    # interleave. Each event points back (previousEventId) to its own branch's earlier events.
    def ev(eid, prev, event):
        return {**event, "id": eid, "previousEventId": prev}

    events = [
        ev(1, 0, {"type": "ExecutionStarted"}),
        ev(2, 1, {"type": "ParallelStateEntered", "stateEnteredEventDetails": {"name": "QC_and_Align"}}),
        ev(3, 2, entered("Quality_Control")),
        ev(4, 2, entered("Align_Genome")),
        ev(5, 3, {"type": "TaskScheduled", "taskScheduledEventDetails": {"resourceType": "batch"}}),
        ev(6, 4, {"type": "TaskScheduled", "taskScheduledEventDetails": {"resourceType": "batch"}}),
        ev(7, 5, task_event("TaskSubmitted", {"JobId": "qc-job"})),
        ev(8, 6, task_event("TaskSubmitted", {"JobId": "align-job"})),
        ev(9, 7, task_event("TaskSucceeded", sfn_job("qc-job", "SUCCEEDED", T0, T0 + 1000, T0 + 61_000))),
        ev(10, 8, task_event("TaskSucceeded", sfn_job("align-job", "SUCCEEDED", T0, T0 + 2000, T0 + 92_000))),
    ]
    tasks = sb.batch_tasks(paged_sfn(events), "exec-1")
    assert [(t["stage"], t["job_id"], t["job"]["JobId"]) for t in tasks] == [
        ("Quality_Control", "qc-job", "qc-job"), ("Align_Genome", "align-job", "align-job")]
