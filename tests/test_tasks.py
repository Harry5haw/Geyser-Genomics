"""Stage logic that runs without bioinformatics tools or AWS."""
import gzip
import importlib
import json
import os
import shlex

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


def _align_stages(cmd):
    # Split on unquoted | tokens only, so a quoted path containing "|" stays in its stage.
    stages = [[]]
    for token in shlex.split(cmd):
        if token == "|":
            stages.append([])
        else:
            stages[-1].append(token)
    return stages


def test_align_command_marks_duplicates_after_coordinate_sort():
    cmd = tasks.align_command("/w/ref.fa", "/w/S1.fastq", "/w/S1.bam", 4, "/w")
    bwa, sort, markdup = _align_stages(cmd)

    assert bwa[:4] == ["bwa", "mem", "-t", "4"] and bwa[-2:] == ["/w/ref.fa", "/w/S1.fastq"]
    assert "-p" not in bwa  # one FASTQ per sample = single-end reads
    assert sort[:2] == ["samtools", "sort"] and "-u" in sort and sort[-1] == "-"
    assert sort[sort.index("-@") + 1] == "4"
    assert sort[sort.index("-m") + 1] == tasks.SORT_MEMORY_PER_THREAD
    assert markdup[:2] == ["samtools", "markdup"] and markdup[-2:] == ["-", "/w/S1.bam"]
    assert markdup[markdup.index("-@") + 1] == "4"


def test_align_command_marks_but_never_removes_duplicates():
    markdup = _align_stages(tasks.align_command("r.fa", "s.fq", "o.bam", 1, "/w"))[-1]
    assert "-r" not in markdup and "--remove" not in markdup
    assert "-s" in markdup  # duplicate stats go to the job log


def test_align_command_skips_fixmate_for_single_end_reads():
    # fixmate needs name-grouped read pairs; the pipeline only ever aligns single-end reads.
    assert "fixmate" not in tasks.align_command("r.fa", "s.fq", "o.bam", 1, "/w")


def test_align_command_keeps_temp_files_in_work_dir():
    sort, markdup = _align_stages(tasks.align_command("r.fa", "s.fq", "o.bam", 2, "/scratch/wd"))[1:]
    prefix = os.path.join("/scratch/wd", "")
    sort_tmp, markdup_tmp = sort[sort.index("-T") + 1], markdup[markdup.index("-T") + 1]
    assert sort_tmp.startswith(prefix) and markdup_tmp.startswith(prefix)
    assert sort_tmp != markdup_tmp


def test_align_command_quotes_paths_with_spaces():
    cmd = tasks.align_command("/my refs/r.fa", "/w/s.fq", "/out dir/o.bam", 1, "/w")
    bwa, _, markdup = _align_stages(cmd)
    assert "/my refs/r.fa" in bwa and markdup[-1] == "/out dir/o.bam"


def test_align_command_caps_sort_memory_on_big_hosts():
    # 16 threads x the 768 MB default would be ~12 GB of sort buffers next to bwa and markdup.
    bwa, sort, markdup = _align_stages(tasks.align_command("r.fa", "s.fq", "o.bam", 16, "/w"))
    assert bwa[bwa.index("-t") + 1] == "16"
    assert sort[sort.index("-@") + 1] == str(tasks.SORT_MAX_THREADS)
    assert sort[sort.index("-m") + 1] == tasks.SORT_MEMORY_PER_THREAD
