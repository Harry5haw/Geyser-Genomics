"""Stage logic that runs without bioinformatics tools or AWS."""
import gzip
import importlib
import importlib.util
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


def test_decompress_never_lets_storage_seek_the_gzip_stream(local_env, monkeypatch):
    # #71: in the cloud the raw read stream is an S3 body that can't seek. GzipFile over it still
    # says seekable(), so decompress must hand storage a stream that says it isn't.
    import io

    content = b"@r1\nACGT\n+\nIIII\n" * 50
    write_raw(local_env, "S1", gzip.compress(content))
    s = tasks.storage()
    real_open, real_upload = s.open_read, s.upload_stream

    class S3Body(io.RawIOBase):
        def __init__(self, fh):
            self._fh = fh

        def readable(self):
            return True

        def readinto(self, b):
            return self._fh.readinto(b)

        def seekable(self):
            return False

        def close(self):
            self._fh.close()
            super().close()

    seekable = []

    def upload(stream, key):
        seekable.append(stream.seekable())
        real_upload(stream, key)

    monkeypatch.setattr(s, "open_read", lambda key: S3Body(real_open(key)))
    monkeypatch.setattr(s, "upload_stream", upload)
    tasks.decompress_task("S1")
    assert seekable == [False]
    assert (local_env / "decompressed" / "S1.fastq").read_bytes() == content


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
    # Import a fresh copy rather than reload the shared module: a reload re-creates classes such as
    # LayoutError, and code that imported the old ones (the web UI) would then stop catching them.
    monkeypatch.delenv("BUCKET_NAME", raising=False)
    spec = importlib.util.spec_from_file_location("tasks_fresh_copy", tasks.__file__)
    spec.loader.exec_module(importlib.util.module_from_spec(spec))


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
    cmd = tasks.align_command("/w/ref.fa", "/w/S1.fastq", "/w/S1.bam", 4, "/w", "S1")
    bwa, sort, markdup = _align_stages(cmd)

    assert bwa[:4] == ["bwa", "mem", "-t", "4"] and bwa[-2:] == ["/w/ref.fa", "/w/S1.fastq"]
    assert bwa[bwa.index("-R") + 1] == "@RG\\tID:S1\\tSM:S1"  # literal backslash-t, as bwa expects
    assert "-p" not in bwa  # one FASTQ = single-end reads, never interleaved
    assert sort[:2] == ["samtools", "sort"] and "-u" in sort and sort[-1] == "-"
    assert sort[sort.index("-@") + 1] == "4"
    assert sort[sort.index("-m") + 1] == tasks.SORT_MEMORY_PER_THREAD
    assert markdup[:2] == ["samtools", "markdup"] and markdup[-2:] == ["-", "/w/S1.bam"]
    assert markdup[markdup.index("-@") + 1] == "4"


def test_align_command_marks_but_never_removes_duplicates():
    markdup = _align_stages(tasks.align_command("r.fa", "s.fq", "o.bam", 1, "/w", "S1"))[-1]
    assert "-r" not in markdup and "--remove" not in markdup
    assert "-s" in markdup  # duplicate stats go to the job log


def test_align_command_skips_fixmate_for_single_end_reads():
    # fixmate needs name-grouped read pairs; a single FASTQ is aligned single-end.
    assert "fixmate" not in tasks.align_command("r.fa", "s.fq", "o.bam", 1, "/w", "S1")


def test_align_command_keeps_temp_files_in_work_dir():
    sort, markdup = _align_stages(tasks.align_command("r.fa", "s.fq", "o.bam", 2, "/scratch/wd", "S1"))[1:]
    prefix = os.path.join("/scratch/wd", "")
    sort_tmp, markdup_tmp = sort[sort.index("-T") + 1], markdup[markdup.index("-T") + 1]
    assert sort_tmp.startswith(prefix) and markdup_tmp.startswith(prefix)
    assert sort_tmp != markdup_tmp


def test_align_command_quotes_paths_with_spaces():
    cmd = tasks.align_command("/my refs/r.fa", "/w/s.fq", "/out dir/o.bam", 1, "/w", "S1")
    bwa, _, markdup = _align_stages(cmd)
    assert "/my refs/r.fa" in bwa and markdup[-1] == "/out dir/o.bam"


def test_align_command_caps_sort_memory_on_big_hosts():
    # 16 threads x the 768 MB default would be ~12 GB of sort buffers next to bwa and markdup.
    bwa, sort, markdup = _align_stages(tasks.align_command("r.fa", "s.fq", "o.bam", 16, "/w", "S1"))
    assert bwa[bwa.index("-t") + 1] == "16"
    assert sort[sort.index("-@") + 1] == str(tasks.SORT_MAX_THREADS)
    assert sort[sort.index("-m") + 1] == tasks.SORT_MEMORY_PER_THREAD



def test_align_command_paired_end_runs_fixmate_between_bwa_and_sort():
    cmd = tasks.align_command("/w/ref.fa", ["/w/S1_R1.fastq", "/w/S1_R2.fastq"], "/w/S1.bam", 4, "/w", "S1")
    bwa, fixmate, sort, markdup = _align_stages(cmd)

    assert bwa[-3:] == ["/w/ref.fa", "/w/S1_R1.fastq", "/w/S1_R2.fastq"]  # R1 then R2
    assert fixmate == ["samtools", "fixmate", "-m", "-u", "-", "-"]  # -m adds the ms/MC tags markdup needs
    assert sort[:2] == ["samtools", "sort"] and markdup[:2] == ["samtools", "markdup"]


def test_align_command_rejects_wrong_number_of_fastqs():
    with pytest.raises(ValueError):
        tasks.align_command("r.fa", [], "o.bam", 1, "/w", "S1")
    with pytest.raises(ValueError):
        tasks.align_command("r.fa", ["a", "b", "c"], "o.bam", 1, "/w", "S1")


# --- layouts and keys ------------------------------------------------------------

def test_single_end_keys_are_unchanged():
    # Step Functions, the web UI and the DeepVariant task all depend on these exact keys.
    assert tasks.keys_for("S1") == {
        "raw": "raw_reads/S1.fastq.gz",
        "fastq": "decompressed/S1.fastq",
        "qc_html": "qc_reports/S1_fastqc.html",
        "qc_zip": "qc_reports/S1_fastqc.zip",
        "bam": "alignments/S1.bam",
        "vcf": "variants/S1.vcf.gz",
    }


def test_paired_end_keys_have_one_per_mate_and_one_bam():
    keys = tasks.keys_for("S1", "paired")
    assert tasks.keys_of(keys, "raw") == ["raw_reads/S1_R1.fastq.gz", "raw_reads/S1_R2.fastq.gz"]
    assert tasks.keys_of(keys, "fastq") == ["decompressed/S1_R1.fastq", "decompressed/S1_R2.fastq"]
    assert tasks.keys_of(keys, "qc_html") == ["qc_reports/S1_R1_fastqc.html", "qc_reports/S1_R2_fastqc.html"]
    assert tasks.keys_of(keys, "bam") == ["alignments/S1.bam"]
    assert keys["vcf"] == "variants/S1.vcf.gz"


@pytest.mark.parametrize("present,expected", [
    ({"raw_reads/S1.fastq.gz"}, "single"),
    ({"raw_reads/S1_R1.fastq.gz", "raw_reads/S1_R2.fastq.gz"}, "paired"),
])
def test_layout_from_raw(present, expected):
    assert tasks.layout_from_raw("S1", present.__contains__) == expected


@pytest.mark.parametrize("present,message", [
    ({"raw_reads/S1_R1.fastq.gz"}, "R2 is missing"),
    ({"raw_reads/S1_R2.fastq.gz"}, "R1 is missing"),
    ({"raw_reads/S1.fastq.gz", "raw_reads/S1_R1.fastq.gz"}, "both single-end and paired-end"),
    (set(), "no raw files"),
])
def test_layout_from_raw_refuses_to_guess(present, message):
    with pytest.raises(tasks.LayoutError, match=message):
        tasks.layout_from_raw("S1", present.__contains__)


def test_decompress_paired_end_writes_both_mates(local_env):
    r1, r2 = b"@a/1\nACGT\n+\nIIII\n" * 50, b"@a/2\nTTGC\n+\nIIII\n" * 50
    write_raw(local_env, "P1_R1", gzip.compress(r1))
    write_raw(local_env, "P1_R2", gzip.compress(r2))

    tasks.decompress_task("P1")

    assert (local_env / "decompressed" / "P1_R1.fastq").read_bytes() == r1
    assert (local_env / "decompressed" / "P1_R2.fastq").read_bytes() == r2
    assert not (local_env / "decompressed" / "P1.fastq").exists()


def test_decompress_refuses_a_lone_mate(local_env):
    write_raw(local_env, "P1_R1", gzip.compress(b"@a\nA\n+\nI\n"))
    with pytest.raises(tasks.LayoutError):
        tasks.decompress_task("P1")
    assert last_metric(local_env)["status"] == "Failure"
    assert not (local_env / "decompressed").exists()


def test_qc_paired_end_runs_fastqc_once_with_a_thread_per_mate(local_env, monkeypatch):
    import subprocess

    for mate in ("R1", "R2"):
        path = local_env / "decompressed" / f"P1_{mate}.fastq"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"@r\nA\n+\nI\n")
        write_raw(local_env, f"P1_{mate}", gzip.compress(b"@r\nA\n+\nI\n"))
    calls = []

    def fake_fastqc(cmd, check=False, timeout=None, **kwargs):
        calls.append(cmd)
        out_dir = cmd[cmd.index("-o") + 1]
        for fastq in (a for a in cmd if a.endswith(".fastq")):
            stem = os.path.basename(fastq)[:-len(".fastq")]
            for ext in ("html", "zip"):
                with open(os.path.join(out_dir, f"{stem}_fastqc.{ext}"), "w") as fh:
                    fh.write(stem)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(tasks.subprocess, "run", fake_fastqc)
    tasks.qc_task("P1")

    (cmd,) = calls
    assert cmd[:3] == ["fastqc", "-t", "2"]  # R1 and R2 in parallel: same wall time as one file
    for mate in ("R1", "R2"):
        assert (local_env / "qc_reports" / f"P1_{mate}_fastqc.html").read_text() == f"P1_{mate}"



def test_later_stages_detect_the_layout_from_their_own_inputs():
    # qc/align read decompressed/; they must not need the raw upload to still be there.
    present = {"decompressed/S1_R1.fastq", "decompressed/S1_R2.fastq"}
    assert tasks.layout_from_keys("S1", present.__contains__, "fastq") == "paired"
    assert tasks.layout_from_keys("S1", {"decompressed/S1.fastq"}.__contains__, "fastq") == "single"
    with pytest.raises(tasks.LayoutError, match="fastq R1 exists but R2 is missing"):
        tasks.layout_from_keys("S1", {"decompressed/S1_R1.fastq"}.__contains__, "fastq")
