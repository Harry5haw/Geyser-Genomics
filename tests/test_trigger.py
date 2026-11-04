import json

import pytest


class FakeS3:
    """HeadObject for any key: the single-end trigger reads the upload's metadata (the gate profile)."""

    def __init__(self, metadata=None, missing=False):
        self.metadata, self.missing, self.heads = metadata or {}, missing, []

    def head_object(self, Bucket, Key):
        self.heads.append(Key)
        if self.missing:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        return {"ETag": '"e"', "LastModified": "2026-11-01 20:00:00+00:00", "Metadata": dict(self.metadata)}


@pytest.fixture(autouse=True)
def s3(trigger, monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(trigger, "_s3_client", fake)
    return fake


@pytest.mark.parametrize(
    "key,expected",
    [
        ("raw_reads/SRR062634.fastq.gz", "SRR062634"),
        ("raw_reads/small_test.fastq.gz", "small_test"),
        ("SRR1.fq.gz", "SRR1"),
    ],
)
def test_parse_sample_id(trigger, key, expected):
    assert trigger.parse_sample_id(key) == expected


def test_parse_sample_id_rejects_empty(trigger):
    with pytest.raises(ValueError):
        trigger.parse_sample_id("raw_reads/.fastq.gz")


def test_build_pipeline_input_defaults_to_chr20(trigger, monkeypatch):
    monkeypatch.delenv("REFERENCE_NAME", raising=False)
    assert trigger.build_pipeline_input("raw_reads/S1.fastq.gz") == {"srr_id": "S1", "reference_name": "chr20.fa"}


def test_handler_starts_execution_with_expected_payload(trigger, monkeypatch):
    calls = []

    class FakeSfn:
        def start_execution(self, **kwargs):
            calls.append(kwargs)
            return {"executionArn": "arn:fake"}

    monkeypatch.setenv("STATE_MACHINE_ARN", "arn:sm")
    monkeypatch.delenv("REFERENCE_NAME", raising=False)
    monkeypatch.setattr(trigger, "_sfn_client", FakeSfn())
    event = {"Records": [{"s3": {"bucket": {"name": "b"}, "object": {"key": "raw_reads/SRR9%2Bx.fastq.gz"}}}]}

    result = trigger.handler(event, None)

    assert result["statusCode"] == 200
    assert calls[0]["stateMachineArn"] == "arn:sm"
    assert json.loads(calls[0]["input"]) == {"srr_id": "SRR9+x", "reference_name": "chr20.fa"}


def test_handler_rejects_malformed_event(trigger, monkeypatch):
    monkeypatch.setenv("STATE_MACHINE_ARN", "arn:sm")
    with pytest.raises(RuntimeError):
        trigger.handler({"Records": []}, None)


# ------------------------------------------------------------------ execution names


import os  # noqa: E402
import re  # noqa: E402

from conftest import REPO_ROOT  # noqa: E402

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def s3_event(key, event_time="2026-11-01T12:34:56.789Z", sequencer="0055AED6DCD90281E5"):
    obj = {"key": key}
    if sequencer:
        obj["sequencer"] = sequencer
    record = {"s3": {"bucket": {"name": "b"}, "object": obj}}
    if event_time:
        record["eventTime"] = event_time
    return {"Records": [record]}


class RecordingSfn:
    def __init__(self, error=None):
        self.calls, self.error = [], error

    def start_execution(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"executionArn": f"arn:fake:{kwargs['name']}"}


def test_execution_name_is_readable_and_deterministic(trigger):
    name = trigger.build_execution_name("SRR062634", "2026-11-01T12:34:56.789Z", "b/raw_reads/SRR062634.fastq.gz/0055")
    assert re.fullmatch(r"SRR062634-20261101T123456Z-[0-9a-f]{8}", name)
    assert name == trigger.build_execution_name(
        "SRR062634", "2026-11-01T12:34:56.789Z", "b/raw_reads/SRR062634.fastq.gz/0055"
    )
    assert name != trigger.build_execution_name(
        "SRR062634", "2026-11-01T12:34:56.789Z", "b/raw_reads/SRR062634.fastq.gz/0056"
    )


def test_execution_name_without_event_details_is_random_but_valid(trigger):
    a, b = trigger.build_execution_name("S1"), trigger.build_execution_name("S1", "not-a-time")
    assert a != b and NAME_RE.match(a) and NAME_RE.match(b)


@pytest.mark.parametrize("sample_id", ["SRR9+x", "my sample/with:odd*chars", "é" * 10, "x" * 200])
def test_execution_name_is_always_valid(trigger, sample_id):
    name = trigger.build_execution_name(sample_id, "2026-11-01T12:34:56Z", "k")
    assert NAME_RE.match(name)
    assert len(name) <= trigger.MAX_SAMPLE_PART + len("-20261101T123456Z-12345678")


def test_batch_job_names_still_fit(trigger):
    """The state machine builds job names as '<Prefix>-<srr_id>-<execution name>'; Batch allows 128 chars."""
    with open(os.path.join(REPO_ROOT, "infrastructure", "state_machine.asl.json"), encoding="utf-8") as fh:
        asl = fh.read()
    prefixes = re.findall(r"States\.Format\('([A-Za-z]+)-\{\}-\{\}', \$\.srr_id, \$\$\.Execution\.Name\)", asl)
    # AlignGenome twice: bwa-mem2 and giraffe (#146) name their align job the same way.
    assert prefixes == [
        "QualityControl",
        "AlignGenome",
        "AlignGenome",
        # v1.3 the large route: QC on the whole input, the pre-flight, the merge (the chunks are named by index).
        "QualityControl",
        "Preflight",
        "AlignMerge",
        "DeepVariant",
        "PlanRegions",
        "CallVariants",
        "QualityGate",
        "Publish",
    ]
    sample_id = "S" * 50
    name = trigger.build_execution_name(sample_id, "2026-11-01T12:34:56Z", "k")
    for prefix in prefixes:
        job_name = f"{prefix}-{sample_id}-{name}"
        assert len(job_name) <= 128 and re.fullmatch(r"[A-Za-z0-9_-]+", job_name)
    # The variant-calling shards (Map iterations) name the region's index instead of the sample, which the
    # execution name already contains: with the sample too, 131 characters at worst.
    assert "States.Format('CallVariantsShard-{}-{}', $.index, $$.Execution.Name)" in asl
    shard = f"CallVariantsShard-{99999}-{name}"
    assert len(shard) <= 128 and re.fullmatch(r"[A-Za-z0-9_-]+", shard)


def test_handler_names_the_execution_after_the_sample(trigger, monkeypatch):
    sfn = RecordingSfn()
    monkeypatch.setenv("STATE_MACHINE_ARN", "arn:sm")
    monkeypatch.setattr(trigger, "_sfn_client", sfn)
    result = trigger.handler(s3_event("raw_reads/SRR062634.fastq.gz"), None)
    name = sfn.calls[0]["name"]
    assert re.fullmatch(r"SRR062634-20261101T123456Z-[0-9a-f]{8}", name)
    assert json.loads(result["body"])["executionName"] == name
    trigger.handler(s3_event("raw_reads/SRR062634.fastq.gz"), None)  # same event delivered again
    assert sfn.calls[1]["name"] == name


def test_handler_treats_existing_execution_as_duplicate(trigger, monkeypatch):
    class ExecutionAlreadyExists(Exception):
        pass

    monkeypatch.setenv("STATE_MACHINE_ARN", "arn:sm")
    monkeypatch.setattr(trigger, "_sfn_client", RecordingSfn(ExecutionAlreadyExists("exists")))
    result = trigger.handler(s3_event("raw_reads/S1.fastq.gz"), None)
    assert result["statusCode"] == 200 and json.loads(result["body"])["duplicate"] is True


def test_handler_recognises_client_error_code(trigger, monkeypatch):
    err = Exception("boom")
    err.response = {"Error": {"Code": "ExecutionAlreadyExists"}}
    monkeypatch.setenv("STATE_MACHINE_ARN", "arn:sm")
    monkeypatch.setattr(trigger, "_sfn_client", RecordingSfn(err))
    assert json.loads(trigger.handler(s3_event("raw_reads/S1.fastq.gz"), None)["body"])["duplicate"] is True


def test_handler_reraises_other_errors(trigger, monkeypatch):
    monkeypatch.setenv("STATE_MACHINE_ARN", "arn:sm")
    monkeypatch.setattr(trigger, "_sfn_client", RecordingSfn(RuntimeError("AccessDenied")))
    with pytest.raises(RuntimeError):
        trigger.handler(s3_event("raw_reads/S1.fastq.gz"), None)


def test_caller_comes_from_the_environment_only_when_set(trigger, monkeypatch):
    monkeypatch.delenv("REFERENCE_NAME", raising=False)
    monkeypatch.delenv("CALLER", raising=False)
    assert "caller" not in trigger.build_pipeline_input("raw_reads/S1.fastq.gz")
    monkeypatch.setenv("CALLER", "deepvariant")
    assert trigger.build_pipeline_input("raw_reads/S1.fastq.gz")["caller"] == "deepvariant"
    monkeypatch.setenv("CALLER", "gatk")
    with pytest.raises(ValueError, match="CALLER must be one of"):
        trigger.build_pipeline_input("raw_reads/S1.fastq.gz")


def test_trigger_callers_match_the_state_machine_choice(trigger):
    with open(os.path.join(REPO_ROOT, "infrastructure", "state_machine.asl.json"), encoding="utf-8") as fh:
        asl = fh.read()
    assert '"StringEquals": "deepvariant"' in asl
    assert set(trigger.CALLERS) == {"bcftools", "deepvariant"}


# --- the gate threshold profile (#157): S3 user metadata gate-profile -> the input's gate_profile ------


def _start(trigger, monkeypatch, key="raw_reads/S1.fastq.gz"):
    calls = []

    class FakeSfn:
        def start_execution(self, **kwargs):
            calls.append(kwargs)
            return {"executionArn": "arn:fake"}

    monkeypatch.setenv("STATE_MACHINE_ARN", "arn:sm")
    monkeypatch.delenv("REFERENCE_NAME", raising=False)
    monkeypatch.delenv("CALLER", raising=False)
    monkeypatch.setattr(trigger, "_sfn_client", FakeSfn())
    trigger.handler({"Records": [{"s3": {"bucket": {"name": "b"}, "object": {"key": key}}}]}, None)
    return json.loads(calls[0]["input"])


@pytest.mark.parametrize("key", ["gate-profile", "gate_profile"])
def test_an_uploads_gate_profile_metadata_reaches_the_input(trigger, monkeypatch, s3, key):
    s3.metadata = {key: "synthetic"}
    assert _start(trigger, monkeypatch)["gate_profile"] == "synthetic"
    assert s3.heads == ["raw_reads/S1.fastq.gz"]


def test_the_value_goes_on_exactly_as_uploaded(trigger, monkeypatch, s3):
    # No normalising here: the state machine is the one validator, for uploads and manual starts alike.
    s3.metadata = {"gate-profile": " Synthetic "}
    assert _start(trigger, monkeypatch)["gate_profile"] == " Synthetic "


def test_a_misspelled_key_goes_on_flagged_not_ignored(trigger, monkeypatch, s3):
    s3.metadata = {"gateprofile": "synthetic"}
    assert _start(trigger, monkeypatch)["gate_profile"] == {"unrecognised metadata key": "gateprofile"}


def test_without_the_metadata_the_input_has_no_profile(trigger, monkeypatch, s3):
    s3.metadata = {"other": "x"}
    assert "gate_profile" not in _start(trigger, monkeypatch)  # the state machine's default


def test_an_unknown_profile_is_passed_on_for_the_state_machine_to_refuse(trigger, monkeypatch, s3):
    # One place validates (Check_Gate_Profile): the run fails there with UnknownGateProfile.
    s3.metadata = {"gate-profile": "strict"}
    assert _start(trigger, monkeypatch)["gate_profile"] == "strict"


def test_a_vanished_upload_still_starts_its_run(trigger, monkeypatch, s3):
    # As before the profile: the run starts and QC fails on the missing upload, visibly.
    s3.missing = True
    assert "gate_profile" not in _start(trigger, monkeypatch)


def test_unreadable_metadata_goes_on_flagged_so_the_run_fails_visibly(trigger, monkeypatch, s3):
    from botocore.exceptions import ClientError

    def denied(Bucket, Key):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "HeadObject")

    s3.head_object = denied
    assert _start(trigger, monkeypatch)["gate_profile"] == {"metadata unreadable": "AccessDenied"}


# --- the reference genome per upload: S3 user metadata reference -> the input's reference_name ------


@pytest.mark.parametrize("key", ["reference", "reference-name", "reference_name"])
def test_an_uploads_reference_metadata_replaces_the_default(trigger, monkeypatch, s3, key):
    s3.metadata = {key: "GRCh38.fa"}
    assert _start(trigger, monkeypatch)["reference_name"] == "GRCh38.fa"
    assert s3.heads == ["raw_reads/S1.fastq.gz"]  # the same one HeadObject as the gate profile


def test_without_reference_metadata_the_default_stays(trigger, monkeypatch, s3):
    s3.metadata = {"gate-profile": "synthetic"}
    run_input = _start(trigger, monkeypatch)
    assert run_input["reference_name"] == "chr20.fa" and run_input["gate_profile"] == "synthetic"


def test_reference_metadata_wins_over_reference_name(trigger, monkeypatch, s3):
    # REFERENCE_NAME is the deployment's default; the upload's own metadata is more specific.
    calls = []

    class FakeSfn:
        def start_execution(self, **kwargs):
            calls.append(json.loads(kwargs["input"]))
            return {"executionArn": "arn:fake"}

    monkeypatch.setenv("STATE_MACHINE_ARN", "arn:sm")
    monkeypatch.setenv("REFERENCE_NAME", "other.fa")
    monkeypatch.setattr(trigger, "_sfn_client", FakeSfn())
    event = {"Records": [{"s3": {"bucket": {"name": "b"}, "object": {"key": "raw_reads/S1.fastq.gz"}}}]}
    trigger.handler(event, None)
    s3.metadata = {"reference": "GRCh38.fa"}
    trigger.handler(event, None)
    assert [c["reference_name"] for c in calls] == ["other.fa", "GRCh38.fa"]


@pytest.mark.parametrize("value", ["missing.fa", " GRCh38.fa", "sub/dir.fa", ""])
def test_a_reference_goes_on_exactly_as_uploaded_for_the_state_machine_to_check(trigger, monkeypatch, s3, value):
    # Check_Reference_Name / Check_Reference are the one validator: even an empty value is not defaulted.
    s3.metadata = {"reference": value}
    assert _start(trigger, monkeypatch)["reference_name"] == value


@pytest.mark.parametrize("key", ["ref", "refernce", "genome-ref"])
def test_a_misspelled_reference_key_goes_on_flagged_not_ignored(trigger, monkeypatch, s3, key):
    s3.metadata = {key: "GRCh38.fa"}
    assert _start(trigger, monkeypatch)["reference_name"] == {"unrecognised metadata key": key}


def test_unreadable_metadata_flags_the_reference_too(trigger, monkeypatch, s3):
    # Neither setting falls back to its default when the metadata couldn't be read.
    from botocore.exceptions import ClientError

    def denied(Bucket, Key):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "HeadObject")

    s3.head_object = denied
    run_input = _start(trigger, monkeypatch)
    assert run_input["reference_name"] == run_input["gate_profile"] == {"metadata unreadable": "AccessDenied"}


def test_a_vanished_upload_keeps_the_default_reference(trigger, monkeypatch, s3):
    s3.missing = True
    assert _start(trigger, monkeypatch)["reference_name"] == "chr20.fa"


# --- v1.3: the upload's size (HeadObject ContentLength) as input_bytes, for Choose_Route -------------------


def _start_sized(trigger, monkeypatch, event_size=None):
    calls = []

    class FakeSfn:
        def start_execution(self, **kwargs):
            calls.append(json.loads(kwargs["input"]))
            return {"executionArn": "arn:fake"}

    monkeypatch.setenv("STATE_MACHINE_ARN", "arn:sm")
    monkeypatch.delenv("REFERENCE_NAME", raising=False)
    monkeypatch.delenv("CALLER", raising=False)
    monkeypatch.setattr(trigger, "_sfn_client", FakeSfn())
    obj = {"key": "raw_reads/S1.fastq.gz", **({"size": event_size} if event_size is not None else {})}
    trigger.handler({"Records": [{"s3": {"bucket": {"name": "b"}, "object": obj}}]}, None)
    return calls[0]


def test_a_single_end_upload_passes_its_size(trigger, monkeypatch, s3):
    original = s3.head_object
    s3.head_object = lambda Bucket, Key: {**original(Bucket, Key), "ContentLength": 60_000_000_000}
    assert _start_sized(trigger, monkeypatch, event_size=1)["input_bytes"] == 60_000_000_000  # the HEAD wins


def test_without_a_head_size_the_events_size_is_used(trigger, monkeypatch, s3):
    s3.missing = True  # vanished: no HEAD; the event still says how big it was
    assert _start_sized(trigger, monkeypatch, event_size=1234)["input_bytes"] == 1234


def test_with_no_size_at_all_the_input_has_none(trigger, monkeypatch, s3):
    assert "input_bytes" not in _start_sized(trigger, monkeypatch)  # Choose_Route: today's path


def test_input_bytes_adds_up_mates_and_refuses_unknowns(trigger):
    assert trigger.input_bytes([{"ContentLength": 3}, {"ContentLength": 4}]) == 7
    assert trigger.input_bytes([{"ContentLength": 3}, {}]) is None
    assert trigger.input_bytes([{"ContentLength": True}]) is None
    assert trigger.input_bytes([]) is None
