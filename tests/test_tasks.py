"""Stage logic that runs without bioinformatics tools or AWS."""
import gzip
import importlib
import json

import pytest

import tasks


def write_raw(root, sample, data):
    raw = root / "raw_reads" / f"{sample}.fastq.gz"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(data)


def last_metric(root):
    return json.loads((root / "metrics" / "metrics.jsonl").read_text().splitlines()[-1])


def test_decompress_writes_plain_fastq(local_env):
    content = b"@r1\nACGT\n+\nIIII\n" * 500
    write_raw(local_env, "S1", gzip.compress(content))

    tasks.decompress_task("S1")

    assert (local_env / "decompressed" / "S1.fastq").read_bytes() == content
    metric = last_metric(local_env)
    assert metric["task"] == "Decompress" and metric["status"] == "Success"


def test_decompress_handles_multi_member_gzip(local_env):
    # bgzip output and concatenated .gz files have several gzip members; all must be read.
    write_raw(local_env, "S1", gzip.compress(b"@a\nA\n+\nI\n") + gzip.compress(b"@b\nC\n+\nI\n"))
    tasks.decompress_task("S1")
    assert (local_env / "decompressed" / "S1.fastq").read_bytes() == b"@a\nA\n+\nI\n@b\nC\n+\nI\n"


def test_failed_stage_raises_and_records_failure(local_env):
    with pytest.raises(Exception):
        tasks.decompress_task("MISSING")
    assert last_metric(local_env)["status"] == "Failure"


def test_corrupt_input_fails_the_stage(local_env):
    # The old implementation swallowed upload errors in a thread and could report success.
    write_raw(local_env, "S1", b"this is not gzip")
    with pytest.raises(Exception):
        tasks.decompress_task("S1")
    assert last_metric(local_env)["status"] == "Failure"


def test_metrics_outage_does_not_fail_stage(local_env, monkeypatch):
    class Broken:
        def record(self, *a, **k):
            raise RuntimeError("cloudwatch down")

    monkeypatch.setattr(tasks, "_metrics", Broken())
    write_raw(local_env, "S1", gzip.compress(b"@r\nA\n+\nI\n"))
    tasks.decompress_task("S1")  # must not raise


def test_align_requires_reference_argument():
    with pytest.raises(SystemExit):
        tasks.main(["align", "S1"])


def test_importing_tasks_needs_no_aws(monkeypatch):
    # Regression: the old tasks.py exited at import time without BUCKET_NAME.
    monkeypatch.delenv("BUCKET_NAME", raising=False)
    importlib.reload(tasks)
