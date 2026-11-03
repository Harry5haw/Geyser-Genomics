"""Paired-end handling in the trigger Lambda: wait for both mates, then start exactly one run."""

import json
from datetime import UTC

import pytest
from botocore.exceptions import ClientError


@pytest.mark.parametrize(
    "key,expected",
    [
        ("raw_reads/S1.fastq.gz", ("S1", None)),
        ("raw_reads/S1_R1.fastq.gz", ("S1", "R1")),
        ("raw_reads/S1_R2.fastq.gz", ("S1", "R2")),
        ("raw_reads/my_sample_R2.fq.gz", ("my_sample", "R2")),
        ("raw_reads/run_1.fastq.gz", ("run_1", None)),  # SRA-style _1/_2 are NOT mates (decision D1, #57)
        ("raw_reads/R1.fastq.gz", ("R1", None)),  # a bare 'R1' is a sample name, not an empty ID
        ("raw_reads/S1_r1.fastq.gz", ("S1_r1", None)),  # case-sensitive, like the keys tasks.py reads
    ],
)
def test_parse_raw_key(trigger, key, expected):
    assert trigger.parse_raw_key(key) == expected


def test_mate_key_swaps_only_the_mate_suffix(trigger):
    assert trigger.mate_key("raw_reads/S1_R1.fastq.gz") == "raw_reads/S1_R2.fastq.gz"
    assert trigger.mate_key("raw_reads/S1_R2.fastq.gz") == "raw_reads/S1_R1.fastq.gz"
    assert trigger.mate_key("raw_reads/R1_R1.fastq.gz") == "raw_reads/R1_R2.fastq.gz"
    with pytest.raises(ValueError):
        trigger.mate_key("raw_reads/S1.fastq.gz")


@pytest.mark.parametrize("key", ["raw_reads/S1_R1.fq.gz", "raw_reads/S1_R2.fastq.bz2.gz", "raw_reads/S1_R1.FASTQ.gz"])
def test_mates_must_use_the_name_the_stages_read(trigger, key):
    # tasks.keys_for only reads <id>_R1.fastq.gz; any other name would never pair or never be found.
    with pytest.raises(ValueError, match="must be named"):
        trigger.mate_key(key)


V1, V2 = ('"abc"', "2026-11-01 20:00:00+00:00"), ('"def"', "2026-11-01 20:00:05+00:00")


def test_execution_name_is_deterministic_and_valid(trigger):
    a = trigger.paired_execution_name("S1", [V1, V2])
    assert a == trigger.paired_execution_name("S1", [("abc", V1[1]), ("def", V2[1])])  # quotes don't matter
    assert a != trigger.paired_execution_name("S1", [V1, ('"xyz"', V2[1])])  # new content -> new run
    assert a != trigger.paired_execution_name(
        "S1", [V1, (V2[0], "2026-11-02 09:00:00+00:00")]
    )  # same content re-uploaded
    long_name = trigger.paired_execution_name("x" * 64 + "+weird id", [V1, V2])
    assert len(long_name) <= 80
    assert all(c.isalnum() or c in "-_" for c in long_name)


def test_paired_name_uses_the_same_format_as_single_end(trigger):
    # <sample>-<UTC time of the later upload>-<hash>, like build_execution_name (#65)
    sample, stamp, digest = trigger.paired_execution_name("S1", [V1, V2]).split("-")
    assert sample == "S1" and stamp == "20261101T200005Z" and len(digest) == 8
    # order of arrival doesn't matter: the later LastModified wins whichever mate it belongs to
    assert trigger.paired_execution_name("S1", [V2, V1]).split("-")[1] == "20261101T200005Z"


def test_paired_name_accepts_headobject_datetimes(trigger):
    from datetime import datetime

    when = datetime(2026, 11, 1, 20, 0, 5, tzinfo=UTC)
    assert trigger.paired_execution_name("S1", [('"abc"', when), ('"def"', when)]).split("-")[1] == "20261101T200005Z"


def test_paired_upload_uses_the_same_pipeline_input_as_single_end(trigger, monkeypatch):
    monkeypatch.delenv("REFERENCE_NAME", raising=False)
    assert trigger.build_pipeline_input("raw_reads/S1_R2.fastq.gz") == {"srr_id": "S1", "reference_name": "chr20.fa"}


# --- handler ------------------------------------------------------------------


def _not_found():
    return ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")


class FakeS3:
    """objects: key -> ETag (LastModified is fixed) or key -> (ETag, LastModified); metadata: key -> dict."""

    def __init__(self, objects, error=None, metadata=None):
        self.objects, self.error, self.heads, self.metadata = objects, error, [], metadata or {}

    def head_object(self, Bucket, Key):
        self.heads.append(Key)
        if self.error:
            raise self.error
        if Key not in self.objects:
            raise _not_found()
        value = self.objects[Key]
        etag, modified = value if isinstance(value, tuple) else (value, "2026-11-01 20:00:00+00:00")
        return {"ETag": f'"{etag}"', "LastModified": modified, "Metadata": dict(self.metadata.get(Key, {}))}


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
    def install(objects, s3_error=None, metadata=None):
        s3, sfn = FakeS3(objects, s3_error, metadata), FakeSfn()
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
    assert "raw_reads/S1_R2.fastq.gz" in s3.heads
    assert sfn.started == []


@pytest.mark.parametrize("second", ["R1", "R2"])
def test_second_mate_starts_one_named_run_in_either_order(trigger, aws, second):
    s3, sfn = aws({"raw_reads/S1_R1.fastq.gz": "e1", "raw_reads/S1_R2.fastq.gz": "e2"})
    etag = {"R1": "e1", "R2": "e2"}[second]

    result = trigger.handler(event(f"raw_reads/S1_{second}.fastq.gz", etag), None)

    assert result["statusCode"] == 200
    (start,) = sfn.started
    assert json.loads(start["input"]) == {"srr_id": "S1", "reference_name": "chr20.fa"}
    fixed = "2026-11-01 20:00:00+00:00"
    # same name whichever mate came last
    assert start["name"] == trigger.paired_execution_name("S1", [('"e1"', fixed), ('"e2"', fixed)])


def test_reuploading_an_identical_pair_runs_again(trigger, aws, monkeypatch):
    # e.g. after a failed run and a pipeline fix: same bytes (same ETags), new LastModified.
    s3, sfn = aws({"raw_reads/S1_R1.fastq.gz": "e1", "raw_reads/S1_R2.fastq.gz": "e2"})
    trigger.handler(event("raw_reads/S1_R2.fastq.gz", "e2"), None)
    s3.objects = {
        "raw_reads/S1_R1.fastq.gz": ("e1", "2026-11-02 08:00:00+00:00"),
        "raw_reads/S1_R2.fastq.gz": ("e2", "2026-11-02 08:00:03+00:00"),
    }
    result = trigger.handler(event("raw_reads/S1_R2.fastq.gz", "e2"), None)
    assert "executionArn" in json.loads(result["body"])
    assert len(sfn.started) == 2 and sfn.started[0]["name"] != sfn.started[1]["name"]


def test_wrongly_named_mate_fails_loudly(trigger, aws):
    s3, sfn = aws({})
    with pytest.raises(ValueError, match="must be named"):
        trigger.handler(event("raw_reads/S1_R1.fq.gz", "e1"), None)
    assert sfn.started == []


def test_simultaneous_mates_start_exactly_one_run(trigger, aws):
    # Both files land before either Lambda runs: both events see both mates.
    s3, sfn = aws({"raw_reads/S1_R1.fastq.gz": "e1", "raw_reads/S1_R2.fastq.gz": "e2"})
    first = trigger.handler(event("raw_reads/S1_R1.fastq.gz", "e1"), None)
    second = trigger.handler(event("raw_reads/S1_R2.fastq.gz", "e2"), None)

    assert len(sfn.started) == 1
    assert first["statusCode"] == second["statusCode"] == 200
    assert json.loads(second["body"])["duplicate"] is True


def test_permission_error_on_the_mate_is_not_treated_as_waiting(trigger, aws):
    # Without s3:ListBucket a missing object is a 403. That must fail loudly, not silently never start.
    denied = ClientError({"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject")
    s3, sfn = aws({}, s3_error=denied)
    with pytest.raises(ClientError):
        trigger.handler(event("raw_reads/S1_R1.fastq.gz", "e1"), None)
    assert sfn.started == []


def test_single_end_upload_starts_immediately_reading_only_its_metadata(trigger, aws):
    # One HeadObject, for the gate profile (#157); the name still comes from the event (#65).
    s3, sfn = aws({"raw_reads/S1.fastq.gz": "e"})
    result = trigger.handler(event("raw_reads/S1.fastq.gz", "e"), None)
    assert result["statusCode"] == 200
    assert s3.heads == ["raw_reads/S1.fastq.gz"]
    assert sfn.started[0]["name"].startswith("S1-")


def test_other_start_errors_propagate(trigger, aws, monkeypatch):
    s3, sfn = aws({"raw_reads/S1.fastq.gz": "e"})

    def boom(**kwargs):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "StartExecution")

    monkeypatch.setattr(sfn, "start_execution", boom)
    with pytest.raises(ClientError):
        trigger.handler(event("raw_reads/S1.fastq.gz", "e"), None)


PAIR = {"raw_reads/S1_R1.fastq.gz": "e1", "raw_reads/S1_R2.fastq.gz": "e2"}


def test_a_pairs_gate_profile_can_be_on_either_mate(trigger, aws):
    s3, sfn = aws(PAIR, metadata={"raw_reads/S1_R2.fastq.gz": {"gate-profile": "synthetic"}})
    trigger.handler(event("raw_reads/S1_R1.fastq.gz", "e1"), None)
    assert json.loads(sfn.started[0]["input"])["gate_profile"] == "synthetic"


def test_mates_with_different_profiles_are_an_error(trigger, aws):
    meta = {
        "raw_reads/S1_R1.fastq.gz": {"gate-profile": "default"},
        "raw_reads/S1_R2.fastq.gz": {"gate-profile": "synthetic"},
    }
    s3, sfn = aws(PAIR, metadata=meta)
    with pytest.raises(ValueError, match="differ"):
        trigger.handler(event("raw_reads/S1_R1.fastq.gz", "e1"), None)
    assert sfn.started == []
