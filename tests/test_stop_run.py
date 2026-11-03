"""scripts/stop_run.py with stubbed Step Functions and Batch clients (botocore Stubber): no AWS calls.
A Stubber fails on any call it wasn't given, so these also prove --dry-run only reads."""

import os
import re

import boto3
import pytest
from botocore.stub import Stubber

import stop_run

SM = "arn:aws:states:eu-west-2:000000000000:stateMachine:geyser-pipeline-sfn-dev"
NAME = "S1-20261102T200023Z-1d9aceed"
ARN = "arn:aws:states:eu-west-2:000000000000:execution:geyser-pipeline-sfn-dev:" + NAME
QUEUES = ["geyser-job-queue", "geyser-gpu-queue-dev"]
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def clients():
    sfn = boto3.client("stepfunctions", region_name="eu-west-2", aws_access_key_id="x", aws_secret_access_key="x")
    batch = boto3.client("batch", region_name="eu-west-2", aws_access_key_id="x", aws_secret_access_key="x")
    return sfn, Stubber(sfn), batch, Stubber(batch)


def job(job_id, name):
    return {"jobId": job_id, "jobName": name, "jobArn": f"arn:aws:batch:eu-west-2:0:job/{job_id}", "createdAt": 1}


# This run's jobs in both queues, plus jobs that must never be touched: another run, and one whose name only
# contains this run's name somewhere other than at the end.
JOBS = {
    ("geyser-job-queue", "RUNNABLE"): [job("j1", f"CallVariantsShard-0-{NAME}")],
    ("geyser-job-queue", "RUNNING"): [
        job("j2", f"AlignGenome-S1-{NAME}"),
        job("j9", "AlignGenome-S2-S2-20261102T210000Z-aaaaaaaa"),
        job("j8", f"AlignGenome-{NAME}-copy"),
    ],
    ("geyser-gpu-queue-dev", "STARTING"): [job("j3", f"DeepVariant-S1-{NAME}")],
}


def expect_reads(sfn_stub, batch_stub, status="RUNNING", page_size=None):
    sfn_stub.add_response("describe_execution", _execution(status), {"executionArn": ARN})
    batch_stub.add_response(
        "describe_job_queues",
        {"jobQueues": [_queue(q) for q in QUEUES]},
        {},
    )
    for queue in QUEUES:
        for state in stop_run.ACTIVE_JOB_STATES:
            jobs = JOBS.get((queue, state), [])
            if page_size and len(jobs) > page_size:  # the first page with a token, then the rest
                batch_stub.add_response(
                    "list_jobs",
                    {"jobSummaryList": jobs[:page_size], "nextToken": "t"},
                    {"jobQueue": queue, "jobStatus": state},
                )
                batch_stub.add_response(
                    "list_jobs",
                    {"jobSummaryList": jobs[page_size:]},
                    {"jobQueue": queue, "jobStatus": state, "nextToken": "t"},
                )
            else:
                batch_stub.add_response("list_jobs", {"jobSummaryList": jobs}, {"jobQueue": queue, "jobStatus": state})


def _execution(status):
    return {
        "executionArn": ARN,
        "stateMachineArn": SM,
        "status": status,
        "startDate": "2026-11-02T20:00:23Z",
    }


def _queue(name):
    return {
        "jobQueueName": name,
        "jobQueueArn": f"arn:aws:batch:eu-west-2:0:job-queue/{name}",
        "state": "ENABLED",
        "priority": 1,
        "computeEnvironmentOrder": [],
    }


def run(argv, sfn, batch):
    return stop_run.main(argv + ["--state-machine-arn", SM], sfn_client=sfn, batch_client=batch)


def test_execution_arn_from_name_or_arn():
    assert stop_run.execution_arn(NAME, SM) == ARN
    assert stop_run.execution_arn(ARN) == ARN
    with pytest.raises(ValueError, match="SFN_ARN"):
        stop_run.execution_arn(NAME)
    with pytest.raises(ValueError, match="not an execution ARN"):
        stop_run.execution_arn(SM)


def test_dry_run_only_reads(capsys):
    sfn, sfn_stub, batch, batch_stub = clients()
    expect_reads(sfn_stub, batch_stub)
    with sfn_stub, batch_stub:  # no stop_execution / terminate_job stubbed: calling them would fail
        assert run([NAME, "--dry-run"], sfn, batch) == 0
    sfn_stub.assert_no_pending_responses()
    batch_stub.assert_no_pending_responses()
    out = capsys.readouterr().out
    assert f"Would stop execution {NAME}" in out
    assert out.count("Would terminate") == 3 and "j9" not in out and f"AlignGenome-{NAME}-copy" not in out


def test_stops_the_execution_then_terminates_only_its_active_jobs(capsys):
    sfn, sfn_stub, batch, batch_stub = clients()
    expect_reads(sfn_stub, batch_stub, page_size=1)  # also follows list_jobs pages
    sfn_stub.add_response(
        "stop_execution",
        {"stopDate": "2026-11-02T20:05:00Z"},
        {"executionArn": ARN, "error": "StoppedByOperator", "cause": stop_run.DEFAULT_REASON},
    )
    for job_id in ("j1", "j2", "j3"):
        batch_stub.add_response("terminate_job", {}, {"jobId": job_id, "reason": stop_run.DEFAULT_REASON})
    with sfn_stub, batch_stub:
        assert run([NAME], sfn, batch) == 0
    sfn_stub.assert_no_pending_responses()
    batch_stub.assert_no_pending_responses()
    assert capsys.readouterr().out.count("Terminating") == 3


def test_a_finished_execution_still_gets_its_leftover_jobs_terminated(capsys):
    # The case this script exists for: the execution was stopped, its .sync jobs kept running.
    sfn, sfn_stub, batch, batch_stub = clients()
    expect_reads(sfn_stub, batch_stub, status="ABORTED")
    for job_id in ("j1", "j2", "j3"):
        batch_stub.add_response("terminate_job", {}, {"jobId": job_id, "reason": stop_run.DEFAULT_REASON})
    with sfn_stub, batch_stub:
        assert run([NAME], sfn, batch) == 0
    out = capsys.readouterr().out
    assert "already ABORTED" in out and out.count("Terminating") == 3


def test_a_missing_execution_is_exit_1(capsys):
    sfn, sfn_stub, batch, batch_stub = clients()
    sfn_stub.add_client_error("describe_execution", service_error_code="ExecutionDoesNotExist")
    with sfn_stub, batch_stub:
        assert run([NAME, "--dry-run"], sfn, batch) == 1
    assert "No execution" in capsys.readouterr().out


def test_a_job_that_cannot_be_terminated_is_exit_2_and_the_rest_still_are(capsys):
    sfn, sfn_stub, batch, batch_stub = clients()
    expect_reads(sfn_stub, batch_stub, status="SUCCEEDED")
    batch_stub.add_response("terminate_job", {}, {"jobId": "j1", "reason": stop_run.DEFAULT_REASON})
    batch_stub.add_client_error("terminate_job", service_error_code="ClientException")
    batch_stub.add_response("terminate_job", {}, {"jobId": "j3", "reason": stop_run.DEFAULT_REASON})
    with sfn_stub, batch_stub:
        assert run([NAME], sfn, batch) == 2
    batch_stub.assert_no_pending_responses()
    assert "could not terminate j2" in capsys.readouterr().out


def test_nothing_running_is_exit_0(capsys):
    sfn, sfn_stub, batch, batch_stub = clients()
    sfn_stub.add_response("describe_execution", _execution("SUCCEEDED"), {"executionArn": ARN})
    batch_stub.add_response("describe_job_queues", {"jobQueues": [_queue(QUEUES[0])]}, {})
    for state in stop_run.ACTIVE_JOB_STATES:
        batch_stub.add_response("list_jobs", {"jobSummaryList": []}, {"jobQueue": QUEUES[0], "jobStatus": state})
    with sfn_stub, batch_stub:
        assert run([NAME], sfn, batch) == 0
    assert "No queued or running Batch jobs" in capsys.readouterr().out


def test_every_job_the_state_machine_names_ends_with_the_execution_name():
    # The suffix match relies on this: every Batch job name is '<Stage>-<sample or shard>-<execution name>'.
    with open(os.path.join(REPO, "infrastructure", "state_machine.asl.json"), encoding="utf-8") as fh:
        asl = fh.read()
    names = re.findall(r'"JobName\.\$": "([^"]+)"', asl)
    formats = [n for n in names if n.startswith("States.Format(")]
    assert formats, "no job name formats found"
    for fmt in formats:
        assert re.fullmatch(r"States\.Format\('[A-Za-z]+-\{\}-\{\}', \$[.\w]+, \$\$\.Execution\.Name\)", fmt), fmt
    # The other JobName.$ entries pass these formatted names on unchanged.
    assert all(n.startswith("States.Format(") or n == "$.batch_params.JobName" for n in names), names
