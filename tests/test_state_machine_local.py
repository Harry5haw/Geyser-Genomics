"""The real state machine definition, run in Step Functions Local (Docker) with Batch and SNS mocked.

The state machine was the one layer with no offline test. This runs infrastructure/state_machine.asl.json,
the file Terraform renders with templatefile(), in amazon/aws-stepfunctions-local. A MockConfigFile
answers every batch:submitJob.sync and sns:publish call, so nothing reaches AWS and no credentials are
needed. Each test case below picks success or failure per task state and checks the path taken.

Opt in with GEYSER_SFN_LOCAL=1 (needs Docker; pulls the image on first use, ~200 MB).
"""
import json
import os
import re
import socket
import subprocess
import time
import uuid

import pytest

from conftest import REPO_ROOT

pytestmark = pytest.mark.skipif(
    os.environ.get("GEYSER_SFN_LOCAL") != "1",
    reason="set GEYSER_SFN_LOCAL=1 to run the state machine in Step Functions Local (Docker)",
)

IMAGE = ("amazon/aws-stepfunctions-local@sha256:"
         "ea8de9d3c489f889582e8d84aab67031e613e055ff4b94ccc41aa3af681efce2")
ASL_PATH = os.path.join(REPO_ROOT, "infrastructure", "state_machine.asl.json")
MACHINE = "geyser-pipeline-local"
REGION = "eu-west-2"
ACCOUNT = "123456789012"  # Step Functions Local's fixed account
# Stand-ins for the three values step_functions.tf passes to templatefile().
TEMPLATE_VARS = {
    "job_definition": "geyser-app-job-def",
    "job_queue": "geyser-job-queue",
    "sns_topic_arn": f"arn:aws:sns:{REGION}:{ACCOUNT}:geyser-PipelineStatus-local",
}
BATCH_TASKS = ("Quality_Control", "Align_Genome", "Call_Variants")

OK, FAIL = "BatchSucceeded", "BatchFailed"
TEST_CASES = {
    "HappyPath": {"Quality_Control": OK, "Align_Genome": OK, "Call_Variants": OK},
    "QCFails": {"Quality_Control": FAIL, "Align_Genome": OK, "Call_Variants": OK, "Notify_Failure": "Published"},
    "AlignFails": {"Quality_Control": OK, "Align_Genome": FAIL, "Call_Variants": OK, "Notify_Failure": "Published"},
    "VariantsFail": {"Quality_Control": OK, "Align_Genome": OK, "Call_Variants": FAIL, "Notify_Failure": "Published"},
    "NotifyFails": {"Quality_Control": FAIL, "Align_Genome": OK, "Call_Variants": OK,
                    "Notify_Failure": "PublishDenied"},
}
MOCK_CONFIG = {
    "StateMachines": {MACHINE: {"TestCases": TEST_CASES}},
    "MockedResponses": {
        OK: {"0": {"Return": {"JobId": "job-ok", "JobName": "mocked", "Status": "SUCCEEDED"}}},
        FAIL: {"0": {"Throw": {"Error": "States.TaskFailed",
                               "Cause": "Essential container in task exited (exit code 1)"}}},
        "Published": {"0": {"Return": {"MessageId": "message-1"}}},
        "PublishDenied": {"0": {"Throw": {"Error": "SNS.AuthorizationErrorException", "Cause": "denied"}}},
    },
}


def render_definition() -> str:
    """templatefile() for this file: only ${name} placeholders, and every one must be filled."""
    with open(ASL_PATH, encoding="utf-8") as fh:
        text = fh.read()
    assert "%{" not in text and "$${" not in text, "templatefile directives/escapes are not handled here"
    placeholders = set(re.findall(r"\$\{(\w+)\}", text))
    assert placeholders == set(TEMPLATE_VARS), placeholders
    for name, value in TEMPLATE_VARS.items():
        text = text.replace("${" + name + "}", value)
    json.loads(text)
    return text


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def sfn(tmp_path_factory):
    import boto3
    from botocore.config import Config

    config_dir = tmp_path_factory.mktemp("sfn-local")
    (config_dir / "MockConfigFile.json").write_text(json.dumps(MOCK_CONFIG), encoding="utf-8")
    port = _free_port()
    name = f"geyser-sfn-local-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:8083",
         "--mount", f"type=bind,readonly,source={config_dir},target=/home/StepFunctionsLocal/mock",
         "-e", "SFN_MOCK_CONFIG=/home/StepFunctionsLocal/mock/MockConfigFile.json",
         "-e", f"AWS_DEFAULT_REGION={REGION}", IMAGE],
        check=True, capture_output=True)
    try:
        client = boto3.client(
            "stepfunctions", endpoint_url=f"http://127.0.0.1:{port}", region_name=REGION,
            aws_access_key_id="local", aws_secret_access_key="local",
            config=Config(retries={"max_attempts": 1}))
        deadline = time.time() + 60
        while True:
            try:
                client.list_state_machines()
                break
            except Exception:  # noqa: BLE001 - still starting
                if time.time() > deadline:
                    logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
                    pytest.fail(f"Step Functions Local did not start:\n{logs.stdout}{logs.stderr}")
                time.sleep(1)
        arn = client.create_state_machine(
            name=MACHINE, definition=render_definition(),
            roleArn=f"arn:aws:iam::{ACCOUNT}:role/DummyRole")["stateMachineArn"]
        yield client, arn
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def run_case(sfn, case, sample="S1", reference="chr20.fa"):
    """Start one mocked execution and wait for it. Returns (description, history events)."""
    client, arn = sfn
    execution = client.start_execution(
        stateMachineArn=f"{arn}#{case}", name=f"{case}-{uuid.uuid4().hex[:8]}",
        input=json.dumps({"srr_id": sample, "reference_name": reference}))["executionArn"]
    deadline = time.time() + 60
    while True:
        desc = client.describe_execution(executionArn=execution)
        if desc["status"] != "RUNNING":
            break
        assert time.time() < deadline, f"{case} still running after 60 s"
        time.sleep(0.5)
    events = client.get_execution_history(executionArn=execution, maxResults=1000)["events"]
    return desc, events


def failure_error(events):
    """The error the execution failed with (Step Functions Local leaves it out of describe_execution)."""
    failed = [e for e in events if e["type"] == "ExecutionFailed"]
    return failed[0]["executionFailedEventDetails"].get("error") if failed else None


def entered(events) -> list:
    return [e["stateEnteredEventDetails"]["name"] for e in events if "stateEnteredEventDetails" in e]


def batch_parameters(events) -> dict:
    """{task state: the Batch SubmitJob parameters it sent}, from the TaskScheduled events."""
    names = {e["id"]: e["stateEnteredEventDetails"]["name"] for e in events if e["type"] == "TaskStateEntered"}
    params = {}
    for e in events:
        if e["type"] == "TaskScheduled" and e["taskScheduledEventDetails"]["resourceType"] == "batch":
            params[names[e["previousEventId"]]] = json.loads(e["taskScheduledEventDetails"]["parameters"])
    return params


def test_template_has_only_the_terraform_variables():
    render_definition()


def test_happy_path_runs_qc_and_align_in_parallel_then_variants(sfn):
    desc, events = run_case(sfn, "HappyPath")
    assert desc["status"] == "SUCCEEDED"
    states = entered(events)
    assert "Notify_Failure" not in states and "Pipeline_Failed" not in states
    # Both branches start before either Batch job finishes, and variants only after both.
    first_done = min(i for i, e in enumerate(events) if e["type"] == "TaskSucceeded")
    starts = [i for i, e in enumerate(events) if e["type"] == "TaskStateEntered"
              and e["stateEnteredEventDetails"]["name"] in ("Quality_Control", "Align_Genome")]
    assert len(starts) == 2 and max(starts) < first_done
    parallel_exit = next(i for i, e in enumerate(events) if e["type"] == "ParallelStateExited")
    variants = next(i for i, e in enumerate(events) if e["type"] == "TaskStateEntered"
                    and e["stateEnteredEventDetails"]["name"] == "Call_Variants")
    assert parallel_exit < variants

    output = json.loads(desc["output"])
    assert output["parallel_output"] == {"qc": {"JobId": "job-ok", "Status": "SUCCEEDED"},
                                         "align": {"JobId": "job-ok", "Status": "SUCCEEDED"}}
    assert output["srr_id"] == "S1" and output["reference_name"] == "chr20.fa"


def test_each_stage_gets_the_command_tasks_py_expects(sfn):
    _, events = run_case(sfn, "HappyPath", sample="SAMPLE7", reference="ref.fa")
    params = batch_parameters(events)
    assert set(params) == set(BATCH_TASKS)
    commands = {state: p["ContainerOverrides"]["Command"] for state, p in params.items()}
    assert commands == {
        "Quality_Control": ["python", "tasks.py", "qc", "SAMPLE7"],
        "Align_Genome": ["python", "tasks.py", "align", "SAMPLE7", "ref.fa"],
        "Call_Variants": ["python", "tasks.py", "variants", "SAMPLE7", "ref.fa"],
    }
    for state, p in params.items():
        assert p["JobDefinition"] == TEMPLATE_VARS["job_definition"]
        assert p["JobQueue"] == TEMPLATE_VARS["job_queue"]
        assert re.fullmatch(r"(QualityControl|AlignGenome|CallVariants)-SAMPLE7-HappyPath-\w+", p["JobName"])
    timeouts = {state: p["Timeout"]["AttemptDurationSeconds"] for state, p in params.items()}
    assert timeouts == {"Quality_Control": 3600, "Align_Genome": 14400, "Call_Variants": 7200}


@pytest.mark.parametrize("case,failed_state", [
    ("QCFails", "Quality_Control"), ("AlignFails", "Align_Genome"), ("VariantsFail", "Call_Variants")])
def test_a_failed_stage_notifies_and_ends_failed(sfn, case, failed_state):
    desc, events = run_case(sfn, case)
    assert desc["status"] == "FAILED" and failure_error(events) == "PipelineStageFailed"
    states = entered(events)
    assert states[-2:] == ["Notify_Failure", "Pipeline_Failed"]
    if failed_state != "Call_Variants":
        assert "Call_Variants" not in states  # a failed branch stops the run before variants

    published = next(e for e in events if e["type"] == "TaskScheduled"
                     and e["taskScheduledEventDetails"]["resourceType"] == "sns")
    message = json.loads(published["taskScheduledEventDetails"]["parameters"])["Message"]
    assert message["Status"] == "FAILED"
    assert message["ErrorDetails"]["Error"] == "States.TaskFailed"
    assert message["Input"]["Execution"]["Input"]["srr_id"] == "S1"


def test_run_still_ends_failed_when_the_notification_fails(sfn):
    desc, events = run_case(sfn, "NotifyFails")
    assert desc["status"] == "FAILED" and failure_error(events) == "PipelineStageFailed"
    assert any(e["type"] == "TaskFailed" and e["taskFailedEventDetails"]["resourceType"] == "sns" for e in events)
