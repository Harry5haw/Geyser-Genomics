"""The real state machine definition, run in Step Functions Local (Docker) with Batch and SNS mocked.

The state machine was the one layer with no offline test. This runs infrastructure/state_machine.asl.json,
the file Terraform renders with templatefile(), in amazon/aws-stepfunctions-local. A MockConfigFile
answers every batch:submitJob.sync and sns:publish call, so nothing reaches AWS and no credentials are
needed. Each test case below picks success or failure per task state and checks the path taken.

Opt in with GEYSER_SFN_LOCAL=1 (needs Docker; pulls the image on first use, ~200 MB).
"""

import io
import json
import os
import re
import socket
import subprocess
import tarfile
import time
import uuid

import pytest

from conftest import REPO_ROOT

pytestmark = pytest.mark.skipif(
    os.environ.get("GEYSER_SFN_LOCAL") != "1",
    reason="set GEYSER_SFN_LOCAL=1 to run the state machine in Step Functions Local (Docker)",
)

IMAGE = "amazon/aws-stepfunctions-local@sha256:ea8de9d3c489f889582e8d84aab67031e613e055ff4b94ccc41aa3af681efce2"
ASL_PATH = os.path.join(REPO_ROOT, "infrastructure", "state_machine.asl.json")
MACHINE = "geyser-pipeline-local"
REGION = "eu-west-2"
ACCOUNT = "123456789012"  # Step Functions Local's fixed account
# Inside the container. The image runs as uid 1000 (stepfunctionslocal) and reads this file on every
# StartExecution, not at startup: if it can't, every mocked run fails with a bare HTTP 500.
MOCK_CONFIG_DIR = "/home/stepfunctionslocal"
READY_TIMEOUT_SECONDS = 90
# Stand-ins for the three values step_functions.tf passes to templatefile().
TEMPLATE_VARS = {
    "job_definition": "geyser-app-job-def",
    "job_queue": "geyser-job-queue",
    "sns_topic_arn": f"arn:aws:sns:{REGION}:{ACCOUNT}:geyser-PipelineStatus-local",
    # The defaults of variables.tf (stage_resources, pipeline_timeout_hours).
    "pipeline_timeout_seconds": "43200",
    "qc_vcpu": "1",
    "qc_memory_mb": "2048",
    "align_vcpu": "4",
    "align_memory_mb": "8192",
    "variants_vcpu": "2",
    "variants_memory_mb": "4096",
    # Cloud DeepVariant (deepvariant.tf, batch_gpu.tf).
    "deepvariant_job_definition": "geyser-deepvariant-job",
    "gpu_job_queue": "geyser-gpu-queue-dev",
}
BATCH_TASKS = ("Quality_Control", "Align_Genome", "Call_Variants")

OK, FAIL = "BatchSucceeded", "BatchFailed"
TEST_CASES = {
    "HappyPath": {"Quality_Control": OK, "Align_Genome": OK, "Call_Variants": OK},
    "QCFails": {"Quality_Control": FAIL, "Align_Genome": OK, "Call_Variants": OK, "Notify_Failure": "Published"},
    "AlignFails": {"Quality_Control": OK, "Align_Genome": FAIL, "Call_Variants": OK, "Notify_Failure": "Published"},
    "VariantsFail": {"Quality_Control": OK, "Align_Genome": OK, "Call_Variants": FAIL, "Notify_Failure": "Published"},
    "NotifyFails": {
        "Quality_Control": FAIL,
        "Align_Genome": OK,
        "Call_Variants": OK,
        "Notify_Failure": "PublishDenied",
    },
    # A Batch API error (e.g. throttled SubmitJob) on the first try, then success: retried, run succeeds.
    "AlignThrottledOnce": {"Quality_Control": OK, "Align_Genome": "ThrottledThenOk", "Call_Variants": OK},
    # Batch API errors every time: retried MaxAttempts times, then the run fails through Notify_Failure.
    "AlignAlwaysThrottled": {
        "Quality_Control": OK,
        "Align_Genome": "AlwaysThrottled",
        "Call_Variants": OK,
        "Notify_Failure": "Published",
    },
    # caller = deepvariant: DeepVariant on the GPU queue instead of bcftools...
    "DeepVariantOk": {"Quality_Control": OK, "Align_Genome": OK, "DeepVariant": OK, "Call_Variants": OK},
    # ...and bcftools when DeepVariant fails; the run still succeeds.
    "DeepVariantFallsBack": {"Quality_Control": OK, "Align_Genome": OK, "DeepVariant": FAIL, "Call_Variants": OK},
    # Both callers fail: the run fails through Notify_Failure as before.
    "BothCallersFail": {
        "Quality_Control": OK,
        "Align_Genome": OK,
        "DeepVariant": FAIL,
        "Call_Variants": FAIL,
        "Notify_Failure": "Published",
    },
}
MOCK_CONFIG = {
    "StateMachines": {MACHINE: {"TestCases": TEST_CASES}},
    "MockedResponses": {
        OK: {"0": {"Return": {"JobId": "job-ok", "JobName": "mocked", "Status": "SUCCEEDED"}}},
        FAIL: {
            "0": {"Throw": {"Error": "States.TaskFailed", "Cause": "Essential container in task exited (exit code 1)"}}
        },
        "Published": {"0": {"Return": {"MessageId": "message-1"}}},
        "PublishDenied": {"0": {"Throw": {"Error": "SNS.AuthorizationErrorException", "Cause": "denied"}}},
        "ThrottledThenOk": {
            "0": {"Throw": {"Error": "Batch.AWSBatchException", "Cause": "Too Many Requests"}},
            "1": {"Return": {"JobId": "job-ok", "JobName": "mocked", "Status": "SUCCEEDED"}},
        },
        "AlwaysThrottled": {"0-3": {"Throw": {"Error": "Batch.AWSBatchException", "Cause": "Too Many Requests"}}},
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


def _mock_config_tar() -> bytes:
    """MockConfigFile.json as a tar stream for `docker cp -`: root-owned, mode 0644.

    Copied in rather than bind-mounted, so host permissions don't matter. (A bind mount of a pytest
    temp dir, mode 0700 and owned by the CI runner's uid 1001, was unreadable to the container's
    uid 1000 on Linux: every StartExecution returned 500. Docker Desktop hid that locally.)
    """
    data = json.dumps(MOCK_CONFIG).encode("utf-8")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo("MockConfigFile.json")
        info.size, info.mode, info.uid, info.gid, info.mtime = len(data), 0o644, 0, 0, int(time.time())
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def container_logs(name, lines=40) -> str:
    logs = subprocess.run(["docker", "logs", "--tail", str(lines), name], capture_output=True, text=True)
    return logs.stdout + logs.stderr


def _start_probe(client, arn):
    """Start a mocked execution; True once one starts. This is the readiness check: the API answering
    is not enough, since the mock config is only read when an execution starts."""
    client.start_execution(
        stateMachineArn=f"{arn}#HappyPath",
        name=f"probe-{uuid.uuid4().hex[:8]}",
        input=json.dumps({"srr_id": "PROBE", "reference_name": "ref.fa"}),
    )
    return True


@pytest.fixture(scope="module")
def sfn():
    import boto3
    from botocore.config import Config

    port = _free_port()
    name = f"geyser-sfn-local-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        [
            "docker",
            "create",
            "--name",
            name,
            "-p",
            f"127.0.0.1:{port}:8083",
            "-e",
            f"SFN_MOCK_CONFIG={MOCK_CONFIG_DIR}/MockConfigFile.json",
            "-e",
            f"AWS_DEFAULT_REGION={REGION}",
            IMAGE,
        ],
        check=True,
        capture_output=True,
    )
    try:
        subprocess.run(
            ["docker", "cp", "-", f"{name}:{MOCK_CONFIG_DIR}/"],
            input=_mock_config_tar(),
            check=True,
            capture_output=True,
        )
        subprocess.run(["docker", "start", name], check=True, capture_output=True)
        client = boto3.client(
            "stepfunctions",
            endpoint_url=f"http://127.0.0.1:{port}",
            region_name=REGION,
            aws_access_key_id="local",
            aws_secret_access_key="local",
            config=Config(retries={"max_attempts": 1}),
        )

        # Ready = the API answers, the definition is accepted, and a mocked execution starts.
        deadline, arn, last_error = time.time() + READY_TIMEOUT_SECONDS, None, None
        while True:
            try:
                if arn is None:
                    client.list_state_machines()
                    arn = client.create_state_machine(
                        name=MACHINE, definition=render_definition(), roleArn=f"arn:aws:iam::{ACCOUNT}:role/DummyRole"
                    )["stateMachineArn"]
                if _start_probe(client, arn):
                    break
            except Exception as err:  # noqa: BLE001 - still starting, or a real problem shown below
                last_error = err
            if time.time() > deadline:
                pytest.fail(
                    f"Step Functions Local not ready after {READY_TIMEOUT_SECONDS} s "
                    f"({'no state machine' if arn is None else 'mocked execution would not start'}): "
                    f"{last_error}\n--- container log ---\n{container_logs(name)}"
                )
            time.sleep(1)
        yield client, arn, name
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def run_case(sfn, case, sample="S1", reference="chr20.fa", wait_seconds=60, caller=None):
    """Start one mocked execution and wait for it. Returns (description, history events)."""
    client, arn, name = sfn
    run_input = {"srr_id": sample, "reference_name": reference}
    if caller:
        run_input["caller"] = caller
    try:
        execution = client.start_execution(
            stateMachineArn=f"{arn}#{case}",
            name=f"{case}-{uuid.uuid4().hex[:8]}",
            input=json.dumps(run_input),
        )["executionArn"]
    except Exception as err:  # noqa: BLE001 - the 500s say nothing; the container log does
        pytest.fail(f"StartExecution failed for {case}: {err}\n--- container log ---\n{container_logs(name)}")
    deadline = time.time() + wait_seconds
    while True:
        desc = client.describe_execution(executionArn=execution)
        if desc["status"] != "RUNNING":
            break
        assert time.time() < deadline, f"{case} still running after {wait_seconds} s\n{container_logs(name)}"
        time.sleep(0.5)
    events = client.get_execution_history(executionArn=execution, maxResults=1000)["events"]
    return desc, events


def test_mock_config_is_readable_by_the_container_user():
    # The regression behind the first CI failure: whatever the host, the file must reach the
    # container as something its uid 1000 can read.
    with tarfile.open(fileobj=io.BytesIO(_mock_config_tar())) as tar:
        (member,) = tar.getmembers()
        assert member.name == "MockConfigFile.json" and member.mode & 0o004  # world-readable
        assert json.loads(tar.extractfile(member).read()) == MOCK_CONFIG


def failure_error(events):
    """The error the execution failed with (Step Functions Local leaves it out of describe_execution)."""
    failed = [e for e in events if e["type"] == "ExecutionFailed"]
    return failed[0]["executionFailedEventDetails"].get("error") if failed else None


def state_entered(events, name) -> dict:
    (event,) = [
        e
        for e in events
        if e.get("stateEnteredEventDetails", {}).get("name") == name and e["type"].endswith("StateEntered")
    ]
    return event


def ancestor(by_id, event, event_type):
    """The nearest earlier event of event_type on event's previousEventId chain, or None."""
    while event is not None:
        if event["type"] == event_type:
            return event
        event = by_id.get(event.get("previousEventId"))
    return None


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
    # Structure, not timing: with instant mocks either branch may finish before the other has
    # logged anything, so event order says nothing. The previousEventId links do: both branches
    # hang off the one ParallelStateStarted, and variants comes after the Parallel state exits.
    by_id = {e["id"]: e for e in events}
    (started,) = [e for e in events if e["type"] == "ParallelStateStarted"]
    heads = {
        e["stateEnteredEventDetails"]["name"]
        for e in events
        if e.get("previousEventId") == started["id"] and "stateEnteredEventDetails" in e
    }
    assert heads == {"Prepare_QC_Command", "Prepare_Align_Command"}
    for task in ("Quality_Control", "Align_Genome", "Call_Variants"):
        assert ancestor(by_id, state_entered(events, task), "ParallelStateStarted") is not None
    variants = state_entered(events, "Call_Variants")
    assert ancestor(by_id, variants, "ParallelStateExited") is not None
    assert ancestor(by_id, state_entered(events, "Quality_Control"), "ParallelStateExited") is None

    output = json.loads(desc["output"])
    assert output["parallel_output"] == {
        "qc": {"JobId": "job-ok", "Status": "SUCCEEDED"},
        "align": {"JobId": "job-ok", "Status": "SUCCEEDED"},
    }
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
    for p in params.values():
        assert p["JobDefinition"] == TEMPLATE_VARS["job_definition"]
        assert p["JobQueue"] == TEMPLATE_VARS["job_queue"]
        assert re.fullmatch(r"(QualityControl|AlignGenome|CallVariants)-SAMPLE7-HappyPath-\w+", p["JobName"])
    timeouts = {state: p["Timeout"]["AttemptDurationSeconds"] for state, p in params.items()}
    assert timeouts == {"Quality_Control": 3600, "Align_Genome": 14400, "Call_Variants": 7200}
    # Each stage asks Batch for its own size (variables.tf stage_resources), as strings.
    sizes = {
        state: {r["Type"]: r["Value"] for r in p["ContainerOverrides"]["ResourceRequirements"]}
        for state, p in params.items()
    }
    assert sizes == {
        "Quality_Control": {"VCPU": "1", "MEMORY": "2048"},
        "Align_Genome": {"VCPU": "4", "MEMORY": "8192"},
        "Call_Variants": {"VCPU": "2", "MEMORY": "4096"},
    }


def attempts(events, state) -> int:
    """How many times a task state called its service (TaskScheduled events), retries included."""
    names = {e["id"]: e["stateEnteredEventDetails"]["name"] for e in events if e["type"] == "TaskStateEntered"}
    by_id = {e["id"]: e for e in events}
    count = 0
    for e in events:
        if e["type"] != "TaskScheduled":
            continue
        cursor = e
        while cursor is not None and cursor["id"] not in names:
            cursor = by_id.get(cursor.get("previousEventId"))
        count += cursor is not None and names[cursor["id"]] == state
    return count


def history_text(events) -> str:
    """One line per event (id, previous id, type, state name, error) for assertion messages."""
    lines = []
    for e in events:
        details = e.get("stateEnteredEventDetails") or e.get("stateExitedEventDetails") or {}
        error = (e.get("taskFailedEventDetails") or {}).get("error", "")
        lines.append(
            f"{e['id']:>3} <- {e.get('previousEventId', ''):>3} {e['type']:<24} {details.get('name', '')} {error}"
        )
    return "execution history:\n" + "\n".join(lines)


def test_the_whole_run_has_a_time_limit():
    definition = json.loads(render_definition())
    assert definition["TimeoutSeconds"] == 12 * 3600


def test_batch_api_errors_are_retried_and_the_run_succeeds(sfn):
    desc, events = run_case(sfn, "AlignThrottledOnce")
    assert desc["status"] == "SUCCEEDED"
    assert attempts(events, "Align_Genome") == 2 and attempts(events, "Quality_Control") == 1


def test_persistent_batch_api_errors_give_up_after_max_attempts(sfn):
    # Back-off 10 s, 20 s, 40 s between the attempts: 70 s of waiting.
    desc, events = run_case(sfn, "AlignAlwaysThrottled", wait_seconds=150)
    assert desc["status"] == "FAILED" and failure_error(events) == "PipelineStageFailed"
    # first try + MaxAttempts retries. Once in CI (2 Nov 2026, push run of a docs-only commit whose
    # pull_request run passed) this counted 5; it never has locally. The message has the history so
    # a repeat shows where the extra TaskScheduled comes from.
    assert attempts(events, "Align_Genome") == 1 + 3, history_text(events)
    assert "Notify_Failure" in entered(events)


def test_a_failed_job_is_not_retried_by_the_state_machine(sfn):
    # States.TaskFailed means the job ran and failed. The job definition's retryStrategy already
    # retried it if the cause was infrastructure; an application failure (corrupt upload) fails fast.
    _, events = run_case(sfn, "AlignFails")
    assert attempts(events, "Align_Genome") == 1


def test_every_stage_gets_the_execution_name_as_run_name(sfn):
    # The stages put RUN_NAME in the run record (app/provenance.py) so its parts group by run.
    desc, events = run_case(sfn, "HappyPath")
    params = batch_parameters(events)
    assert set(params) == set(BATCH_TASKS)
    for state, p in params.items():
        assert p["ContainerOverrides"]["Environment"] == [{"Name": "RUN_NAME", "Value": desc["name"]}], state


@pytest.mark.parametrize(
    "case,failed_state",
    [("QCFails", "Quality_Control"), ("AlignFails", "Align_Genome"), ("VariantsFail", "Call_Variants")],
)
def test_a_failed_stage_notifies_and_ends_failed(sfn, case, failed_state):
    desc, events = run_case(sfn, case)
    assert desc["status"] == "FAILED" and failure_error(events) == "PipelineStageFailed"
    states = entered(events)
    assert states[-2:] == ["Notify_Failure", "Pipeline_Failed"]
    if failed_state != "Call_Variants":
        assert "Call_Variants" not in states  # a failed branch stops the run before variants

    published = next(
        e for e in events if e["type"] == "TaskScheduled" and e["taskScheduledEventDetails"]["resourceType"] == "sns"
    )
    message = json.loads(published["taskScheduledEventDetails"]["parameters"])["Message"]
    assert message["Status"] == "FAILED"
    assert message["ErrorDetails"]["Error"] == "States.TaskFailed"
    assert message["Input"]["Execution"]["Input"]["srr_id"] == "S1"


def test_run_still_ends_failed_when_the_notification_fails(sfn):
    desc, events = run_case(sfn, "NotifyFails")
    assert desc["status"] == "FAILED" and failure_error(events) == "PipelineStageFailed"
    assert any(e["type"] == "TaskFailed" and e["taskFailedEventDetails"]["resourceType"] == "sns" for e in events)


def test_without_a_caller_the_run_uses_bcftools_as_before(sfn):
    desc, events = run_case(sfn, "HappyPath", caller="bcftools")
    assert desc["status"] == "SUCCEEDED"
    assert "DeepVariant" not in entered(events) and "Call_Variants" in entered(events)


def test_caller_deepvariant_runs_deepvariant_on_the_gpu_queue(sfn):
    desc, events = run_case(sfn, "DeepVariantOk", sample="SAMPLE7", reference="ref.fa", caller="deepvariant")
    assert desc["status"] == "SUCCEEDED"
    states = entered(events)
    assert "DeepVariant" in states and "Call_Variants" not in states
    names = {e["id"]: e["stateEnteredEventDetails"]["name"] for e in events if e["type"] == "TaskStateEntered"}
    (dv,) = [
        json.loads(e["taskScheduledEventDetails"]["parameters"])
        for e in events
        if e["type"] == "TaskScheduled" and names.get(e["previousEventId"]) == "DeepVariant"
    ]
    assert dv["JobQueue"] == TEMPLATE_VARS["gpu_job_queue"]
    assert dv["JobDefinition"] == TEMPLATE_VARS["deepvariant_job_definition"]
    assert dv["ContainerOverrides"]["Command"] == [
        "/opt/geyser-venv/bin/python",
        "deepvariant_task.py",
        "SAMPLE7",
        "ref.fa",
    ]
    assert dv["ContainerOverrides"]["Environment"][0]["Name"] == "RUN_NAME"
    assert dv["Timeout"]["AttemptDurationSeconds"] == 3600


def test_a_failed_deepvariant_falls_back_to_bcftools(sfn):
    desc, events = run_case(sfn, "DeepVariantFallsBack", caller="deepvariant")
    assert desc["status"] == "SUCCEEDED"
    states = entered(events)
    assert states.index("DeepVariant") < states.index("Call_Variants")
    assert "Notify_Failure" not in states
    output = json.loads(desc["output"])
    assert output["deepvariant_error"]["Error"] == "States.TaskFailed"  # why the fallback happened


def test_both_callers_failing_fails_the_run(sfn):
    desc, events = run_case(sfn, "BothCallersFail", caller="deepvariant")
    assert desc["status"] == "FAILED" and failure_error(events) == "PipelineStageFailed"
    assert entered(events)[-2:] == ["Notify_Failure", "Pipeline_Failed"]
