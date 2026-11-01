import json

import pytest


@pytest.mark.parametrize("key,expected", [
    ("raw_reads/SRR062634.fastq.gz", "SRR062634"),
    ("raw_reads/small_test.fastq.gz", "small_test"),
    ("SRR1.fq.gz", "SRR1"),
])
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
