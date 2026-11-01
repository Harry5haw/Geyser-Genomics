"""Paired-end handling in the trigger Lambda: wait for both mates, then start exactly one run."""
import json

import pytest
from botocore.exceptions import ClientError


@pytest.mark.parametrize("key,expected", [
    ("raw_reads/S1.fastq.gz", ("S1", None)),
    ("raw_reads/S1_R1.fastq.gz", ("S1", "R1")),
    ("raw_reads/S1_R2.fastq.gz", ("S1", "R2")),
    ("raw_reads/my_sample_R2.fq.gz", ("my_sample", "R2")),
    ("raw_reads/run_1.fastq.gz", ("run_1", None)),  # SRA-style _1/_2 are NOT mates (decision D1, #57)
    ("raw_reads/R1.fastq.gz", ("R1", None)),        # a bare 'R1' is a sample name, not an empty ID
    ("raw_reads/S1_r1.fastq.gz", ("S1_r1", None)),  # case-sensitive, like the keys tasks.py reads
])
def test_parse_raw_key(trigger, key, expected):
    assert trigger.parse_raw_key(key) == expected


def test_mate_key_swaps_only_the_mate_suffix(trigger):
    assert trigger.mate_key("raw_reads/S1_R1.fastq.gz") == "raw_reads/S1_R2.fastq.gz"
    assert trigger.mate_key("raw_reads/S1_R2.fastq.gz") == "raw_reads/S1_R1.fastq.gz"
    assert trigger.mate_key("raw_reads/R1_R1.fastq.gz") == "raw_reads/R1_R2.fastq.gz"
    with pytest.raises(ValueError):
        trigger.mate_key("raw_reads/S1.fastq.gz")


def test_execution_name_is_deterministic_and_valid(trigger):
    a = trigger.execution_name("S1", ['"abc"', '"def"'])
    assert a == trigger.execution_name("S1", ["abc", "def"])  # event ETags are unquoted, HeadObject's quoted
    assert a != trigger.execution_name("S1", ["abc", "xyz"])  # re-upload with new content -> new run
    long_name = trigger.execution_name("x" * 64 + "+weird id", ["a", "b"])
    assert len(long_name) <= 80
    assert all(c.isalnum() or c in "-_" for c in long_name)


def test_paired_upload_uses_the_same_pipeline_input_as_single_end(trigger, monkeypatch):
    monkeypatch.delenv("REFERENCE_NAME", raising=False)
    assert trigger.build_pipeline_input("raw_reads/S1_R2.fastq.gz") == {"srr_id": "S1", "reference_name": "chr20.fa"}


# --- handler ------------------------------------------------------------------

def _not_found():
    return ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")


class FakeS3:
    def __init__(self, objects, error=None):
        self.objects, self.error, self.heads = objects, error, []

    def head_object(self, Bucket, Key):
        self.heads.append(Key)
        if self.error:
            raise self.error
        if Key not in self.objects:
            raise _not_found()
        return {"ETag": f'"{self.objects[Key]}"'}


class FakeSfn:
    """Behaves like a STANDARD state machine: a second start under an existing name is refused."""

    def __init__(self):
        self.started = []

    def start_execution(self, **kwargs):
        name = kwargs.get("name")
        if name and any(s.get("name") == name for s in self.started):
            raise ClientError({"Error": {"Code": "ExecutionAlreadyExists", "Message": "exists"}}, "StartExecution")
        self.started.append(kwargs)
        return {"executionArn": f"arn:exec:{name or len(self.started)}"}


@pytest.fixture
def aws(trigger, monkeypatch):
    def install(objects, s3_error=None):
        s3, sfn = FakeS3(objects, s3_error), FakeSfn()
        monkeypatch.setattr(trigger, "_s3_client", s3)
        monkeypatch.setattr(trigger, "_sfn_client", sfn)
        return s3, sfn

    monkeypatch.setenv("STATE_MACHINE_ARN", "arn:sm")
    monkeypatch.delenv("REFERENCE_NAME", raising=False)
    return install


def event(key, etag):
    return {"Records": [{"s3": {"bucket": {"name": "b"}, "object": {"key": key, "eTag": etag}}}]}


def test_first_mate_waits_without_starting(trigger, aws):
    s3, sfn = aws({"raw_reads/S1_R1.fastq.gz": "e1"})
    result = trigger.handler(event("raw_reads/S1_R1.fastq.gz", "e1"), None)
    assert result["statusCode"] == 202
    assert json.loads(result["body"]) == {"waiting_for": "raw_reads/S1_R2.fastq.gz"}
    assert s3.heads == ["raw_reads/S1_R2.fastq.gz"]
    assert sfn.started == []


@pytest.mark.parametrize("second", ["R1", "R2"])
def test_second_mate_starts_one_named_run_in_either_order(trigger, aws, second):
    s3, sfn = aws({"raw_reads/S1_R1.fastq.gz": "e1", "raw_reads/S1_R2.fastq.gz": "e2"})
    etag = {"R1": "e1", "R2": "e2"}[second]

    result = trigger.handler(event(f"raw_reads/S1_{second}.fastq.gz", etag), None)

    assert result["statusCode"] == 200
    (start,) = sfn.started
    assert json.loads(start["input"]) == {"srr_id": "S1", "reference_name": "chr20.fa"}
    assert start["name"] == trigger.execution_name("S1", ["e1", "e2"])  # same name whichever mate came last


def test_simultaneous_mates_start_exactly_one_run(trigger, aws):
    # Both files land before either Lambda runs: both events see both mates.
    s3, sfn = aws({"raw_reads/S1_R1.fastq.gz": "e1", "raw_reads/S1_R2.fastq.gz": "e2"})
    first = trigger.handler(event("raw_reads/S1_R1.fastq.gz", "e1"), None)
    second = trigger.handler(event("raw_reads/S1_R2.fastq.gz", "e2"), None)

    assert len(sfn.started) == 1
    assert first["statusCode"] == second["statusCode"] == 200
    assert "already_started" in json.loads(second["body"])


def test_permission_error_on_the_mate_is_not_treated_as_waiting(trigger, aws):
    # Without s3:ListBucket a missing object is a 403. That must fail loudly, not silently never start.
    denied = ClientError({"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject")
    s3, sfn = aws({}, s3_error=denied)
    with pytest.raises(ClientError):
        trigger.handler(event("raw_reads/S1_R1.fastq.gz", "e1"), None)
    assert sfn.started == []


def test_single_end_upload_starts_immediately_without_s3_calls(trigger, aws):
    s3, sfn = aws({})
    result = trigger.handler(event("raw_reads/S1.fastq.gz", "e"), None)
    assert result["statusCode"] == 200
    assert s3.heads == []
    assert "name" not in sfn.started[0]  # unchanged single-end behaviour


def test_other_start_errors_propagate(trigger, aws, monkeypatch):
    s3, sfn = aws({})

    def boom(**kwargs):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "StartExecution")

    monkeypatch.setattr(sfn, "start_execution", boom)
    with pytest.raises(ClientError):
        trigger.handler(event("raw_reads/S1.fastq.gz", "e"), None)
