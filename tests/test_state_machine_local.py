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
# The same definition rendered with gate_mode = "fail" (variables.tf's default since #157; MACHINE is "warn").
MACHINE_FAIL = "geyser-pipeline-local-gate-fail"
# The same definition with publishing on (W3: the Publish state after every run).
MACHINE_PUBLISH = "geyser-pipeline-local-publish"
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
    "variants_vcpu": "1",
    "variants_memory_mb": "2048",
    "plan_vcpu": "0.25",
    "plan_memory_mb": "512",
    "merge_vcpu": "0.25",
    "merge_memory_mb": "512",
    # Cloud DeepVariant (deepvariant.tf, batch_gpu.tf).
    "deepvariant_job_definition": "geyser-deepvariant-job",
    # vg giraffe as the aligner (#146, giraffe.tf).
    "giraffe_job_definition": "geyser-giraffe-job",
    "gpu_job_queue": "geyser-gpu-queue-dev",
    # Scatter-gather (#108): the data lake, and the defaults of variant_region_window / variants_max_concurrency.
    "bucket": "geyser-data-lake-local",
    "variant_region_window": "50000000",
    "variants_max_concurrency": "4",
    # The quality gate (#144): warn here, so the warn paths stay tested; MACHINE_FAIL renders variables.tf's
    # default, fail (#157).
    "gate_mode": "warn",
    # The hosted site (W3): publishing off, as variables.tf's default. MACHINE_PUBLISH renders it on.
    "publish_enabled": "false",
    "publish_job_definition": "geyser-publish-job",
    "site_bucket": "",
    "site_distribution_id": "",
    # v1.3 the large route (genome_scale.tf) and the per-run parallelism: variables.tf's defaults, and the
    # maxima step_functions.tf derives from fargate_spot_max_vcpus = 48 (live, 4 Nov 2026).
    "large_job_definition": "geyser-app-large-job",
    "large_input_bytes": "5000000000",
    "genome_chunk_count": "24",
    "genome_align_concurrency": "5",
    "genome_merge_threads": "8",
    "align_concurrency_max": "5",  # (48 - qc 8) / chunk 8
    "calling_concurrency_max": "48",  # 48 / variants 1
    "merge_threads_max": "8",
    "preflight_vcpu": "8",
    "preflight_memory_mb": "16384",
    "qc_large_vcpu": "8",
    "qc_large_memory_mb": "16384",
    "chunk_vcpu": "8",
    "chunk_memory_mb": "32768",
    "align_merge_vcpu": "8",
    "align_merge_memory_mb": "16384",
    # v1.5 the DeepVariant split (deepvariant_cpu.tf): variables.tf's defaults, and what step_functions.tf derives
    # from fargate_spot_max_vcpus = 48 and dv_part_resources' 8 vCPUs (parts x 8 shards, 0..48 / 8 parts).
    "deepvariant_cpu_job_definition": "geyser-deepvariant-cpu-job",
    "deepvariant_split_parts": "1",
    "dv_parts_max": "6",
    "dv_shards_by_parts": "[0,8,16,24,32,40,48]",
    "dv_part_timeout_seconds": "14400",
    # v2.0: the GPU DeepVariant job's attempt limit (variables.tf's default; was a fixed 3600).
    "deepvariant_timeout_seconds": "14400",
}
PUBLISH_VARS = {"publish_enabled": "true", "site_bucket": "geyser-site-local", "site_distribution_id": "E2EXAMPLE"}
# Each Map's MaxConcurrencyPath (the run's setting): the real definition's, which this Step Functions Local image
# rejects ("Field 'MaxConcurrencyPath' is not supported"). for_sfn_local() swaps in the default as MaxConcurrency;
# test_each_map_reads_its_concurrency_from_the_run_settings pins the real definition.
CONCURRENCY_PATHS = {
    "$.run_settings.align_concurrency": "genome_align_concurrency",
    "$.run_settings.calling_concurrency": "variants_max_concurrency",
}


def for_sfn_local(definition: str) -> str:
    data = json.loads(definition)

    def swap(states):
        for state in states.values():
            path = state.pop("MaxConcurrencyPath", None)
            if path is not None:
                state["MaxConcurrency"] = int(TEMPLATE_VARS[CONCURRENCY_PATHS[path]])
            for branch in state.get("Branches", []):
                swap(branch["States"])
            if "Iterator" in state:
                swap(state["Iterator"]["States"])

    swap(data["States"])
    return json.dumps(data)


def fargate_memory_table() -> dict:
    """{vcpu: [min MiB, max MiB]} from resilience.tf's local.fargate_memory_mb (the one copy of the table)."""
    with open(os.path.join(REPO_ROOT, "infrastructure", "resilience.tf"), encoding="utf-8") as fh:
        tf = fh.read()
    block = tf[tf.index("fargate_memory_mb = {") :]
    block = block[: block.index("}")]
    return {v: [int(lo), int(hi)] for v, lo, hi in re.findall(r'"([\d.]+)"\s*=\s*\[(\d+),\s*(\d+)\]', block)}


def align_override_sizes(qc_vcpu=1, plan_vcpu=0.25, fargate_max_vcpus=8) -> list:
    """step_functions.tf local.align_override_sizes, the same rule: Fargate's valid pairs (0.25 vCPU takes
    512/1024/2048 MiB, the others 1 GiB steps), where max(qc, plan) + align fits in fargate_max_vcpus.
    Keys in Terraform's map order (lexical), as the plan renders them."""
    sizes = []
    for vcpu, (lo, hi) in sorted(fargate_memory_table().items()):
        options = [512, 1024, 2048] if vcpu == "0.25" else range(lo, hi + 1, 1024)
        if float(vcpu) + max(qc_vcpu, plan_vcpu) <= fargate_max_vcpus:
            sizes += [f"{vcpu}/{mb}" for mb in options]
    return sizes


# Per-run overrides (Run_Setting_Rules): the allowed align sizes and the window bounds (step_functions.tf locals).
TEMPLATE_VARS["align_sizes"] = json.dumps(align_override_sizes())
TEMPLATE_VARS["variant_region_window_min"] = "5000000"
TEMPLATE_VARS["variant_region_window_max"] = "250000000"
BATCH_TASKS = ("Quality_Control", "Align_Genome", "Plan_Regions", "Call_Shard", "Call_Variants", "Quality_Gate")
# What Read_Regions gets from S3 in the tests: the planner's JSON list, as GetObject returns it (a string).
REGIONS = ["chr20:1-50000000", "chr20:50000001-64444167", "chr21:1-46709983"]
# What Read_Manifest gets from S3 on the large route: the pre-flight's manifest (schema 1), three chunks.
MANIFEST = {
    "schema": 1,
    "sample_id": "S1",
    "layout": "paired",
    "reads": 300,
    "chunk_count": 3,
    "chunks": [
        {
            "index": i,
            "name": f"chunk-{i:03d}",
            "reads": 100,
            "aligned": f"intermediate/aligned/S1/run/chunk-{i:03d}.bam",
        }
        for i in range(3)
    ],
}
LARGE = 60_000_000_000  # input_bytes of a 30x genome (~53-60 GB): above large_input_bytes
# What Read_Gate_Verdict gets from S3: the verdict tasks.py gate stored (abridged), as a string.
GATE_REASONS = ["duplicate_pct 15.2 is above the maximum 6", "mean_depth 12.1 is below the minimum 20"]
VERDICTS = {
    "pass": {"schema": 1, "sample_id": "S1", "verdict": "pass", "reasons": [], "config": "built-in default"},
    "fail": {"schema": 1, "sample_id": "S1", "verdict": "fail", "reasons": GATE_REASONS, "config": "built-in default"},
    # The gate couldn't judge (e.g. the binary crashed): tasks.py stores this instead of a verdict.
    "none": {"sample_id": "S1", "verdict": None, "mode": "warn", "error": "geyser-gate exited -6"},
    # Not the gate's JSON: no verdict field, a fail without its reasons, not an object at all.
    "malformed": {"schema": 1},
    "reasonless": {"verdict": "fail"},
    "list": [],
}

OK, FAIL = "BatchSucceeded", "BatchFailed"
# Every case plans, reads and calls its regions unless it says otherwise.
SCATTER = {"Plan_Regions": OK, "Read_Regions": "RegionsRead", "Call_Shard": "ShardsOk", "Close_Run_Record": "Closed"}
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
    # v1.5 split mode (dv_parts > 1): the make_examples parts on the Fargate queue, then the GPU job...
    "DeepVariantSplitOk": {"Make_Examples_Part": "ShardsOk", "DeepVariant": OK},
    # ...and bcftools when a part fails (every part, as ShardsFail: see its note), without the GPU job.
    "DeepVariantSplitPartFails": {"Make_Examples_Part": "ShardsFail", "DeepVariant": OK},
    # v2.0 from_alignment: the sample's CRAM isn't in the data lake, so the run fails before any job.
    "AlignmentMissing": {"Check_Alignment": "NoSuchKey", "Notify_Failure": "Published"},
    "PlanFails": {"Plan_Regions": FAIL, "Notify_Failure": "Published"},
    # aligner is neither bwa-mem2 nor giraffe: the align branch fails before submitting anything.
    "AlignerUnknown": {"Notify_Failure": "Published"},
    "RegionsUnreadable": {"Read_Regions": "NoSuchKey", "Notify_Failure": "Published"},
    "ShardFails": {"Call_Shard": "ShardsFail", "Notify_Failure": "Published"},
    # The quality gate (#144). The warn machine and the fail machine get the same cases.
    "GateFailsVerdict": {"Read_Gate_Verdict": "VerdictFail", "Notify_Failure": "Published"},
    "GateNoVerdict": {"Read_Gate_Verdict": "VerdictNone", "Notify_Failure": "Published"},
    "GateJobFails": {"Quality_Gate": FAIL, "Notify_Failure": "Published"},
    "GateVerdictMissing": {"Read_Gate_Verdict": "NoSuchKey", "Notify_Failure": "Published"},
    "GateVerdictMalformed": {"Read_Gate_Verdict": "VerdictMalformed", "Notify_Failure": "Published"},
    "GateVerdictReasonless": {"Read_Gate_Verdict": "VerdictReasonless", "Notify_Failure": "Published"},
    "GateVerdictList": {"Read_Gate_Verdict": "VerdictList", "Notify_Failure": "Published"},
    # Closing the run record fails (S3 denied): best effort, the run still notifies and ends failed.
    "CloseFails": {"Quality_Control": FAIL, "Close_Run_Record": "CloseDenied", "Notify_Failure": "Published"},
    # The run's reference/<name> isn't in the data lake: the run fails before any job.
    "ReferenceMissing": {"Check_Reference": "NoSuchKey", "Notify_Failure": "Published"},
    # S3 errors on the HeadObject once (e.g. a 503), then answers: retried, the run goes on.
    "ReferenceFlakyOnce": {"Check_Reference": "ReferenceFlakyThenFound"},
    # The hosted site (W3), on MACHINE_PUBLISH: every run ends with Publish, succeeded or failed.
    "PublishOk": {"Publish": OK},
    "PublishFails": {"Publish": FAIL},
    "QCFailsThenPublished": {"Quality_Control": FAIL, "Notify_Failure": "Published", "Publish": OK},
    "QCFailsAndPublishFails": {"Quality_Control": FAIL, "Notify_Failure": "Published", "Publish": FAIL},
    # v1.3 the large route: QC alongside the pre-flight, the chunks, the merge. The manifest has three chunks.
    "LargeRouteOk": {
        "Quality_Control_Large": OK,
        "Preflight": OK,
        "Read_Manifest": "ManifestRead",
        "Align_Chunk": "ChunksOk",
        "Align_Merge": OK,
    },
    "PreflightFails": {
        "Quality_Control_Large": OK,
        "Preflight": FAIL,
        "Notify_Failure": "Published",
    },
    "ManifestMissing": {
        "Quality_Control_Large": OK,
        "Preflight": OK,
        "Read_Manifest": "NoSuchKey",
        "Notify_Failure": "Published",
    },
    "ChunkFails": {
        "Quality_Control_Large": OK,
        "Preflight": OK,
        "Read_Manifest": "ManifestRead",
        "Align_Chunk": "ChunksFail",
        "Notify_Failure": "Published",
    },
    "MergeFails": {
        "Quality_Control_Large": OK,
        "Preflight": OK,
        "Read_Manifest": "ManifestRead",
        "Align_Chunk": "ChunksOk",
        "Align_Merge": FAIL,
        "Notify_Failure": "Published",
    },
    "LargeQCFails": {
        "Quality_Control_Large": FAIL,
        "Preflight": OK,
        "Read_Manifest": "ManifestRead",
        "Align_Chunk": "ChunksOk",
        "Align_Merge": OK,
        "Notify_Failure": "Published",
    },
}
# Every case passes the gate unless it says otherwise.
GATE = {"Quality_Gate": OK, "Read_Gate_Verdict": "VerdictPass"}
# Every case finds its reference unless it says otherwise (Check_Reference: an S3 HeadObject), and, when it
# starts from_alignment, its CRAM (Check_Alignment, likewise).
REFERENCE = {"Check_Reference": "ReferenceFound", "Check_Alignment": "AlignmentFound"}
TEST_CASES = {
    name: {"Quality_Control": OK, "Align_Genome": OK, "Call_Variants": OK, **REFERENCE, **SCATTER, **GATE, **case}
    for name, case in TEST_CASES.items()
}
MOCK_CONFIG = {
    "StateMachines": {
        MACHINE: {"TestCases": TEST_CASES},
        MACHINE_FAIL: {"TestCases": TEST_CASES},
        MACHINE_PUBLISH: {"TestCases": TEST_CASES},
    },
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
        "RegionsRead": {"0": {"Return": {"Body": json.dumps(REGIONS), "ContentType": "application/json"}}},
        # The pre-flight's manifest (docs/design/genome-scale-app.md, schema 1), abridged to what the Map reads.
        "ManifestRead": {"0": {"Return": {"Body": json.dumps(MANIFEST), "ContentType": "application/json"}}},
        "ChunksOk": {"0-99": {"Return": {"JobId": "job-ok", "JobName": "mocked", "Status": "SUCCEEDED"}}},
        "ChunksFail": {
            "0-99": {
                "Throw": {"Error": "States.TaskFailed", "Cause": "Essential container in task exited (exit code 1)"}
            }
        },
        **{
            f"Verdict{kind.capitalize()}": {"0": {"Return": {"Body": json.dumps(v), "ContentType": "application/json"}}}
            for kind, v in VERDICTS.items()
        },
        "NoSuchKey": {
            "0-3": {"Throw": {"Error": "S3.NoSuchKeyException", "Cause": "The specified key does not exist."}}
        },
        "ShardsOk": {"0-99": {"Return": {"JobId": "job-ok", "JobName": "mocked", "Status": "SUCCEEDED"}}},
        "Closed": {"0": {"Return": {"ETag": '"etag-1"'}}},
        "ReferenceFound": {"0": {"Return": {"ContentLength": 65186437, "ETag": '"etag-ref"'}}},
        "AlignmentFound": {"0": {"Return": {"ContentLength": 17000000000, "ETag": '"etag-cram"'}}},
        "ReferenceFlakyThenFound": {
            "0": {"Throw": {"Error": "S3.S3Exception", "Cause": "Service Unavailable"}},
            "1": {"Return": {"ContentLength": 65186437, "ETag": '"etag-ref"'}},
        },
        "CloseDenied": {"0-3": {"Throw": {"Error": "S3.S3Exception", "Cause": "Access Denied"}}},
        # Every shard fails: Step Functions Local doesn't count mocked calls reliably across concurrent Map
        # iterations, so "the second call fails" usually never happened. The point (a failed shard stops the
        # run before the merge) is the same.
        "ShardsFail": {
            "0-99": {
                "Throw": {"Error": "States.TaskFailed", "Cause": "Essential container in task exited (exit code 1)"}
            }
        },
    },
}


# step_functions.tf's replace() on the rendered template: every "Comment" key, at any depth, with its trailing
# comma (#209: the deployed definition must stay under the provider's 64 KB scanner). The same RE2/Python regex.
COMMENT_KEY = r'"Comment":\s*"(?:[^"\\]|\\.)*",\s*'


def render_definition(*, keep_comments=False, **overrides) -> str:
    """What Terraform deploys: templatefile() for this file (only ${name} placeholders, every one filled), then
    the Comment keys removed as step_functions.tf does. keep_comments=True gives the rendered source as is."""
    with open(ASL_PATH, encoding="utf-8") as fh:
        text = fh.read()
    assert "%{" not in text and "$${" not in text, "templatefile directives/escapes are not handled here"
    placeholders = set(re.findall(r"\$\{(\w+)\}", text))
    assert placeholders == set(TEMPLATE_VARS), placeholders
    for name, value in {**TEMPLATE_VARS, **overrides}.items():
        text = text.replace("${" + name + "}", value)
    if not keep_comments:
        text = re.sub(COMMENT_KEY, "", text)
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
                        name=MACHINE,
                        definition=for_sfn_local(render_definition()),
                        roleArn=f"arn:aws:iam::{ACCOUNT}:role/DummyRole",
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


@pytest.fixture(scope="module")
def sfn_fail(sfn):
    """The definition rendered with gate_mode = "fail", in the same Step Functions Local container."""
    client, _, name = sfn
    arn = client.create_state_machine(
        name=MACHINE_FAIL,
        definition=for_sfn_local(render_definition(gate_mode="fail")),
        roleArn=f"arn:aws:iam::{ACCOUNT}:role/DummyRole",
    )["stateMachineArn"]
    return client, arn, name


@pytest.fixture(scope="module")
def sfn_publish(sfn):
    """The definition rendered with publishing on (W3), in the same Step Functions Local container."""
    client, _, name = sfn
    arn = client.create_state_machine(
        name=MACHINE_PUBLISH,
        definition=for_sfn_local(render_definition(**PUBLISH_VARS)),
        roleArn=f"arn:aws:iam::{ACCOUNT}:role/DummyRole",
    )["stateMachineArn"]
    return client, arn, name


_UNSET = object()


def run_case(
    sfn,
    case,
    sample="S1",
    reference="chr20.fa",
    wait_seconds=60,
    caller=None,
    gate_profile=_UNSET,
    overrides=_UNSET,
    aligner=_UNSET,
    input_bytes=_UNSET,
    from_alignment=_UNSET,
):
    """Start one mocked execution and wait for it. Returns (description, history events)."""
    client, arn, name = sfn
    run_input = {"srr_id": sample}
    if reference is not _UNSET:  # anything, even not a string: the state machine validates it
        run_input["reference_name"] = reference
    if caller:
        run_input["caller"] = caller
    if aligner is not _UNSET:  # anything, even not a string: the state machine validates it
        run_input["aligner"] = aligner
    if gate_profile is not _UNSET:  # anything, even not a string: the state machine validates it
        run_input["gate_profile"] = gate_profile
    if overrides is not _UNSET:  # likewise
        run_input["overrides"] = overrides
    if input_bytes is not _UNSET:  # likewise (the trigger passes the upload's size; Choose_Route reads it)
        run_input["input_bytes"] = input_bytes
    if from_alignment is not _UNSET:  # likewise (Choose_Start reads it)
        run_input["from_alignment"] = from_alignment
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


def batch_calls(events, state) -> list:
    """Every Batch SubmitJob parameter set one task state sent (a Map state calls its task once per item)."""
    names = {e["id"]: e["stateEnteredEventDetails"]["name"] for e in events if e["type"] == "TaskStateEntered"}
    return [
        json.loads(e["taskScheduledEventDetails"]["parameters"])
        for e in events
        if e["type"] == "TaskScheduled"
        and e["taskScheduledEventDetails"]["resourceType"] == "batch"
        and names.get(e["previousEventId"]) == state
    ]


def run_close(events) -> dict:
    """What Close_Run_Record sent to S3 (#155): its PutObject parameters, with the Body parsed."""
    names = {e["id"]: e["stateEnteredEventDetails"]["name"] for e in events if e["type"] == "TaskStateEntered"}
    (put,) = [
        e for e in events if e["type"] == "TaskScheduled" and names.get(e["previousEventId"]) == "Close_Run_Record"
    ]
    params = json.loads(put["taskScheduledEventDetails"]["parameters"])
    # The object itself, not a JSON string: the integration serialises it once (#205).
    assert isinstance(params["Body"], dict), params["Body"]
    return params


def assert_closed_before_notifying(desc, events, error="States.TaskFailed"):
    """The failure path wrote this run's closing part (with the Catch output as it is) before notifying."""
    states = entered(events)
    assert states.index("Prepare_Run_Close") < states.index("Close_Run_Record") < states.index("Notify_Failure")
    close = run_close(events)
    assert close["Bucket"] == TEMPLATE_VARS["bucket"] and close["Key"] == "runs/S1.provenance/Finalize.json"
    body = close["Body"]
    assert {k: body[k] for k in ("schema", "task", "sample_id", "run_name", "status")} == {
        "schema": 1,
        "task": "Finalize",
        "sample_id": "S1",
        "run_name": desc["name"],
        "status": "failed",
    }
    assert body["caught"]["Error"] == error and "Cause" in body["caught"] and body["finished"].endswith("Z")


def test_template_has_only_the_terraform_variables():
    render_definition()


def test_happy_path_runs_qc_and_align_in_parallel_then_variants(sfn):
    desc, events = run_case(sfn, "HappyPath")
    assert desc["status"] == "SUCCEEDED"
    states = entered(events)
    assert "Notify_Failure" not in states and "Pipeline_Failed" not in states
    assert "Close_Run_Record" not in states  # only failed runs are closed by the state machine
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
    assert heads == {"Prepare_QC_Command", "Choose_Aligner"}  # the align branch picks its aligner first (#146)
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
    # The planner runs after the Parallel state, as its own step.
    assert ancestor(by_id, state_entered(events, "Plan_Regions"), "ParallelStateExited") is not None
    assert output["plan_output"] == {"JobId": "job-ok", "Status": "SUCCEEDED"}
    assert output["plan"] == {"regions": REGIONS}
    assert output["srr_id"] == "S1" and output["reference_name"] == "chr20.fa"


def test_each_stage_gets_the_command_tasks_py_expects(sfn):
    _, events = run_case(sfn, "HappyPath", sample="SAMPLE7", reference="ref.fa")
    params = batch_parameters(events)
    assert set(params) == set(BATCH_TASKS)
    commands = {state: p["ContainerOverrides"]["Command"] for state, p in params.items()}
    window = TEMPLATE_VARS["variant_region_window"]
    del commands["Call_Shard"]  # one per region: checked in test_the_map_calls_every_region_once
    assert commands == {
        "Quality_Control": ["python", "tasks.py", "qc", "SAMPLE7"],
        "Align_Genome": ["python", "tasks.py", "align", "SAMPLE7", "ref.fa"],
        "Plan_Regions": [
            "python",
            "tasks.py",
            "regions",
            "ref.fa",
            "--window",
            window,
            "--output-key",
            f"intermediate/variant-regions/SAMPLE7/{params['Plan_Regions']['JobName'].split('-', 2)[2]}.json",
        ],
        "Call_Variants": ["python", "tasks.py", "variants", "SAMPLE7", "ref.fa", "--window", window],
        "Quality_Gate": ["python", "tasks.py", "gate", "SAMPLE7"],
    }
    for state, p in params.items():
        assert p["JobDefinition"] == TEMPLATE_VARS["job_definition"]
        assert p["JobQueue"] == TEMPLATE_VARS["job_queue"]
        if state != "Call_Shard":
            assert re.fullmatch(
                r"(QualityControl|AlignGenome|PlanRegions|CallVariants|QualityGate)-SAMPLE7-HappyPath-\w+", p["JobName"]
            )
    timeouts = {state: p["Timeout"]["AttemptDurationSeconds"] for state, p in params.items()}
    assert timeouts == {
        "Quality_Control": 3600,
        "Align_Genome": 14400,
        "Plan_Regions": 1800,
        "Call_Shard": 7200,
        "Call_Variants": 7200,
        "Quality_Gate": 900,
    }
    # Each stage asks Batch for its own size (variables.tf stage_resources), as strings.
    sizes = {
        state: {r["Type"]: r["Value"] for r in p["ContainerOverrides"]["ResourceRequirements"]}
        for state, p in params.items()
    }
    assert sizes == {
        "Quality_Control": {"VCPU": "1", "MEMORY": "2048"},
        "Align_Genome": {"VCPU": "4", "MEMORY": "8192"},
        "Plan_Regions": {"VCPU": "0.25", "MEMORY": "512"},  # its own size (#173): it only reads the reference index
        "Call_Shard": {"VCPU": "1", "MEMORY": "2048"},  # mpileup | call: 1.0-1.3 vCPU, <= 130 MiB on the real runs
        "Call_Variants": {"VCPU": "0.25", "MEMORY": "512"},  # the merge: concat, index, stats
        "Quality_Gate": {"VCPU": "1", "MEMORY": "2048"},  # QC's size: it parses a few small files
    }


# The Batch job name each task state sets (its Pass state's States.Format prefix).
JOB_NAME_PREFIXES = {
    "Quality_Control": "QualityControl-",
    "Align_Genome": "AlignGenome-",
    "Plan_Regions": "PlanRegions-",
    "Call_Shard": "CallVariantsShard-",
    "Call_Variants": "CallVariants-",
    "Quality_Gate": "QualityGate-",
}


COMPLETED = {"TaskSucceeded", "TaskFailed", "TaskTimedOut"}


def attempts(events, state) -> int:
    """How many times a Batch task state's job ran to an outcome (succeeded, failed or timed out).

    Counts completed attempts, not TaskScheduled: once MaxAttempts is used up, Step Functions Local
    can emit a stray extra TaskScheduled + TaskStarted that never completes, racing the Parallel
    state's failure (seen in #133's event histories). The state machine itself retries correctly.
    Each completion is tied to its attempt through previousEventId (TaskFailed -> TaskStarted ->
    TaskScheduled, which carries the job name the state sets).
    """
    prefix = JOB_NAME_PREFIXES[state]
    by_id = {e["id"]: e for e in events}
    count = 0
    for e in events:
        if e["type"] not in COMPLETED:
            continue
        cursor = e
        while cursor is not None and cursor["type"] != "TaskScheduled":
            cursor = by_id.get(cursor.get("previousEventId"))
        if cursor is None or cursor["taskScheduledEventDetails"]["resourceType"] != "batch":
            continue
        count += json.loads(cursor["taskScheduledEventDetails"]["parameters"])["JobName"].startswith(prefix)
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
        expected = [{"Name": "RUN_NAME", "Value": desc["name"]}]
        if state == "Quality_Gate":  # pinned: the job never fails on a verdict; the state machine decides
            expected.append({"Name": "GATE_MODE", "Value": "warn"})
            # No gate_profile in the input: the default thresholds (the app expands the variable).
            expected.append({"Name": "GATE_CONFIG", "Value": "$GATE_THRESHOLDS_DIR/default.toml"})
        assert p["ContainerOverrides"]["Environment"] == expected, state


@pytest.mark.parametrize(
    "case,failed_state",
    [("QCFails", "Quality_Control"), ("AlignFails", "Align_Genome"), ("VariantsFail", "Call_Variants")],
)
def test_a_failed_stage_notifies_and_ends_failed(sfn, case, failed_state):
    desc, events = run_case(sfn, case)
    assert desc["status"] == "FAILED" and failure_error(events) == "PipelineStageFailed"
    states = entered(events)
    # Not states[-2:]: a cancelled Parallel branch can log its last StateEntered after Notify_Failure.
    assert states[-1] == "Pipeline_Failed" and states.index("Notify_Failure") < states.index("Pipeline_Failed")
    if failed_state != "Call_Variants":
        assert "Call_Variants" not in states  # a failed branch stops the run before variants

    published = next(
        e for e in events if e["type"] == "TaskScheduled" and e["taskScheduledEventDetails"]["resourceType"] == "sns"
    )
    message = json.loads(published["taskScheduledEventDetails"]["parameters"])["Message"]
    assert message["Status"] == "FAILED"
    assert message["ErrorDetails"]["Error"] == "States.TaskFailed"
    assert message["Input"]["Execution"]["Input"]["srr_id"] == "S1"
    assert_closed_before_notifying(desc, events)


def test_run_still_ends_failed_when_the_notification_fails(sfn):
    desc, events = run_case(sfn, "NotifyFails")
    assert desc["status"] == "FAILED" and failure_error(events) == "PipelineStageFailed"
    assert any(e["type"] == "TaskFailed" and e["taskFailedEventDetails"]["resourceType"] == "sns" for e in events)


def test_without_a_caller_the_run_uses_bcftools_as_before(sfn):
    desc, events = run_case(sfn, "HappyPath", caller="bcftools")
    assert desc["status"] == "SUCCEEDED"
    assert "DeepVariant" not in entered(events) and "Plan_Regions" in entered(events)
    assert "Call_Variants" in entered(events)


def test_caller_deepvariant_runs_deepvariant_on_the_gpu_queue(sfn):
    desc, events = run_case(sfn, "DeepVariantOk", sample="SAMPLE7", reference="ref.fa", caller="deepvariant")
    assert desc["status"] == "SUCCEEDED"
    states = entered(events)
    assert "DeepVariant" in states and not {"Plan_Regions", "Call_Variants"} & set(states)
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
    # PIPELINE_CALLER: DeepVariant is this run's caller, so its PASS-call stats become the sample's (#147).
    assert dv["ContainerOverrides"]["Environment"] == [
        {"Name": "RUN_NAME", "Value": desc["name"]},
        {"Name": "PIPELINE_CALLER", "Value": "deepvariant"},
    ]
    # deepvariant_timeout_seconds (v2.0, was a fixed 3600): long enough for a genome's call_variants.
    assert dv["Timeout"]["AttemptDurationSeconds"] == 14400


def test_a_failed_deepvariant_falls_back_to_bcftools(sfn):
    desc, events = run_case(sfn, "DeepVariantFallsBack", caller="deepvariant")
    assert desc["status"] == "SUCCEEDED"
    states = entered(events)
    # The fallback is the whole bcftools path: plan, shards, merge.
    assert states.index("DeepVariant") < states.index("Plan_Regions") < states.index("Call_Variants")
    assert "Notify_Failure" not in states
    output = json.loads(desc["output"])
    assert output["deepvariant_error"]["Error"] == "States.TaskFailed"  # why the fallback happened


def test_both_callers_failing_fails_the_run(sfn):
    desc, events = run_case(sfn, "BothCallersFail", caller="deepvariant")
    assert desc["status"] == "FAILED" and failure_error(events) == "PipelineStageFailed"
    assert entered(events)[-3:] == ["Notify_Failure", "Publish_Run", "Pipeline_Failed"]


# --- v1.5 the DeepVariant split: make_examples parts on Fargate, then the GPU job (#228, #236) ----------------


@pytest.mark.parametrize("overrides", [_UNSET, {"dv_parts": 1}])
def test_dv_parts_1_is_the_single_gpu_job_as_before(sfn, overrides):
    desc, events = run_case(sfn, "DeepVariantOk", caller="deepvariant", overrides=overrides)
    assert desc["status"] == "SUCCEEDED"
    states = entered(events)
    assert states.index("Choose_DeepVariant_Mode") < states.index("Prepare_DeepVariant_Command")
    assert "Make_Examples_Parts" not in states and not batch_calls(events, "Make_Examples_Part")
    (dv,) = batch_calls(events, "DeepVariant")
    assert "--from-examples" not in dv["ContainerOverrides"]["Command"]


def test_split_mode_runs_one_cpu_part_per_dv_part_then_the_gpu_job_on_their_examples(sfn):
    desc, events = run_case(
        sfn, "DeepVariantSplitOk", sample="SAMPLE7", reference="ref.fa", caller="deepvariant", overrides={"dv_parts": 3}
    )
    assert desc["status"] == "SUCCEEDED"
    parts = batch_calls(events, "Make_Examples_Part")
    assert sorted(p["JobName"] for p in parts) == [f"DeepVariant-Examples-{i}-{desc['name']}" for i in range(3)]
    for p in parts:
        part = p["JobName"].split("-")[2]
        assert p["JobDefinition"] == TEMPLATE_VARS["deepvariant_cpu_job_definition"]
        assert p["JobQueue"] == TEMPLATE_VARS["job_queue"]  # the Fargate queue, Spot first
        assert p["Timeout"]["AttemptDurationSeconds"] == 14400
        assert p["ContainerOverrides"]["Command"] == [
            "/opt/geyser-venv/bin/python",
            "deepvariant_task.py",
            "SAMPLE7",
            "ref.fa",
            "--make-examples-part",
            part,
            "--parts",
            "3",
        ]
        # Every part gets the same N = parts x the part's 8 vCPUs.
        assert p["ContainerOverrides"]["Environment"] == [
            {"Name": "RUN_NAME", "Value": desc["name"]},
            {"Name": "PIPELINE_CALLER", "Value": "deepvariant"},
            {"Name": "DEEPVARIANT_SHARDS", "Value": "24"},
        ]
    # The GPU job comes after every part, on their examples, as the single job otherwise.
    by_id = {e["id"]: e for e in events}
    assert ancestor(by_id, state_entered(events, "DeepVariant"), "MapStateExited") is not None
    (dv,) = batch_calls(events, "DeepVariant")
    assert dv["JobQueue"] == TEMPLATE_VARS["gpu_job_queue"]
    assert dv["JobDefinition"] == TEMPLATE_VARS["deepvariant_job_definition"]
    assert dv["JobName"] == f"DeepVariant-SAMPLE7-{desc['name']}"
    assert dv["ContainerOverrides"]["Command"] == [
        "/opt/geyser-venv/bin/python",
        "deepvariant_task.py",
        "SAMPLE7",
        "ref.fa",
        "--from-examples",
    ]
    assert dv["ContainerOverrides"]["Environment"] == [
        {"Name": "RUN_NAME", "Value": desc["name"]},
        {"Name": "PIPELINE_CALLER", "Value": "deepvariant"},
    ]
    assert "Plan_Regions" not in entered(events)
    assert json.loads(desc["output"])["run_settings"]["dv_parts"] == 3


def test_split_mode_only_applies_to_deepvariant(sfn):
    desc, events = run_case(sfn, "HappyPath", overrides={"dv_parts": 3})
    assert desc["status"] == "SUCCEEDED"
    assert "Choose_DeepVariant_Mode" not in entered(events) and "Call_Variants" in entered(events)


def test_a_failed_part_falls_back_to_bcftools_without_the_gpu_job(sfn):
    desc, events = run_case(sfn, "DeepVariantSplitPartFails", caller="deepvariant", overrides={"dv_parts": 2})
    assert desc["status"] == "SUCCEEDED"
    states = entered(events)
    assert states.index("Make_Examples_Parts") < states.index("Plan_Regions") < states.index("Call_Variants")
    assert "DeepVariant" not in states and "Notify_Failure" not in states
    assert json.loads(desc["output"])["deepvariant_error"]["Error"] == "States.TaskFailed"


@pytest.mark.parametrize("dv_parts", [0, 7, 2.5, 3.0, "3", None, -1])
def test_an_invalid_dv_parts_fails_the_run_before_any_job(sfn, dv_parts):
    desc, events = run_case(sfn, "DeepVariantSplitOk", caller="deepvariant", overrides={"dv_parts": dv_parts})
    assert desc["status"] == "FAILED" and not batch_parameters(events)
    assert "Prepare_Run_Overrides_Error" in entered(events)
    assert "States.Runtime" not in history_text(events)
    error = published_message(events)["ErrorDetails"]
    assert error["Error"] == "InvalidRunOverrides" and "dv_parts (1 to 6)" in error["Cause"]
    assert "calling_concurrency (1 to 48), merge_threads (1 to 8) and dv_parts" in error["Cause"]


# --- v2.0 from_alignment: call variants from the sample's existing CRAM -----------------------------------


ALIGNMENT_STATES = {"QC_and_Align", "Large_QC_and_Align", "Quality_Control", "Align_Genome", "Preflight"}


def test_from_alignment_skips_qc_and_alignment_and_runs_the_dv_split_on_the_existing_cram(sfn):
    desc, events = run_case(
        sfn,
        "DeepVariantSplitOk",
        sample="HG003-WGS30x",
        caller="deepvariant",
        overrides={"dv_parts": 3},
        input_bytes=LARGE,  # ignored: no route is chosen
        from_alignment=True,
    )
    assert desc["status"] == "SUCCEEDED"
    states = entered(events)
    assert not ALIGNMENT_STATES & set(states)
    assert states.index("Set_Run_Settings") < states.index("Choose_Start") < states.index("Check_Alignment")
    assert states.index("Check_Alignment") < states.index("Choose_Caller") < states.index("Make_Examples_Parts")
    (check,) = sdk_calls(events, "Check_Alignment")
    assert check == {"Bucket": TEMPLATE_VARS["bucket"], "Key": "alignments/HG003-WGS30x.cram"}
    assert len(batch_calls(events, "Make_Examples_Part")) == 3
    (dv,) = batch_calls(events, "DeepVariant")
    assert dv["ContainerOverrides"]["Command"][-1] == "--from-examples"
    assert "Quality_Gate" in states  # the gate still judges the call set


def test_from_alignment_with_bcftools_plans_calls_and_merges_from_the_cram(sfn):
    desc, events = run_case(sfn, "HappyPath", from_alignment=True)
    assert desc["status"] == "SUCCEEDED"
    states = entered(events)
    assert not ALIGNMENT_STATES & set(states)
    assert states.index("Check_Alignment") < states.index("Plan_Regions") < states.index("Call_Variants")
    assert set(batch_parameters(events)) == {"Plan_Regions", "Call_Shard", "Call_Variants", "Quality_Gate"}


@pytest.mark.parametrize("from_alignment", [_UNSET, False])
def test_without_from_alignment_the_run_aligns_as_before(sfn, from_alignment):
    desc, events = run_case(sfn, "HappyPath", from_alignment=from_alignment)
    assert desc["status"] == "SUCCEEDED"
    states = entered(events)
    assert "Choose_Start" in states and "Check_Alignment" not in states
    assert {"QC_and_Align", "Quality_Control", "Align_Genome"} <= set(states)


@pytest.mark.parametrize("machine", ["sfn", "sfn_fail"])
def test_from_alignment_without_a_cram_fails_the_run_before_any_job(request, machine):
    desc, events = run_case(
        request.getfixturevalue(machine), "AlignmentMissing", caller="deepvariant", from_alignment=True
    )
    assert desc["status"] == "FAILED" and failure_error(events) == "PipelineStageFailed"
    states = entered(events)
    assert not batch_parameters(events)  # no job ran
    assert states[-6:] == [
        "Prepare_Alignment_Not_Found",
        "Prepare_Run_Close",
        "Close_Run_Record",
        "Notify_Failure",
        "Publish_Run",
        "Pipeline_Failed",
    ]
    assert len(sdk_calls(events, "Check_Alignment")) == 1  # a missing key is not retried
    assert_closed_before_notifying(desc, events, "AlignmentNotFound")
    error = published_message(events)["ErrorDetails"]
    assert error["Error"] == "AlignmentNotFound" and "alignments/S1.cram" in error["Cause"]


@pytest.mark.parametrize("from_alignment", ["true", 1, None, [True], {"cram": True}])
def test_an_invalid_from_alignment_fails_before_any_job(sfn, from_alignment):
    desc, events = run_case(sfn, "HappyPath", from_alignment=from_alignment)
    assert desc["status"] == "FAILED" and not batch_parameters(events)
    states = entered(events)
    assert "Prepare_From_Alignment_Error" in states and "Check_Alignment" not in states
    assert "States.Runtime" not in history_text(events)
    error = published_message(events)["ErrorDetails"]
    assert error["Error"] == "InvalidFromAlignment" and error["Cause"].startswith("from_alignment ")


# --- scatter-gather: the Map over the planned regions (#108) ------------------------------------


def test_the_map_calls_every_region_once_then_merges(sfn):
    desc, events = run_case(sfn, "HappyPath", sample="SAMPLE7", reference="ref.fa")
    assert desc["status"] == "SUCCEEDED"
    shards = batch_calls(events, "Call_Shard")
    regions = sorted(p["ContainerOverrides"]["Command"][-1] for p in shards)
    assert regions == sorted(REGIONS)
    for p in shards:
        assert p["ContainerOverrides"]["Command"][:6] == [
            "python",
            "tasks.py",
            "variants",
            "SAMPLE7",
            "ref.fa",
            "--region",
        ]
        assert p["ContainerOverrides"]["Environment"] == [{"Name": "RUN_NAME", "Value": desc["name"]}]
        assert re.fullmatch(rf"CallVariantsShard-[0-2]-{desc['name']}", p["JobName"])
    # The merge comes after every shard, and reads the regions the planner wrote for this run.
    by_id = {e["id"]: e for e in events}
    assert ancestor(by_id, state_entered(events, "Call_Variants"), "MapStateExited") is not None
    names = {e["id"]: e["stateEnteredEventDetails"]["name"] for e in events if e["type"] == "TaskStateEntered"}
    (read,) = [e for e in events if e["type"] == "TaskScheduled" and names.get(e["previousEventId"]) == "Read_Regions"]
    params = json.loads(read["taskScheduledEventDetails"]["parameters"])
    assert params == {
        "Bucket": TEMPLATE_VARS["bucket"],
        "Key": f"intermediate/variant-regions/SAMPLE7/{desc['name']}.json",
    }


def test_each_map_reads_its_concurrency_from_the_run_settings():
    # The real definition (not for_sfn_local's): each Map takes the run's setting, whose default is Terraform's.
    definition = json.loads(render_definition())
    states = definition["States"]
    assert states["Call_Variant_Shards"]["MaxConcurrencyPath"] == "$.run_settings.calling_concurrency"
    assert "MaxConcurrency" not in states["Call_Variant_Shards"]
    (chunks,) = [
        b["States"]["Align_Chunks"] for b in states["Large_QC_and_Align"]["Branches"] if "Align_Chunks" in b["States"]
    ]
    assert chunks["MaxConcurrencyPath"] == "$.run_settings.align_concurrency" and "MaxConcurrency" not in chunks
    defaults = states["Run_Setting_Rules"]["Result"]["defaults"]
    assert defaults["calling_concurrency"] == int(TEMPLATE_VARS["variants_max_concurrency"])
    assert defaults["align_concurrency"] == int(TEMPLATE_VARS["genome_align_concurrency"])
    # Every MaxConcurrencyPath in the file is one for_sfn_local knows how to stand in for.
    paths = re.findall(r'"MaxConcurrencyPath": "([^"]+)"', render_definition())
    assert sorted(paths) == sorted(CONCURRENCY_PATHS)


@pytest.mark.parametrize("case", ["PlanFails", "RegionsUnreadable", "ShardFails"])
def test_a_failed_planner_read_or_shard_notifies_and_never_merges(sfn, case):
    # A missing part would make the merge fail anyway, but it must not even start: the run ends at
    # Notify_Failure -> Pipeline_Failed, and nothing claims a VCF for this run.
    desc, events = run_case(sfn, case)
    assert desc["status"] == "FAILED" and failure_error(events) == "PipelineStageFailed"
    states = entered(events)
    # Not states[-2:]: a cancelled Parallel branch can log its last StateEntered after Notify_Failure.
    assert states[-1] == "Pipeline_Failed" and states.index("Notify_Failure") < states.index("Pipeline_Failed")
    assert "Call_Variants" not in states
    assert_closed_before_notifying(
        desc, events, "S3.NoSuchKeyException" if case == "RegionsUnreadable" else "States.TaskFailed"
    )
    if case == "PlanFails":
        assert "Read_Regions" not in states and "Call_Variant_Shards" not in states


def test_a_failed_close_still_notifies_with_the_original_error(sfn):
    desc, events = run_case(sfn, "CloseFails", wait_seconds=90)
    assert desc["status"] == "FAILED" and failure_error(events) == "PipelineStageFailed"
    states = entered(events)
    assert states.index("Close_Run_Record") < states.index("Notify_Failure") and states[-1] == "Pipeline_Failed"
    published = next(
        e for e in events if e["type"] == "TaskScheduled" and e["taskScheduledEventDetails"]["resourceType"] == "sns"
    )
    message = json.loads(published["taskScheduledEventDetails"]["parameters"])["Message"]
    assert message["ErrorDetails"]["Error"] == "States.TaskFailed"  # the stage's error, not the close's


# --- the quality gate (#144): pass, warn, fail ----------------------------------------------------


def gate_read(events) -> dict:
    """The S3 GetObject parameters Read_Gate_Verdict used."""
    names = {e["id"]: e["stateEnteredEventDetails"]["name"] for e in events if e["type"] == "TaskStateEntered"}
    (read,) = [
        e for e in events if e["type"] == "TaskScheduled" and names.get(e["previousEventId"]) == "Read_Gate_Verdict"
    ]
    return json.loads(read["taskScheduledEventDetails"]["parameters"])


def published_message(events) -> dict:
    (published,) = [
        e for e in events if e["type"] == "TaskScheduled" and e["taskScheduledEventDetails"]["resourceType"] == "sns"
    ]
    return json.loads(published["taskScheduledEventDetails"]["parameters"])["Message"]


def test_the_gate_judges_the_merged_call_set_and_a_pass_ends_the_run(sfn):
    desc, events = run_case(sfn, "HappyPath", sample="SAMPLE7")
    assert desc["status"] == "SUCCEEDED"
    states = entered(events)
    assert states.index("Call_Variants") < states.index("Quality_Gate") < states.index("Read_Gate_Verdict")
    # Every run ends through Publish_Run (W3), which goes straight on while publishing is off.
    assert states[-3:] == ["Gate_Decision", "Publish_Run", "Pipeline_Succeeded"]
    assert gate_read(events) == {"Bucket": TEMPLATE_VARS["bucket"], "Key": "qc_reports/SAMPLE7.gate.json"}
    output = json.loads(desc["output"])
    assert output["gate"]["verdict"]["verdict"] == "pass"
    assert output["batch_params"]["Mode"] == "warn"


def test_the_gate_also_judges_a_deepvariant_run(sfn):
    desc, events = run_case(sfn, "DeepVariantOk", caller="deepvariant")
    assert desc["status"] == "SUCCEEDED"
    states = entered(events)
    assert states.index("DeepVariant") < states.index("Quality_Gate")
    assert "Call_Variants" not in states and states[-1] == "Pipeline_Succeeded"


NO_VERDICT_CASES = [
    "GateNoVerdict",
    "GateJobFails",
    "GateVerdictMissing",
    "GateVerdictMalformed",
    "GateVerdictReasonless",
    "GateVerdictList",
]


@pytest.mark.parametrize("case", ["GateFailsVerdict", *NO_VERDICT_CASES])
def test_warn_mode_records_a_failed_or_missing_verdict_and_the_run_succeeds(sfn, case):
    desc, events = run_case(sfn, case)
    assert desc["status"] == "SUCCEEDED"
    states = entered(events)
    # A judged run (Gate_Warned) is told apart from one the gate never judged (Gate_Skipped, #157).
    last = "Gate_Warned" if case == "GateFailsVerdict" else "Gate_Skipped"
    assert states[-3:] == [last, "Publish_Run", "Pipeline_Succeeded"]
    assert "States.Runtime" not in history_text(events)
    assert "Notify_Failure" not in states
    output = json.loads(desc["output"])
    if case == "GateFailsVerdict":
        assert output["gate"]["verdict"]["reasons"] == GATE_REASONS  # kept in the execution's output
    if case in ("GateJobFails", "GateVerdictMissing"):
        assert "gate_error" in output  # why there was no verdict


def test_fail_mode_fails_the_run_with_the_gates_reasons(sfn_fail):
    desc, events = run_case(sfn_fail, "GateFailsVerdict", sample="SAMPLE7")
    assert desc["status"] == "FAILED" and failure_error(events) == "PipelineStageFailed"
    states = entered(events)
    # The failure path like any failed stage's: the run record is closed as failed, then the notification.
    assert states[-6:] == [
        "Prepare_Gate_Failure",
        "Prepare_Run_Close",
        "Close_Run_Record",
        "Notify_Failure",
        "Publish_Run",
        "Pipeline_Failed",
    ]
    closed = run_close(events)["Body"]
    assert closed["status"] == "failed" and closed["caught"]["Error"] == "QualityGateFailed"
    error = published_message(events)["ErrorDetails"]
    assert error["Error"] == "QualityGateFailed"
    assert error["Cause"].startswith("SAMPLE7 failed the quality gate: ")
    for reason in GATE_REASONS:
        assert reason in error["Cause"]
    assert "qc_reports/SAMPLE7.gate.json" in error["Cause"]


@pytest.mark.parametrize("case", NO_VERDICT_CASES)
def test_fail_mode_treats_no_verdict_as_no_pass(sfn_fail, case):
    desc, events = run_case(sfn_fail, case)
    assert desc["status"] == "FAILED" and failure_error(events) == "PipelineStageFailed"
    assert entered(events)[-6:] == [
        "Prepare_Gate_Error",
        "Prepare_Run_Close",
        "Close_Run_Record",
        "Notify_Failure",
        "Publish_Run",
        "Pipeline_Failed",
    ]
    assert run_close(events)["Body"]["caught"]["Error"] == "QualityGateError"
    error = published_message(events)["ErrorDetails"]
    assert error["Error"] == "QualityGateError" and "gave no verdict for S1" in error["Cause"]


def test_fail_mode_passes_a_good_run(sfn_fail):
    desc, events = run_case(sfn_fail, "HappyPath")
    assert desc["status"] == "SUCCEEDED" and entered(events)[-1] == "Pipeline_Succeeded"


def test_a_failed_stage_before_the_gate_never_reaches_it(sfn):
    _, events = run_case(sfn, "VariantsFail")
    assert "Quality_Gate" not in entered(events)


def test_gate_mode_is_the_only_mode_in_the_definition():
    # fail by default (variables.tf, #157), warn only when rendered so: nothing else in the file decides it.
    for mode in ("warn", "fail"):
        definition = json.loads(render_definition(gate_mode=mode))
        assert definition["States"]["Prepare_Gate_Command"]["Parameters"]["Mode"] == mode
    with open(os.path.join(REPO_ROOT, "infrastructure", "variables.tf"), encoding="utf-8") as fh:
        tf = fh.read()
    block = tf[tf.index('variable "gate_mode"') :]
    assert re.search(r'default\s*=\s*"fail"', block[: block.index("validation")])


# --- the gate threshold profile per run (#157) ------------------------------------------------------


def gate_environment(events) -> dict:
    (params,) = batch_calls(events, "Quality_Gate")
    return {e["Name"]: e["Value"] for e in params["ContainerOverrides"]["Environment"]}


@pytest.mark.parametrize("profile", ["default", "synthetic", "smoke"])
def test_the_gate_profile_picks_the_thresholds_file(sfn, profile):
    desc, events = run_case(sfn, "HappyPath", gate_profile=profile)
    assert desc["status"] == "SUCCEEDED"
    assert gate_environment(events)["GATE_CONFIG"] == f"$GATE_THRESHOLDS_DIR/{profile}.toml"
    assert json.loads(desc["output"])["gate_profile"] == profile


def test_without_a_profile_the_run_uses_the_default_thresholds(sfn):
    desc, events = run_case(sfn, "HappyPath")
    assert entered(events)[:2] == ["Check_Gate_Profile", "Default_Gate_Profile"]
    assert json.loads(desc["output"])["gate_profile"] == "default"
    assert gate_environment(events)["GATE_CONFIG"] == "$GATE_THRESHOLDS_DIR/default.toml"


@pytest.mark.parametrize(
    "profile",
    [
        "strict",
        "Synthetic",
        " synthetic",
        42,
        None,
        # What the trigger passes on when it can't pass a plain value: mates that disagree, a likely
        # misspelt metadata key, metadata it couldn't read.
        ["default", "synthetic"],
        {"unrecognised metadata key": "gateprofile"},
        {"metadata unreadable": "AccessDenied"},
    ],
)
@pytest.mark.parametrize("machine", ["sfn", "sfn_fail"])
def test_an_unknown_profile_fails_the_run_before_any_job_in_either_mode(request, machine, profile):
    desc, events = run_case(request.getfixturevalue(machine), "HappyPath", gate_profile=profile)
    assert desc["status"] == "FAILED" and failure_error(events) == "PipelineStageFailed"
    states = entered(events)
    assert states[:2] == ["Check_Gate_Profile", "Prepare_Profile_Error"]
    assert "QC_and_Align" not in states and "Quality_Gate" not in states  # no job ran
    assert states[-4:] == ["Close_Run_Record", "Notify_Failure", "Publish_Run", "Pipeline_Failed"]
    error = published_message(events)["ErrorDetails"]
    # States.JsonToString writes compact JSON.
    assert error["Error"] == "UnknownGateProfile" and json.dumps(profile, separators=(",", ":")) in error["Cause"]
    assert "default, synthetic, smoke" in error["Cause"]


@pytest.mark.parametrize("profile", ["default", "synthetic", "smoke"])
@pytest.mark.parametrize(
    "machine,case,outcome",
    [
        ("sfn", "HappyPath", "Pipeline_Succeeded"),
        ("sfn", "GateFailsVerdict", "Gate_Warned"),
        ("sfn_fail", "HappyPath", "Pipeline_Succeeded"),
        ("sfn_fail", "GateFailsVerdict", "Pipeline_Failed"),
    ],
)
def test_both_profiles_follow_the_gate_mode(request, profile, machine, case, outcome):
    # The profile chooses the thresholds; gate_mode alone decides what a failed verdict does.
    desc, events = run_case(request.getfixturevalue(machine), case, gate_profile=profile)
    states = entered(events)
    assert outcome in states
    assert desc["status"] == ("FAILED" if outcome == "Pipeline_Failed" else "SUCCEEDED")
    assert gate_environment(events)["GATE_CONFIG"] == f"$GATE_THRESHOLDS_DIR/{profile}.toml"


# --- the aligner per run (#146) -----------------------------------------------------------------------


@pytest.mark.parametrize("aligner", [_UNSET, "bwa-mem2"])
def test_without_giraffe_align_runs_in_the_app_job_definition(sfn, aligner):
    desc, events = run_case(sfn, "HappyPath", aligner=aligner)
    assert desc["status"] == "SUCCEEDED"
    states = entered(events)
    assert "Prepare_Align_Command" in states and "Prepare_Giraffe_Command" not in states
    (align,) = batch_calls(events, "Align_Genome")
    assert align["JobDefinition"] == TEMPLATE_VARS["job_definition"]


def test_aligner_giraffe_runs_the_same_align_stage_in_the_giraffe_job_definition(sfn):
    desc, events = run_case(sfn, "HappyPath", sample="SAMPLE7", reference="ref.fa", aligner="giraffe")
    assert desc["status"] == "SUCCEEDED"
    states = entered(events)
    assert "Prepare_Giraffe_Command" in states and "Prepare_Align_Command" not in states
    (align,) = batch_calls(events, "Align_Genome")
    assert align["JobDefinition"] == TEMPLATE_VARS["giraffe_job_definition"]
    # Everything else is the align stage's: queue, command, job name, run name, size and time limit, so the
    # variants stage, the run record and the dashboards see an ordinary align job.
    assert align["JobQueue"] == TEMPLATE_VARS["job_queue"]
    assert align["ContainerOverrides"]["Command"] == ["python", "tasks.py", "align", "SAMPLE7", "ref.fa"]
    assert re.fullmatch(r"AlignGenome-SAMPLE7-HappyPath-\w+", align["JobName"])
    assert align["ContainerOverrides"]["Environment"] == [{"Name": "RUN_NAME", "Value": desc["name"]}]
    sizes = {r["Type"]: r["Value"] for r in align["ContainerOverrides"]["ResourceRequirements"]}
    assert sizes == {"VCPU": TEMPLATE_VARS["align_vcpu"], "MEMORY": TEMPLATE_VARS["align_memory_mb"]}
    assert align["Timeout"]["AttemptDurationSeconds"] == 14400
    # Only align changes job definition.
    for state, p in batch_parameters(events).items():
        if state != "Align_Genome":
            assert p["JobDefinition"] == TEMPLATE_VARS["job_definition"], state


def test_giraffe_takes_the_runs_align_size_like_bwa_mem2(sfn):
    # The run's settings (#192), overrides included, size align whichever aligner runs it.
    overrides = {"align_vcpu": "2", "align_memory_mb": "8192", "variant_region_window": 17000000}
    desc, events = run_case(sfn, "HappyPath", aligner="giraffe", overrides=overrides)
    assert desc["status"] == "SUCCEEDED"
    states = entered(events)
    assert states.index("Set_Run_Settings") < states.index("Choose_Aligner") < states.index("Prepare_Giraffe_Command")
    (align,) = batch_calls(events, "Align_Genome")
    assert align["JobDefinition"] == TEMPLATE_VARS["giraffe_job_definition"]
    assert run_sizes(events) == {"align": ("2", "8192"), "plan_window": "17000000", "merge_window": "17000000"}


@pytest.mark.parametrize("aligner", ["Giraffe", "bwa", "", 7, None])
def test_an_unknown_aligner_fails_the_run_without_aligning(sfn, aligner):
    desc, events = run_case(sfn, "AlignerUnknown", aligner=aligner)
    assert desc["status"] == "FAILED" and failure_error(events) == "PipelineStageFailed"
    states = entered(events)
    assert "Unknown_Aligner" in states and "Align_Genome" not in states and "Call_Variants" not in states
    assert_closed_before_notifying(desc, events, error="UnknownAligner")


# --- the reference genome per upload: checked before any job ----------------------------------------


def sdk_calls(events, state) -> list:
    """Every SDK-integration call (its parameters) one task state made, retries included."""
    names = {e["id"]: e["stateEnteredEventDetails"]["name"] for e in events if e["type"] == "TaskStateEntered"}
    by_id = {e["id"]: e for e in events}
    calls = []
    for e in events:
        if e["type"] != "TaskScheduled":
            continue
        cursor = e  # a retry's TaskScheduled follows the failed attempt, not the TaskStateEntered
        while cursor is not None and cursor["id"] not in names:
            cursor = by_id.get(cursor.get("previousEventId"))
        if cursor is not None and names[cursor["id"]] == state:
            calls.append(json.loads(e["taskScheduledEventDetails"]["parameters"]))
    return calls


def test_the_reference_is_checked_in_s3_before_any_job(sfn):
    desc, events = run_case(sfn, "HappyPath", reference="GRCh38.fa")
    assert desc["status"] == "SUCCEEDED"
    states = entered(events)
    assert states[:4] == ["Check_Gate_Profile", "Default_Gate_Profile", "Check_Reference_Name", "Check_Reference"]
    assert states.index("Check_Reference") < states.index("QC_and_Align")
    assert sdk_calls(events, "Check_Reference") == [{"Bucket": TEMPLATE_VARS["bucket"], "Key": "reference/GRCh38.fa"}]
    # The stages get the reference the run was checked with.
    assert batch_parameters(events)["Align_Genome"]["ContainerOverrides"]["Command"][-1] == "GRCh38.fa"
    output = json.loads(desc["output"])
    assert output["reference_name"] == "GRCh38.fa" and "reference" not in output  # HeadObject result dropped


def test_a_profile_set_and_a_reference_both_reach_the_check(sfn):
    desc, events = run_case(sfn, "HappyPath", reference="GRCh38.fa", gate_profile="synthetic")
    assert desc["status"] == "SUCCEEDED"
    assert entered(events)[:3] == ["Check_Gate_Profile", "Check_Reference_Name", "Check_Reference"]


def test_a_transient_s3_error_on_the_check_is_retried(sfn):
    desc, events = run_case(sfn, "ReferenceFlakyOnce")
    assert desc["status"] == "SUCCEEDED"
    assert len(sdk_calls(events, "Check_Reference")) == 2


@pytest.mark.parametrize("machine", ["sfn", "sfn_fail"])
def test_a_reference_not_in_the_data_lake_fails_the_run_before_any_job(request, machine):
    desc, events = run_case(request.getfixturevalue(machine), "ReferenceMissing", reference="GRCh37.fa")
    assert desc["status"] == "FAILED" and failure_error(events) == "PipelineStageFailed"
    states = entered(events)
    assert "QC_and_Align" not in states and not batch_parameters(events)  # no job ran
    assert states[-6:] == [
        "Prepare_Reference_Not_Found",
        "Prepare_Run_Close",
        "Close_Run_Record",
        "Notify_Failure",
        "Publish_Run",
        "Pipeline_Failed",
    ]
    assert len(sdk_calls(events, "Check_Reference")) == 1  # a missing key is not retried
    assert_closed_before_notifying(desc, events, "ReferenceNotFound")
    error = published_message(events)["ErrorDetails"]
    assert error["Error"] == "ReferenceNotFound"
    assert f"reference/GRCh37.fa could not be read from {TEMPLATE_VARS['bucket']}" in error["Cause"]
    assert "S3.NoSuchKeyException" in error["Cause"] and "reference=<name>" in error["Cause"]


@pytest.mark.parametrize(
    "reference",
    [
        "",
        "sub/GRCh38.fa",
        "/GRCh38.fa",
        42,
        None,
        # What the trigger passes on when it can't pass a plain value: mates that disagree, a likely
        # misspelt metadata key, metadata it couldn't read.
        ["chr20.fa", "GRCh38.fa"],
        {"unrecognised metadata key": "refernce"},
        {"metadata unreadable": "AccessDenied"},
    ],
)
@pytest.mark.parametrize("machine", ["sfn", "sfn_fail"])
def test_a_reference_that_is_not_a_file_name_fails_the_run_before_any_job(request, machine, reference):
    desc, events = run_case(request.getfixturevalue(machine), "HappyPath", reference=reference)
    assert desc["status"] == "FAILED" and failure_error(events) == "PipelineStageFailed"
    states = entered(events)
    assert states[2:4] == ["Check_Reference_Name", "Prepare_Reference_Error"]
    assert "Check_Reference" not in states and "QC_and_Align" not in states  # not even S3 was asked
    assert states[-4:] == ["Close_Run_Record", "Notify_Failure", "Publish_Run", "Pipeline_Failed"]
    assert_closed_before_notifying(desc, events, "UnknownReference")
    error = published_message(events)["ErrorDetails"]
    assert error["Error"] == "UnknownReference"
    assert json.dumps(reference, separators=(",", ":")) in error["Cause"]  # JsonToString: compact JSON


def test_an_input_without_a_reference_fails_the_run_before_any_job(sfn):
    desc, events = run_case(sfn, "HappyPath", reference=_UNSET)
    assert desc["status"] == "FAILED" and failure_error(events) == "PipelineStageFailed"
    states = entered(events)
    assert states[2:4] == ["Check_Reference_Name", "Prepare_Reference_Missing"]
    assert "QC_and_Align" not in states and states[-1] == "Pipeline_Failed"
    assert_closed_before_notifying(desc, events, "UnknownReference")
    assert "no reference_name" in published_message(events)["ErrorDetails"]["Cause"]


def test_a_bad_gate_profile_is_still_reported_first(sfn):
    # Both wrong: the profile check comes first, so its error is the one notified.
    _, events = run_case(sfn, "HappyPath", reference="", gate_profile="strict")
    assert "Check_Reference_Name" not in entered(events)
    assert published_message(events)["ErrorDetails"]["Error"] == "UnknownGateProfile"


# --- per-run size overrides (the cost x turnaround grid; docs/figures/cost-surface-grid.md) ---------------

DEFAULT_RUN_SETTINGS = {
    "align_vcpu": "4",
    "align_memory_mb": "8192",
    "variant_region_window": "50000000",
    # v1.3: the genome's parallelism (the concurrencies as numbers, for MaxConcurrencyPath).
    "chunk_count": "24",
    "align_concurrency": 5,
    "calling_concurrency": 4,
    "merge_threads": "8",
    # v1.5: DeepVariant's make_examples parts (a number: Prepare_Examples_Parts does arithmetic on it).
    "dv_parts": 1,
}
OVERRIDE_CHAIN = [
    "Run_Setting_Rules",
    "Check_Run_Overrides",
    "Inspect_Run_Overrides",
    "Check_Run_Overrides_Object",
    "Apply_Run_Overrides",
    "Check_Run_Setting_Types",
    "Inspect_Run_Setting_Values",
    "Check_Run_Setting_Values",
    "Set_Run_Settings",
    "Choose_Start",  # v2.0: without from_alignment, on to the routes
    "Choose_Route",  # v1.3: small inputs go on to QC_and_Align as before
]


def run_sizes(events) -> dict:
    """Align's size and the planner's and merge's --window, as the jobs were submitted."""
    params = batch_parameters(events)
    align = {r["Type"]: r["Value"] for r in params["Align_Genome"]["ContainerOverrides"]["ResourceRequirements"]}
    plan, merge = (params[s]["ContainerOverrides"]["Command"] for s in ("Plan_Regions", "Call_Variants"))
    return {
        "align": (align["VCPU"], align["MEMORY"]),
        "plan_window": plan[plan.index("--window") + 1],
        "merge_window": merge[merge.index("--window") + 1],
    }


def test_the_allowed_align_sizes_keep_qc_plus_align_within_the_vcpu_cap():
    sizes = json.loads(TEMPLATE_VARS["align_sizes"])
    assert {"4/8192", "2/4096", "1/4096", "0.25/512"} <= set(sizes)
    assert "0.25/1536" not in sizes and "2/2048" not in sizes  # not Fargate pairs
    assert not [s for s in sizes if s.startswith("8/")]  # 8 + QC's 1 > the 8-vCPU cap
    assert align_override_sizes(qc_vcpu=1, fargate_max_vcpus=4)[-1] == "2/16384"  # 4 + 1 > 4: no 4 vCPU


@pytest.mark.parametrize("overrides", [_UNSET, {}])
def test_without_overrides_the_run_uses_the_terraform_defaults(sfn, overrides):
    desc, events = run_case(sfn, "HappyPath", overrides=overrides)
    assert desc["status"] == "SUCCEEDED"
    states = entered(events)
    chain = states[states.index("Run_Setting_Rules") : states.index("QC_and_Align")]
    assert [s for s in chain if s != "No_Run_Overrides"] == OVERRIDE_CHAIN
    assert ("No_Run_Overrides" in chain) == (overrides is _UNSET)
    assert json.loads(desc["output"])["run_settings"] == DEFAULT_RUN_SETTINGS
    assert run_sizes(events) == {"align": ("4", "8192"), "plan_window": "50000000", "merge_window": "50000000"}


@pytest.mark.parametrize(
    "overrides,expected",
    [
        (
            {"align_vcpu": "2", "align_memory_mb": "4096", "variant_region_window": 17000000},
            {"align": ("2", "4096"), "plan_window": "17000000", "merge_window": "17000000"},
        ),
        # One knob at a time: the rest stay at the defaults.
        (
            {"variant_region_window": 9000000},
            {"align": ("4", "8192"), "plan_window": "9000000", "merge_window": "9000000"},
        ),
        (
            {"align_vcpu": "1"},  # 1 vCPU with the default 8192 MiB: a valid pair
            {"align": ("1", "8192"), "plan_window": "50000000", "merge_window": "50000000"},
        ),
    ],
)
def test_overrides_reach_align_the_planner_and_the_merge(sfn, overrides, expected):
    desc, events = run_case(sfn, "HappyPath", overrides=overrides)
    assert desc["status"] == "SUCCEEDED"
    assert run_sizes(events) == expected
    # Only align changes size: QC, the planner, the shards and the merge keep theirs.
    sizes = {
        state: {r["Type"]: r["Value"] for r in p["ContainerOverrides"]["ResourceRequirements"]}
        for state, p in batch_parameters(events).items()
    }
    assert sizes["Quality_Control"] == {"VCPU": "1", "MEMORY": "2048"}
    assert sizes["Plan_Regions"] == sizes["Call_Variants"] == {"VCPU": "0.25", "MEMORY": "512"}
    assert sizes["Call_Shard"] == {"VCPU": "1", "MEMORY": "2048"}


@pytest.mark.parametrize(
    "overrides",
    [
        # Not an object.
        [],
        5,
        "align_vcpu=2",
        None,
        # An unknown (misspelt) key: never ignored.
        {"align_cpu": "2"},
        {"align_vcpu": "2", "align_memory_mb": "4096", "window": 17000000},
        # Wrong types: sizes are strings (Batch wants strings), the window a number.
        {"align_vcpu": 2},
        {"align_memory_mb": 4096},
        {"variant_region_window": "17000000"},
        {"align_vcpu": {"vcpu": "2"}},
        {"align_vcpu": None},
        # Not an allowed pair: not Fargate's, over the vCPU cap, or not written as Terraform writes it.
        {"align_vcpu": "2", "align_memory_mb": "2048"},
        {"align_vcpu": "0.25", "align_memory_mb": "1536"},
        {"align_vcpu": "8", "align_memory_mb": "16384"},
        {"align_vcpu": "4.0"},
        {"align_vcpu": "0.5"},  # with the default 8192 MiB
        # The window: out of bounds, or not a whole number.
        {"variant_region_window": 1000},
        {"variant_region_window": 300000000},
        {"variant_region_window": 17000000.5},
        {"variant_region_window": 1.7e7},
    ],
)
def test_invalid_overrides_fail_the_run_before_any_job(sfn, overrides):
    desc, events = run_case(sfn, "HappyPath", overrides=overrides)
    assert desc["status"] == "FAILED" and failure_error(events) == "PipelineStageFailed"
    states = entered(events)
    assert "Prepare_Run_Overrides_Error" in states
    assert "QC_and_Align" not in states and not batch_parameters(events)  # no job ran
    assert "States.Runtime" not in history_text(events)
    assert states[-4:] == ["Close_Run_Record", "Notify_Failure", "Publish_Run", "Pipeline_Failed"]
    assert_closed_before_notifying(desc, events, "InvalidRunOverrides")
    error = published_message(events)["ErrorDetails"]
    assert error["Error"] == "InvalidRunOverrides"
    assert "are not valid" in error["Cause"] and error["Cause"].startswith("overrides ")
    assert "4/8192" in error["Cause"] and "from 5000000 to 250000000" in error["Cause"]


def test_a_bad_gate_profile_is_reported_before_the_overrides(sfn):
    _, events = run_case(sfn, "HappyPath", gate_profile="strict", overrides={"align_cpu": "2"})
    assert "Run_Setting_Rules" not in entered(events)
    assert published_message(events)["ErrorDetails"]["Error"] == "UnknownGateProfile"


@pytest.mark.parametrize("machine", ["sfn", "sfn_fail"])
def test_the_checks_run_in_order_gate_profile_reference_then_run_settings(request, machine):
    # All three checks at once, in both gate modes: the smoke profile (#195), a reference (#183) and an
    # override (#192) all reach the run.
    sfn = request.getfixturevalue(machine)
    desc, events = run_case(
        sfn, "HappyPath", reference="GRCh38.fa", gate_profile="smoke", overrides={"variant_region_window": 9000000}
    )
    assert desc["status"] == "SUCCEEDED"
    states = entered(events)
    order = ["Check_Gate_Profile", "Check_Reference_Name", "Check_Reference", "Run_Setting_Rules", "QC_and_Align"]
    assert [states.index(s) for s in order] == sorted(states.index(s) for s in order)
    assert sdk_calls(events, "Check_Reference") == [{"Bucket": TEMPLATE_VARS["bucket"], "Key": "reference/GRCh38.fa"}]
    assert run_sizes(events)["merge_window"] == "9000000"
    output = json.loads(desc["output"])
    assert output["gate_profile"] == "smoke" and output["reference_name"] == "GRCh38.fa"
    # Both wrong: the reference is reported, and the overrides are never looked at.
    _, events = run_case(sfn, "ReferenceMissing", reference="GRCh37.fa", overrides={"align_cpu": "2"})
    assert "Run_Setting_Rules" not in entered(events)
    assert published_message(events)["ErrorDetails"]["Error"] == "ReferenceNotFound"


# --- the hosted site (W3): the Publish state after every run ---------------------------------------------------


@pytest.mark.parametrize("case,status", [("HappyPath", "SUCCEEDED"), ("QCFails", "FAILED")])
def test_with_publishing_off_a_run_ends_as_before(sfn, case, status):
    desc, events = run_case(sfn, case)
    assert desc["status"] == status
    states = entered(events)
    assert "Publish" not in states and states[-2] == "Publish_Run"
    assert json.loads(desc["output"] if status == "SUCCEEDED" else "{}").get("publish_output") is None


def test_a_succeeded_run_publishes_its_page_then_succeeds(sfn_publish):
    desc, events = run_case(sfn_publish, "PublishOk", sample="SAMPLE7")
    assert desc["status"] == "SUCCEEDED"
    assert entered(events)[-4:] == ["Publish_Run", "Publish", "Publish_Finished", "Pipeline_Succeeded"]
    (params,) = batch_calls(events, "Publish")
    # Its own job definition (publish.tf: the geyser-publish image; the site, distribution and API base are its env).
    assert params["JobDefinition"] == TEMPLATE_VARS["publish_job_definition"]
    assert params["JobName"] == f"Publish-SAMPLE7-{desc['name']}"
    overrides = params["ContainerOverrides"]
    assert overrides["Command"] == [
        "python",
        "manage.py",
        "publish",
        "SAMPLE7",
    ]  # T3's contract: the site from $SITE_BUCKET
    # The site and the distribution per run, so switching publishing on never changes the job definition.
    assert overrides["Environment"] == [
        {"Name": "RUN_NAME", "Value": desc["name"]},
        {"Name": "SITE_BUCKET", "Value": "geyser-site-local"},
        {"Name": "CLOUDFRONT_DISTRIBUTION_ID", "Value": "E2EXAMPLE"},
    ]
    assert "ResourceRequirements" not in overrides  # the job definition's own size
    assert params["Timeout"] == {"AttemptDurationSeconds": 900}


def test_a_failed_publish_never_changes_the_runs_outcome(sfn_publish):
    desc, events = run_case(sfn_publish, "PublishFails")
    assert desc["status"] == "SUCCEEDED"
    assert json.loads(desc["output"])["publish_error"]["Error"] == "States.TaskFailed"
    desc, events = run_case(sfn_publish, "QCFailsAndPublishFails")
    assert desc["status"] == "FAILED" and failure_error(events) == "PipelineStageFailed"
    assert entered(events)[-3:] == ["Publish", "Publish_Finished", "Pipeline_Failed"]


def test_a_failed_run_is_published_after_its_notification_and_still_fails(sfn_publish):
    desc, events = run_case(sfn_publish, "QCFailsThenPublished")
    assert desc["status"] == "FAILED" and failure_error(events) == "PipelineStageFailed"
    states = entered(events)
    assert states[-6:] == [
        "Close_Run_Record",
        "Notify_Failure",
        "Publish_Run",
        "Publish",
        "Publish_Finished",
        "Pipeline_Failed",
    ]


@pytest.mark.parametrize("bad", [{"gate_profile": "strict"}, {"reference": "sub/x.fa"}])
def test_a_run_that_failed_before_any_job_is_published_too(sfn_publish, bad):
    # T3's W2: the sample list links every finished sample, so a run that failed before any stage needs its page
    # too (it says so). The switch doesn't depend on the run's data: it is rendered into the Choice.
    desc, events = run_case(sfn_publish, "PublishOk", **bad)
    assert desc["status"] == "FAILED"
    states = entered(events)
    assert "QC_and_Align" not in states and "Run_Setting_Rules" not in states  # no job ran
    assert states[-4:] == ["Publish_Run", "Publish", "Publish_Finished", "Pipeline_Failed"]
    (params,) = batch_calls(events, "Publish")
    assert params["ContainerOverrides"]["Command"] == ["python", "manage.py", "publish", "S1"]


def test_publish_enabled_is_rendered_into_the_choice_not_read_from_the_run():
    for enabled in ("true", "false"):
        states = json.loads(render_definition(publish_enabled=enabled))["States"]
        first = states["Publish_Run"]["Choices"][0]
        assert first == {"Variable": "$$.Execution.Id", "IsPresent": enabled == "true", "Next": "Publish"}
        assert "publish" not in states["Run_Setting_Rules"]["Result"]


# --- v1.3 Big inputs: Choose_Route, the large route, the per-run parallelism ----------------------------------

LARGE_STATES = ("Large_QC_and_Align", "Preflight", "Read_Manifest", "Align_Chunks", "Align_Chunk", "Align_Merge")


@pytest.mark.parametrize("input_bytes", [_UNSET, 0, 4_999_999_999])
def test_small_inputs_take_todays_path_unchanged(sfn, input_bytes):
    desc, events = run_case(sfn, "HappyPath", input_bytes=input_bytes)
    assert desc["status"] == "SUCCEEDED"
    states = entered(events)
    at = states.index("Set_Run_Settings")
    assert states[at + 1 : at + 4] == ["Choose_Start", "Choose_Route", "QC_and_Align"]
    assert not set(LARGE_STATES) & set(states)
    assert set(batch_parameters(events)) == set(BATCH_TASKS)  # the same jobs as before v1.3


@pytest.mark.parametrize("machine", ["sfn", "sfn_fail"])
def test_a_large_input_takes_the_large_route_and_rejoins_at_the_caller(request, machine):
    desc, events = run_case(request.getfixturevalue(machine), "LargeRouteOk", sample="SAMPLE7", input_bytes=LARGE)
    assert desc["status"] == "SUCCEEDED"
    states = entered(events)
    assert "QC_and_Align" not in states and "Align_Genome" not in states
    order = [
        "Choose_Route",
        "Check_Large_Aligner",
        "Large_QC_and_Align",
        "Choose_Caller",
        "Plan_Regions",
        "Quality_Gate",
    ]
    assert [states.index(s) for s in order] == sorted(states.index(s) for s in order)
    large_order = ["Preflight", "Read_Manifest", "Align_Chunks", "Align_Merge"]
    assert [states.index(s) for s in large_order] == sorted(states.index(s) for s in large_order)
    name = desc["name"]
    (preflight,) = batch_calls(events, "Preflight")
    assert preflight["JobName"] == f"Preflight-SAMPLE7-{name}"
    assert preflight["JobDefinition"] == TEMPLATE_VARS["large_job_definition"]
    assert preflight["ContainerOverrides"]["Command"] == [
        "python",
        "tasks.py",
        "preflight",
        "SAMPLE7",
        "--chunks",
        "24",
    ]
    assert preflight["Timeout"] == {"AttemptDurationSeconds": 7200}
    (qc,) = batch_calls(events, "Quality_Control_Large")
    assert qc["ContainerOverrides"]["Command"] == ["python", "tasks.py", "qc", "SAMPLE7"]
    assert qc["JobDefinition"] == TEMPLATE_VARS["large_job_definition"]
    assert qc["ContainerOverrides"]["ResourceRequirements"] == [
        {"Type": "VCPU", "Value": "8"},
        {"Type": "MEMORY", "Value": "16384"},
    ]
    assert sdk_calls(events, "Read_Manifest") == [
        {"Bucket": TEMPLATE_VARS["bucket"], "Key": f"intermediate/chunks/SAMPLE7/{name}/manifest.json"}
    ]
    chunks = batch_calls(events, "Align_Chunk")
    assert sorted(c["ContainerOverrides"]["Command"][-1] for c in chunks) == ["0", "1", "2"]  # one per manifest chunk
    for c in chunks:
        index = c["ContainerOverrides"]["Command"][-1]
        expected = ["python", "tasks.py", "align_chunk", "SAMPLE7", "chr20.fa", "--chunk", index]
        assert c["ContainerOverrides"]["Command"] == expected
        assert c["JobName"] == f"AlignChunk-{index}-{name}"
        assert c["JobDefinition"] == TEMPLATE_VARS["large_job_definition"]
        assert c["Timeout"] == {"AttemptDurationSeconds": 3600}
        assert c["ContainerOverrides"]["ResourceRequirements"][1] == {"Type": "MEMORY", "Value": "32768"}
    (merge,) = batch_calls(events, "Align_Merge")
    expected = ["python", "tasks.py", "align_merge", "SAMPLE7", "chr20.fa", "--threads", "8"]
    assert merge["ContainerOverrides"]["Command"] == expected
    assert merge["JobName"] == f"AlignMerge-SAMPLE7-{name}"
    assert merge["Timeout"] == {"AttemptDurationSeconds": 14400}
    # Downstream is unchanged: the same calling, merge and gate as the small route.
    assert {"Plan_Regions", "Call_Shard", "Call_Variants", "Quality_Gate"} <= set(batch_parameters(events))


@pytest.mark.parametrize(
    "case,failed",
    [
        ("PreflightFails", "Preflight"),
        ("ManifestMissing", "Read_Manifest"),
        ("ChunkFails", "Align_Chunk"),
        ("MergeFails", "Align_Merge"),
        ("LargeQCFails", "Quality_Control_Large"),
    ],
)
def test_a_failure_on_the_large_route_closes_and_notifies_before_calling(sfn, case, failed):
    desc, events = run_case(sfn, case, input_bytes=LARGE)
    assert desc["status"] == "FAILED" and failure_error(events) == "PipelineStageFailed"
    states = entered(events)
    assert failed in states and "Choose_Caller" not in states and "Plan_Regions" not in states
    assert states[-4:] == ["Close_Run_Record", "Notify_Failure", "Publish_Run", "Pipeline_Failed"]


def test_a_failed_preflight_runs_no_alignment(sfn):
    _, events = run_case(sfn, "PreflightFails", input_bytes=LARGE)
    assert "Align_Chunks" not in entered(events) and not batch_calls(events, "Align_Chunk")


@pytest.mark.parametrize("aligner", ["giraffe", "GIRAFFE", "bwa", 42])
def test_the_large_route_refuses_any_aligner_but_bwa_mem2_before_any_job(sfn, aligner):
    desc, events = run_case(sfn, "LargeRouteOk", input_bytes=LARGE, aligner=aligner)
    assert desc["status"] == "FAILED"
    assert "Prepare_Large_Aligner_Error" in entered(events) and not batch_parameters(events)
    error = published_message(events)["ErrorDetails"]
    assert error["Error"] == "AlignerNotOnLargeRoute" and "giraffe needs the whole sample" in error["Cause"]


def test_bwa_mem2_named_explicitly_is_fine_on_the_large_route(sfn):
    desc, _ = run_case(sfn, "LargeRouteOk", input_bytes=LARGE, aligner="bwa-mem2")
    assert desc["status"] == "SUCCEEDED"


@pytest.mark.parametrize("input_bytes", ["60000000000", -1, None, [1], {"size": 1}, True])
def test_an_input_size_that_is_not_a_byte_count_fails_before_any_job(sfn, input_bytes):
    desc, events = run_case(sfn, "HappyPath", input_bytes=input_bytes)
    assert desc["status"] == "FAILED" and not batch_parameters(events)
    assert published_message(events)["ErrorDetails"]["Error"] == "InvalidInputBytes"


def test_the_defaults_are_the_run_settings(sfn):
    desc, _ = run_case(sfn, "LargeRouteOk", input_bytes=LARGE)
    settings = json.loads(desc["output"])["run_settings"]
    assert settings["chunk_count"] == "24" and settings["merge_threads"] == "8"
    assert settings["align_concurrency"] == 5 and settings["calling_concurrency"] == 4  # numbers, for the Maps


def test_parallelism_overrides_reach_the_preflight_the_merge_and_both_maps(sfn):
    overrides = {"chunk_count": 4, "align_concurrency": 2, "calling_concurrency": 48, "merge_threads": 4}
    desc, events = run_case(sfn, "LargeRouteOk", input_bytes=LARGE, overrides=overrides)
    assert desc["status"] == "SUCCEEDED"
    (preflight,) = batch_calls(events, "Preflight")
    assert preflight["ContainerOverrides"]["Command"][-2:] == ["--chunks", "4"]
    (merge,) = batch_calls(events, "Align_Merge")
    assert merge["ContainerOverrides"]["Command"][-2:] == ["--threads", "4"]
    # The Maps read these through MaxConcurrencyPath (pinned by test_each_map_reads_its_concurrency_from_the_run_settings).
    settings = json.loads(desc["output"])["run_settings"]
    assert settings["align_concurrency"] == 2 and settings["calling_concurrency"] == 48


@pytest.mark.parametrize(
    "overrides",
    [
        {"chunk_count": 1},
        {"chunk_count": 97},
        {"chunk_count": "24"},
        {"chunk_count": 24.5},
        {"chunk_count": 2.4e1},
        {"align_concurrency": 0},
        {"align_concurrency": 6},  # 6 x 8 + QC 8 = 56 > the 48-vCPU Spot cap
        {"align_concurrency": "5"},
        {"calling_concurrency": 49},
        {"calling_concurrency": 0},
        {"calling_concurrency": 1.5},
        {"merge_threads": 9},
        {"merge_threads": 0},
        {"merge_threads": None},
        {"align_concurency": 2},  # misspelt: never a silent default
    ],
)
@pytest.mark.parametrize("input_bytes", [LARGE, _UNSET])
def test_out_of_bounds_parallelism_fails_before_any_job_on_either_route(sfn, overrides, input_bytes):
    desc, events = run_case(sfn, "LargeRouteOk", input_bytes=input_bytes, overrides=overrides)
    assert desc["status"] == "FAILED" and not batch_parameters(events)
    states = entered(events)
    assert "Prepare_Run_Overrides_Error" in states and "Choose_Route" not in states
    error = published_message(events)["ErrorDetails"]
    assert error["Error"] == "InvalidRunOverrides"
    assert "align_concurrency (1 to 5)" in error["Cause"] and "calling_concurrency (1 to 48)" in error["Cause"]
