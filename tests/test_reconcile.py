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
        + ["variants/S6.vcf.gz"]                                                       # orphan
        + ["metrics/metrics.jsonl"])


def by_sample(result):
    return {r["sample_id"]: r for r in result["samples"]}


def test_statuses_and_upload_problems(tmp_path):
    result = reconcile.reconcile(make_bucket(tmp_path, KEYS).list_keys(""))
    rows = by_sample(result)
    assert {s: r["status"] for s, r in rows.items()} == {
        "S1": "complete", "S2": "not started", "S3": "incomplete", "S6": "orphan"}
    assert rows["S1"]["extra_outputs"] == {"dv": True} and rows["S1"]["problem"] is None
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
    assert not any(p.match("alignments/sub/ABC.bam") for p in patterns)
