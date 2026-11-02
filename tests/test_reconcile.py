"""scripts/reconcile.py on a temporary local bucket and a fake Step Functions client (no AWS)."""
import json
from unittest import mock

import reconcile
from storage import LocalStorage

SFN_ARN = "arn:aws:states:eu-west-2:000000000000:stateMachine:geyser-pipeline-sfn-dev"


def make_bucket(tmp_path, keys):
    for key in keys:
        path = tmp_path.joinpath(*key.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    return LocalStorage(str(tmp_path))


def full_sample(sample_id):
    return [f"raw_reads/{sample_id}.fastq.gz", f"decompressed/{sample_id}.fastq",
            f"qc_reports/{sample_id}_fastqc.html", f"qc_reports/{sample_id}_fastqc.zip",
            f"alignments/{sample_id}.bam", f"variants/{sample_id}.vcf.gz"]


KEYS = (full_sample("S1") + ["variants_deepvariant/S1.vcf.gz"]
        + ["raw_reads/S2.fastq.gz"]                                                    # not started
        + ["raw_reads/S3.fastq.gz", "decompressed/S3.fastq", "qc_reports/S3_fastqc.html"]  # stopped after qc
        + ["raw_reads/S4.fq.gz", "raw_reads/notes.txt", "raw_reads/sub/S5.fastq.gz", "raw_reads/S7.v2.fastq.gz"]
        + ["variants/S6.vcf.gz", "variants_pangenome/S9.vcf.gz"]                       # orphans
        + ["metrics/metrics.jsonl"])


def by_sample(result):
    return {r["sample_id"]: r for r in result["samples"]}


def test_statuses_and_upload_problems(tmp_path):
    result = reconcile.reconcile(make_bucket(tmp_path, KEYS).list_keys(""))
    rows = by_sample(result)
    assert {s: r["status"] for s, r in rows.items()} == {
        "S1": "complete", "S2": "not started", "S3": "incomplete", "S6": "orphan", "S9": "orphan"}
    assert rows["S1"]["extra_outputs"] == {"dv": True, "pg": False} and rows["S1"]["problem"] is None
    assert rows["S9"]["extra_outputs"] == {"dv": False, "pg": True}
    assert "last output from 'qc'" in rows["S3"]["note"]
    assert rows["S6"]["problem"] is None  # worth knowing, not a failure

    problems = {p["key"]: p for p in result["upload_problems"]}
    assert problems["raw_reads/notes.txt"]["problem"] == "ignored"
    for key, sample in (("raw_reads/S4.fq.gz", "S4"), ("raw_reads/sub/S5.fastq.gz", "S5"),
                        ("raw_reads/S7.v2.fastq.gz", "S7")):
        assert problems[key]["problem"] == "will fail"
        assert f"decompress reads raw_reads/{sample}.fastq.gz" in problems[key]["detail"]
    assert result["problem_count"] == 4 + 2  # 4 upload problems + S2 and S3


def fake_sfn(running_inputs):
    client = mock.Mock()
    client.list_executions.side_effect = [
        {"executions": [{"executionArn": f"e{i}"} for i in range(2)], "nextToken": "t"},
        {"executions": [{"executionArn": f"e{i}"} for i in range(2, len(running_inputs))]},
    ]
    client.describe_execution.side_effect = lambda executionArn: {
        "input": json.dumps({"srr_id": running_inputs[int(executionArn[1:])]})}
    return client


def test_open_runs_and_concurrent_runs(tmp_path):
    sfn = fake_sfn(["S2", "S2", "S3", "S8"])
    running = reconcile.open_runs(sfn, SFN_ARN)
    assert running == {"S2": 2, "S3": 1, "S8": 1}
    assert sfn.list_executions.call_args_list[1].kwargs["nextToken"] == "t"
    assert sfn.list_executions.call_args_list[0].kwargs["statusFilter"] == "RUNNING"

    rows = by_sample(reconcile.reconcile(make_bucket(tmp_path, KEYS).list_keys(""), running))
    assert rows["S2"]["status"] == "running" and rows["S2"]["problem"] == "concurrent runs"
    assert "overwrite each other's outputs" in rows["S2"]["note"]
    assert rows["S3"]["status"] == "running" and rows["S3"]["problem"] is None
    assert rows["S8"]["status"] == "running" and rows["S8"]["raw"] is False  # open run, nothing in the bucket yet


def test_cli_report_json_and_exit_code(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("SFN_ARN", raising=False)
    make_bucket(tmp_path, KEYS)
    assert reconcile.main(["--local-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "S1" in out and "complete" in out and "Upload problems:" in out and "6 problem(s)" in out
    assert " S1                      complete     D Q A V dv" in out
    assert "!S3                      incomplete   D Q . . .." in out

    assert reconcile.main(["--local-dir", str(tmp_path), "--json", "--fail-on-problems"]) == 1
    assert json.loads(capsys.readouterr().out)["problem_count"] == 6


def test_clean_bucket_passes(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("SFN_ARN", raising=False)
    make_bucket(tmp_path, full_sample("S1"))
    assert reconcile.main(["--local-dir", str(tmp_path), "--fail-on-problems"]) == 0
    assert "1 complete; 0 problem(s)" in capsys.readouterr().out


def test_cli_uses_state_machine_when_given(tmp_path, capsys):
    sfn = fake_sfn(["S2", "S2"])
    reconcile.main(["--local-dir", str(make_bucket(tmp_path, KEYS).root), "--state-machine-arn", SFN_ARN],
                   sfn_client=sfn)
    assert "2 runs at once" in capsys.readouterr().out


def test_patterns_follow_keys_for():
    patterns = reconcile.key_patterns(reconcile.extra_outputs())
    assert any(p.match("alignments/ABC.bam") for p in patterns)
    assert not any(p.match("raw_reads/ABC.fastq.gz") for p in patterns)
    assert not any(p.match("raw_reads/ABC_R1.fastq.gz") for p in patterns)
    assert not any(p.match("alignments/sub/ABC.bam") for p in patterns)
    assert reconcile.match_sample("decompressed/ABC_R2.fastq", patterns) == "ABC"
    assert reconcile.match_sample("qc_reports/ABC_R1_fastqc.zip", patterns) == "ABC"
    assert reconcile.match_sample("qc_reports/ABC_fastqc.zip", patterns) == "ABC"


def paired_sample(sample_id):
    return ([f"raw_reads/{sample_id}_{m}.fastq.gz" for m in ("R1", "R2")]
            + [f"decompressed/{sample_id}_{m}.fastq" for m in ("R1", "R2")]
            + [f"qc_reports/{sample_id}_{m}_fastqc.{ext}" for m in ("R1", "R2") for ext in ("html", "zip")]
            + [f"alignments/{sample_id}.bam", f"variants/{sample_id}.vcf.gz"])


PAIRED_KEYS = (paired_sample("P1")
               + ["raw_reads/P2_R1.fastq.gz"]                                           # lone mate
               + ["raw_reads/P3_R1.fastq.gz", "raw_reads/P3_R2.fastq.gz",
                  "decompressed/P3_R1.fastq", "decompressed/P3_R2.fastq",
                  "qc_reports/P3_R1_fastqc.html"]                                       # QC for R1 only
               + ["raw_reads/P4.fastq.gz", "raw_reads/P4_R1.fastq.gz", "raw_reads/P4_R2.fastq.gz"]
               + ["raw_reads/P5_R1.fq.gz", "raw_reads/sub/P6_R1.fastq.gz"]
               + ["decompressed/P7_R1.fastq", "decompressed/P7_R2.fastq"]               # orphan, paired
               + full_sample("run_1"))  # SRA-style _1 is not a mate: run_1 is a single-end sample


def test_paired_end_samples(tmp_path):
    result = reconcile.reconcile(make_bucket(tmp_path, PAIRED_KEYS).list_keys(""))
    rows = by_sample(result)
    assert {s: r["status"] for s, r in rows.items()} == {
        "P1": "complete", "P2": "lone mate", "P3": "incomplete", "P4": "both layouts", "P7": "orphan",
        "run_1": "complete"}
    assert rows["P1"]["layout"] == "paired" and rows["P1"]["problem"] is None
    assert all(rows["P1"]["outputs"].values())
    assert rows["run_1"]["layout"] == "single"
    assert rows["P2"]["problem"] == "lone mate" and "R2 missing" in rows["P2"]["note"]
    # One mate's QC report is not a QC output, but it does show the run reached qc.
    assert rows["P3"]["outputs"] == {"fastq": True, "qc_html": False, "bam": False, "vcf": False}
    assert "last output from 'qc'" in rows["P3"]["note"]
    assert rows["P4"]["problem"] == "both layouts" and rows["P4"]["layout"] is None
    assert rows["P7"]["raw"] is False and rows["P7"]["outputs"]["fastq"] is True
    # Per-mate keys never show up as samples of their own.
    assert not any(s.endswith(("_R1", "_R2")) for s in rows)

    problems = {p["key"]: p for p in result["upload_problems"]}
    assert set(problems) == {"raw_reads/P5_R1.fq.gz", "raw_reads/sub/P6_R1.fastq.gz"}
    assert problems["raw_reads/P5_R1.fq.gz"]["problem"] == "refused"
    assert "_R1.fastq.gz" in problems["raw_reads/P5_R1.fq.gz"]["detail"]
    assert problems["raw_reads/sub/P6_R1.fastq.gz"]["problem"] == "will fail"
    detail = problems["raw_reads/sub/P6_R1.fastq.gz"]["detail"]
    assert "waits for raw_reads/sub/P6_R2.fastq.gz" in detail and "stages read raw_reads/P6_R1.fastq.gz" in detail
    assert result["problem_count"] == 2 + 3  # 2 upload problems + P2, P3, P4


def test_paired_end_report(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("SFN_ARN", raising=False)
    make_bucket(tmp_path, paired_sample("P1"))
    assert reconcile.main(["--local-dir", str(tmp_path), "--fail-on-problems"]) == 0
    out = capsys.readouterr().out
    assert " P1                      complete     D Q A V .. ..  paired-end" in out
    assert "1 complete; 0 problem(s)" in out


def test_folder_markers_are_not_uploads():
    result = reconcile.reconcile(["raw_reads/", "raw_reads/sub/"] + full_sample("S1"))
    assert result["upload_problems"] == [] and result["problem_count"] == 0


def test_warns_when_open_runs_are_not_checked(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("SFN_ARN", raising=False)
    make_bucket(tmp_path, full_sample("S1"))
    reconcile.main(["--local-dir", str(tmp_path)])
    assert "open runs aren't checked" in capsys.readouterr().err
    reconcile.main(["--local-dir", str(tmp_path), "--state-machine-arn", SFN_ARN], sfn_client=fake_sfn(["S8", "S8"]))
    assert "open runs aren't checked" not in capsys.readouterr().err


def test_one_decompressed_mate_is_incomplete_not_unstarted():
    rows = by_sample(reconcile.reconcile(["raw_reads/S1_R1.fastq.gz", "raw_reads/S1_R2.fastq.gz",
                                          "decompressed/S1_R1.fastq"]))
    assert rows["S1"]["status"] == "incomplete" and rows["S1"]["outputs"]["fastq"] is False
    assert "last output from 'decompress'" in rows["S1"]["note"]
