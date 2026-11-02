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


def test_decompress_is_no_longer_a_stage():
    with pytest.raises(SystemExit):
        tasks.main(["decompress", "S1"])


def test_failed_stage_raises_and_records_failure(local_env):
    with pytest.raises(tasks.LayoutError):
        tasks.qc_task("MISSING")
    metric = last_metric(local_env)
    assert metric["task"] == "QualityControl" and metric["status"] == "Failure"


def test_metrics_outage_does_not_fail_stage(local_env, monkeypatch):
    class Broken:
        def record(self, *a, **k):
            raise RuntimeError("cloudwatch down")

    monkeypatch.setattr(tasks, "_metrics", Broken())
    assert tasks.timed_stage("Example")(lambda sample_id: "done")("S1") == "done"  # must not raise


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

    assert bwa[:4] == ["bwa-mem2", "mem", "-t", "4"] and bwa[-2:] == ["/w/ref.fa", "/w/S1.fastq"]
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


def test_align_command_runs_bwa_when_asked():
    # Same arguments for both aligners: only the program name changes.
    default = tasks.align_command("/w/ref.fa", "/w/S1.fastq", "/w/S1.bam", 4, "/w", "S1")
    bwa = tasks.align_command("/w/ref.fa", "/w/S1.fastq", "/w/S1.bam", 4, "/w", "S1", aligner="bwa")
    assert bwa.startswith("bwa mem ") and default.startswith("bwa-mem2 mem ")
    assert bwa.replace("bwa mem ", "", 1) == default.replace("bwa-mem2 mem ", "", 1)


def test_align_command_rejects_unknown_aligner():
    with pytest.raises(ValueError, match="minimap2"):
        tasks.align_command("r.fa", "s.fq", "o.bam", 1, "/w", "S1", aligner="minimap2")


@pytest.mark.parametrize("value, expected", [(None, "bwa-mem2"), ("", "bwa-mem2"), ("bwa", "bwa"),
                                             (" bwa-mem2 ", "bwa-mem2")])
def test_aligner_from_env(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("ALIGNER", raising=False)
    else:
        monkeypatch.setenv("ALIGNER", value)
    assert tasks.aligner_from_env() == expected


def test_aligner_from_env_rejects_unknown_names(monkeypatch):
    monkeypatch.setenv("ALIGNER", "BWA")  # names are exact: the value is the program that runs
    with pytest.raises(ValueError, match="BWA"):
        tasks.aligner_from_env()


def test_every_aligner_index_has_the_files_bwa_mem_reads():
    # .amb/.ann/.pac are common to both formats; each aligner adds its own suffix-array files.
    for exts in tasks.ALIGNER_INDEX_EXTS.values():
        assert {".amb", ".ann", ".pac"} <= set(exts) and len(set(exts)) == len(exts)
    assert {".0123", ".bwt.2bit.64"} <= set(tasks.ALIGNER_INDEX_EXTS["bwa-mem2"])


def test_align_command_writes_cram_against_the_reference():
    markdup = _align_stages(tasks.align_command("/w/ref.fa", "/w/S1.fastq", "/w/S1.cram", 4, "/w", "S1"))[-1]
    assert markdup[markdup.index("--reference") + 1] == "/w/ref.fa"
    assert markdup[markdup.index("-O") + 1] == "cram"
    assert markdup[-2:] == ["-", "/w/S1.cram"]


def test_align_command_writes_bam_for_a_bam_path():
    markdup = _align_stages(tasks.align_command("/w/ref.fa", "/w/S1.fastq", "/w/S1.bam", 4, "/w", "S1"))[-1]
    assert "-O" not in markdup and "--reference" not in markdup


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
        "qc_html": "qc_reports/S1.fastp.html",
        "qc_json": "qc_reports/S1.fastp.json",
        "cram": "alignments/S1.cram",
        "crai": "alignments/S1.cram.crai",
        "bam": "alignments/S1.bam",
        "vcf": "variants/S1.vcf.gz",
        "tbi": "variants/S1.vcf.gz.tbi",
        "flagstat": "stats/S1/flagstat.txt",
        "mosdepth_summary": "stats/S1/mosdepth.summary.txt",
        "mosdepth_dist": "stats/S1/mosdepth.global.dist.txt",
        "bcftools_stats": "stats/S1/bcftools.stats.txt",
    }


def test_paired_end_keys_have_one_raw_per_mate_and_one_of_everything_else():
    keys = tasks.keys_for("S1", "paired")
    assert tasks.keys_of(keys, "raw") == ["raw_reads/S1_R1.fastq.gz", "raw_reads/S1_R2.fastq.gz"]
    assert tasks.keys_of(keys, "qc_html") == ["qc_reports/S1.fastp.html"]  # one fastp report for the pair
    assert keys["qc_json"] == "qc_reports/S1.fastp.json"
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


def test_qc_refuses_a_lone_mate(local_env):
    write_raw(local_env, "P1_R1", gzip.compress(b"@a\nA\n+\nI\n"))
    with pytest.raises(tasks.LayoutError, match="R2 is missing"):
        tasks.qc_task("P1")
    assert last_metric(local_env)["status"] == "Failure"
    assert not (local_env / "qc_reports").exists()


def test_qc_paired_end_runs_fastp_once_with_both_mates(local_env, monkeypatch):
    import subprocess

    for mate in ("R1", "R2"):
        write_raw(local_env, f"P1_{mate}", gzip.compress(b"@r\nA\n+\nI\n"))
    calls = []

    def fake_fastp(cmd, check=False, timeout=None, **kwargs):
        calls.append(cmd)
        for flag in ("--html", "--json"):
            with open(cmd[cmd.index(flag) + 1], "w") as fh:
                fh.write(flag)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(tasks.subprocess, "run", fake_fastp)
    tasks.qc_task("P1")

    (cmd,) = calls
    assert cmd[0] == "fastp"
    assert os.path.basename(cmd[cmd.index("-i") + 1]) == "P1_R1.fastq.gz"  # the upload, still gzipped
    assert os.path.basename(cmd[cmd.index("-I") + 1]) == "P1_R2.fastq.gz"
    assert (local_env / "qc_reports" / "P1.fastp.html").read_text() == "--html"
    assert (local_env / "qc_reports" / "P1.fastp.json").read_text() == "--json"
    assert not list((local_env / "qc_reports").glob("P1_R*"))


def test_qc_command_reports_only_and_never_writes_reads():
    cmd = tasks.qc_command(["a.fastq.gz"], "r.html", "r.json", threads=64, sample_id="S1")
    assert cmd[:3] == ["fastp", "-i", "a.fastq.gz"] and "-I" not in cmd
    # No output reads at all, and nothing trimmed or filtered: the report describes the upload as is.
    for flag in ("-o", "-O", "--out1", "--out2"):
        assert flag not in cmd
    for flag in ("--disable_adapter_trimming", "--disable_quality_filtering", "--disable_length_filtering",
                 "--disable_trim_poly_g"):
        assert flag in cmd
    assert cmd[cmd.index("--thread") + 1] == str(tasks.QC_MAX_THREADS)  # fastp caps at 16
    assert cmd[cmd.index("--json") + 1] == "r.json" and cmd[cmd.index("--html") + 1] == "r.html"
    assert tasks.qc_command("a.fastq.gz", "r.html", "r.json", 1, "S1")[:3] == ["fastp", "-i", "a.fastq.gz"]
    with pytest.raises(ValueError):
        tasks.qc_command(["a", "b", "c"], "r.html", "r.json", 1, "S1")


def test_qc_counts_fastqc_reports_from_before_fastp():
    present = {"qc_reports/S1_fastqc.html", "qc_reports/P1_R1_fastqc.html", "qc_reports/P1_R2_fastqc.html",
               "qc_reports/H1_R1_fastqc.html", "qc_reports/N1.fastp.html"}
    done = lambda sample, layout: tasks.output_present(sample, layout, "qc_html", present.__contains__)  # noqa: E731
    assert done("S1", "single") and done("P1", "paired") and done("N1", "single") and done("N1", "paired")
    assert not done("H1", "paired")  # only R1's report: QC never finished
    assert not tasks.output_present("S1", "single", "bam", present.__contains__)  # legacy only applies to QC
