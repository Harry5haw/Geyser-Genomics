"""scripts/reconcile.py on a temporary local bucket and a fake Step Functions client (no AWS)."""

import json
from collections import Counter
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
    return [
        f"raw_reads/{sample_id}.fastq.gz",
        f"qc_reports/{sample_id}.fastp.html",
        f"qc_reports/{sample_id}.fastp.json",
        f"alignments/{sample_id}.cram",
        f"alignments/{sample_id}.cram.crai",
        f"variants/{sample_id}.vcf.gz",
        f"variants/{sample_id}.vcf.gz.tbi",
    ]


KEYS = (
    full_sample("S1")
    + ["variants_deepvariant/S1.vcf.gz"]
    + ["raw_reads/S2.fastq.gz"]  # not started
    + ["raw_reads/S3.fastq.gz", "qc_reports/S3.fastp.html"]  # align failed
    + ["raw_reads/S10.fastq.gz", "alignments/S10.bam"]  # qc failed, align ran (BAM: before CRAM)
    + ["decompressed/OLD.fastq"]  # left over from the four-stage layout: ignored
    + ["raw_reads/S4.fq.gz", "raw_reads/notes.txt", "raw_reads/sub/S5.fastq.gz", "raw_reads/S7.v2.fastq.gz"]
    + ["variants/S6.vcf.gz", "variants_pangenome/S9.vcf.gz"]  # orphans
    + ["metrics/metrics.jsonl"]
)


def by_sample(result):
    return {r["sample_id"]: r for r in result["samples"]}


def test_statuses_and_upload_problems(tmp_path):
    result = reconcile.reconcile(make_bucket(tmp_path, KEYS).list_keys(""))
    rows = by_sample(result)
    assert {s: r["status"] for s, r in rows.items()} == {
        "S1": "complete",
        "S2": "not started",
        "S3": "incomplete",
        "S6": "orphan",
        "S9": "orphan",
        "S10": "incomplete",
    }
    assert rows["S1"]["extra_outputs"] == {"csq": False, "dv": True, "pg": False} and rows["S1"]["problem"] is None
    assert rows["S9"]["extra_outputs"] == {"csq": False, "dv": False, "pg": True}
    # qc and align run side by side, so either can be the one missing.
    assert rows["S3"]["note"] == "no VCF; output from qc; none from align, variants"
    assert rows["S10"]["note"] == "no VCF; output from align; none from qc, variants"
    assert rows["S6"]["problem"] is None  # worth knowing, not a failure

    problems = {p["key"]: p for p in result["upload_problems"]}
    assert problems["raw_reads/notes.txt"]["problem"] == "ignored"
    for key, sample in (
        ("raw_reads/S4.fq.gz", "S4"),
        ("raw_reads/sub/S5.fastq.gz", "S5"),
        ("raw_reads/S7.v2.fastq.gz", "S7"),
    ):
        assert problems[key]["problem"] == "will fail"
        assert f"the stages read raw_reads/{sample}.fastq.gz" in problems[key]["detail"]
    assert result["problem_count"] == 4 + 3  # 4 upload problems + S2, S3 and S10


def fake_sfn(running_inputs):
    client = mock.Mock()
    client.list_executions.side_effect = [
        {"executions": [{"executionArn": f"e{i}"} for i in range(2)], "nextToken": "t"},
        {"executions": [{"executionArn": f"e{i}"} for i in range(2, len(running_inputs))]},
    ]
    client.describe_execution.side_effect = lambda executionArn: {
        "input": json.dumps({"srr_id": running_inputs[int(executionArn[1:])]})
    }
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
    assert "S1" in out and "complete" in out and "Upload problems:" in out and "7 problem(s)" in out
    assert " S1                      complete     Q A V dv .." in out
    assert "!S3                      incomplete   Q . . .. .." in out
    assert "!S10                     incomplete   . A . .. .." in out

    assert reconcile.main(["--local-dir", str(tmp_path), "--json", "--fail-on-problems"]) == 1
    assert json.loads(capsys.readouterr().out)["problem_count"] == 7


def test_clean_bucket_passes(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("SFN_ARN", raising=False)
    make_bucket(tmp_path, full_sample("S1"))
    assert reconcile.main(["--local-dir", str(tmp_path), "--fail-on-problems"]) == 0
    assert "1 complete; 0 problem(s)" in capsys.readouterr().out


def test_cli_uses_state_machine_when_given(tmp_path, capsys):
    sfn = fake_sfn(["S2", "S2"])
    reconcile.main(
        ["--local-dir", str(make_bucket(tmp_path, KEYS).root), "--state-machine-arn", SFN_ARN], sfn_client=sfn
    )
    assert "2 runs at once" in capsys.readouterr().out


def test_patterns_follow_keys_for():
    patterns = reconcile.key_patterns(reconcile.extra_outputs())
    assert any(p.match("alignments/ABC.bam") for p in patterns)
    assert not any(p.match("raw_reads/ABC.fastq.gz") for p in patterns)
    assert not any(p.match("raw_reads/ABC_R1.fastq.gz") for p in patterns)
    assert not any(p.match("alignments/sub/ABC.bam") for p in patterns)
    assert reconcile.match_sample("decompressed/ABC_R2.fastq", patterns) is None  # no longer a stage output
    assert reconcile.match_sample("qc_reports/ABC.fastp.json", patterns) == "ABC"
    assert reconcile.match_sample("qc_reports/ABC_fastqc.html", patterns) == "ABC"  # FastQC's, before #100
    assert reconcile.match_sample("qc_reports/ABC_R2_fastqc.html", patterns) == "ABC"
    assert reconcile.match_sample("qc_reports/ABC_fastqc.zip", patterns) is None  # the zip was never an output


def test_samples_with_fastqc_reports_from_before_fastp_still_count_as_qc_done():
    old_single = [
        "raw_reads/OLD.fastq.gz",
        "qc_reports/OLD_fastqc.html",
        "qc_reports/OLD_fastqc.zip",
        "alignments/OLD.bam",
        "variants/OLD.vcf.gz",
    ]
    old_paired = (
        ["raw_reads/OLDP_R1.fastq.gz", "raw_reads/OLDP_R2.fastq.gz"]
        + [f"qc_reports/OLDP_{m}_fastqc.html" for m in ("R1", "R2")]
        + ["alignments/OLDP.bam", "variants/OLDP.vcf.gz"]
    )
    half = ["raw_reads/HALF_R1.fastq.gz", "raw_reads/HALF_R2.fastq.gz", "qc_reports/HALF_R1_fastqc.html"]
    rows = by_sample(reconcile.reconcile(old_single + old_paired + half))
    assert rows["OLD"]["status"] == "complete" and rows["OLD"]["outputs"]["qc_html"]
    assert rows["OLDP"]["status"] == "complete" and rows["OLDP"]["outputs"]["qc_html"]
    assert rows["HALF"]["outputs"]["qc_html"] is False  # one mate's FastQC report was never a finished QC


def paired_sample(sample_id):
    return (
        [f"raw_reads/{sample_id}_{m}.fastq.gz" for m in ("R1", "R2")]
        + [f"qc_reports/{sample_id}.fastp.{ext}" for ext in ("html", "json")]  # one report for the pair
        + [f"alignments/{sample_id}.cram", f"alignments/{sample_id}.cram.crai", f"variants/{sample_id}.vcf.gz"]
    )


PAIRED_KEYS = (
    paired_sample("P1")
    + ["raw_reads/P2_R1.fastq.gz"]  # lone mate
    + ["raw_reads/P3_R1.fastq.gz", "raw_reads/P3_R2.fastq.gz", "qc_reports/P3.fastp.html"]  # align failed
    + ["raw_reads/P4.fastq.gz", "raw_reads/P4_R1.fastq.gz", "raw_reads/P4_R2.fastq.gz"]
    + ["raw_reads/P5_R1.fq.gz", "raw_reads/sub/P6_R1.fastq.gz"]
    + ["qc_reports/P7.fastp.html", "alignments/P7.bam"]  # orphan
    + full_sample("run_1")
)  # SRA-style _1 is not a mate: run_1 is a single-end sample


def test_paired_end_samples(tmp_path):
    result = reconcile.reconcile(make_bucket(tmp_path, PAIRED_KEYS).list_keys(""))
    rows = by_sample(result)
    assert {s: r["status"] for s, r in rows.items()} == {
        "P1": "complete",
        "P2": "lone mate",
        "P3": "incomplete",
        "P4": "both layouts",
        "P7": "orphan",
        "run_1": "complete",
    }
    assert rows["P1"]["layout"] == "paired" and rows["P1"]["problem"] is None
    assert all(rows["P1"]["outputs"].values())
    assert rows["run_1"]["layout"] == "single"
    assert rows["P2"]["problem"] == "lone mate" and "R2 missing" in rows["P2"]["note"]
    assert rows["P3"]["outputs"] == {"qc_html": True, "cram": False, "vcf": False}
    assert "output from qc; none from align, variants" in rows["P3"]["note"]
    assert rows["P4"]["problem"] == "both layouts" and rows["P4"]["layout"] is None
    assert rows["P7"]["raw"] is False and rows["P7"]["outputs"]["qc_html"] is True and rows["P7"]["outputs"]["cram"]
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
    assert " P1                      complete     Q A V .. .. .  paired-end" in out  # last '.': no run record
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


def test_one_mates_fastqc_report_is_incomplete_not_unstarted():
    # FastQC (before #100) wrote one report per mate: one alone is not a QC output, but it does show
    # the run reached qc.
    rows = by_sample(
        reconcile.reconcile(["raw_reads/S1_R1.fastq.gz", "raw_reads/S1_R2.fastq.gz", "qc_reports/S1_R1_fastqc.html"])
    )
    assert rows["S1"]["status"] == "incomplete" and rows["S1"]["outputs"]["qc_html"] is False
    assert "output from qc" in rows["S1"]["note"]


def test_indexes_belong_to_their_sample_but_are_not_outputs():
    patterns = reconcile.key_patterns(reconcile.extra_outputs())
    assert reconcile.match_sample("alignments/ABC.cram", patterns) == "ABC"
    assert reconcile.match_sample("alignments/ABC.cram.crai", patterns) == "ABC"
    assert reconcile.match_sample("variants/ABC.vcf.gz.tbi", patterns) == "ABC"
    expected = reconcile.keys_for("ABC")
    assert reconcile.output_keys(expected, "cram") == [["alignments/ABC.cram"], ["alignments/ABC.bam"]]
    assert reconcile.output_keys(expected, "vcf") == [["variants/ABC.vcf.gz"]]


def test_stats_files_belong_to_their_sample():
    patterns = reconcile.key_patterns(reconcile.extra_outputs())
    for name in ("flagstat.txt", "mosdepth.summary.txt", "mosdepth.global.dist.txt", "bcftools.stats.txt"):
        assert reconcile.match_sample(f"stats/ABC/{name}", patterns) == "ABC"
    assert reconcile.match_sample("stats/ABC/other.txt", patterns) is None


def test_run_records_show_in_the_report(tmp_path, capsys, monkeypatch):
    import provenance

    monkeypatch.delenv("SFN_ARN", raising=False)
    store = make_bucket(tmp_path, full_sample("S1") + paired_sample("P1"))
    provenance._put_json(
        store,
        "runs/S1.provenance.json",
        {"status": "succeeded", "complete": True, "run_name": "S1-run", "stages": {"Align": {"status": "Success"}}},
    )
    provenance._put_json(
        store,
        "runs/P1.provenance.json",
        {"status": "failed", "complete": False, "run_name": "P1-run", "stages": {"Align": {"status": "Failure"}}},
    )

    assert reconcile.main(["--local-dir", str(tmp_path), "--json"]) == 0
    rows = {r["sample_id"]: r for r in json.loads(capsys.readouterr().out)["samples"]}
    assert rows["S1"]["run_record"]["status"] == "succeeded"
    assert "run record: Align in P1-run failed" in rows["P1"]["note"]

    reconcile.main(["--local-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert " S1 " in out and "Q A V .. .. R" in out and "Q A V .. .. r" in out


def put_parts(store, sample_id, statuses, run_name="run"):
    import provenance

    for task, status in statuses:
        provenance._put_json(
            store,
            f"runs/{sample_id}.provenance/{task}.json",
            {
                "task": task,
                "status": status,
                "run_name": run_name,
                "finished": "2026-11-02T10:00:00Z",
                "inputs": [],
                "outputs": [],
                "tools": {},
                "stats": {},
            },
        )


def test_a_failed_core_stage_is_a_problem_even_with_all_outputs(tmp_path, capsys, monkeypatch):
    # An earlier run's outputs are all there, but the latest run's variant calling failed.
    monkeypatch.delenv("SFN_ARN", raising=False)
    store = make_bucket(tmp_path, full_sample("S1"))
    put_parts(store, "S1", [("QualityControl", "Success"), ("Align", "Success"), ("CallVariants", "Failure")])

    assert reconcile.main(["--local-dir", str(tmp_path), "--fail-on-problems"]) == 1
    out = capsys.readouterr().out
    assert "!S1 " in out and "Q A V .. .. r" in out and "CallVariants in run failed" in out


def test_a_failed_extra_caller_is_noted_not_a_problem(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("SFN_ARN", raising=False)
    store = make_bucket(tmp_path, full_sample("S1"))
    put_parts(
        store,
        "S1",
        [("QualityControl", "Success"), ("Align", "Success"), ("CallVariants", "Success"), ("DeepVariant", "Failure")],
    )

    assert reconcile.main(["--local-dir", str(tmp_path), "--fail-on-problems"]) == 0
    out = capsys.readouterr().out
    assert "Q A V .. .. R" in out and "DeepVariant in run failed" in out


def test_a_failed_run_with_a_retry_open_is_not_a_problem(tmp_path):
    from collections import Counter

    store = make_bucket(tmp_path, full_sample("S1"))
    put_parts(store, "S1", [("QualityControl", "Success"), ("Align", "Failure")])
    keys = set(store.list_keys(""))
    (row,) = reconcile.reconcile(keys, Counter({"S1": 1}), reconcile.run_records(store, keys))["samples"]
    assert row["status"] == "running" and row["problem"] is None and "Align in run failed" in row["note"]


def test_an_unreadable_run_record_is_skipped(tmp_path, monkeypatch, capsys):
    import provenance

    store = make_bucket(tmp_path, full_sample("S1") + ["runs/S1.provenance.json"])

    def denied(storage, sample_id):
        raise PermissionError("AccessDenied")

    monkeypatch.setattr(provenance, "read_record", denied)
    assert reconcile.run_records(store, set(store.list_keys(""))) == {}
    assert "could not read the run record of S1" in capsys.readouterr().err


def test_annotated_vcf_shows_as_its_own_column_only_when_present(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("SFN_ARN", raising=False)
    make_bucket(tmp_path, full_sample("S1"))
    reconcile.main(["--local-dir", str(tmp_path)])
    assert "csq" not in capsys.readouterr().out  # no sample annotated: no column

    make_bucket(tmp_path, ["variants/S1.csq.vcf.gz"])
    rows = by_sample(reconcile.reconcile(make_bucket(tmp_path, []).list_keys("")))
    assert rows["S1"]["extra_outputs"]["csq"] is True and rows["S1"]["status"] == "complete"
    reconcile.main(["--local-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert "csq annotated VCF" in out


def test_annotated_vcf_is_not_mistaken_for_another_samples_vcf(tmp_path):
    keys = full_sample("S1") + ["variants/S1.csq.vcf.gz", "variants/S1.csq.vcf.gz.tbi"]
    rows = by_sample(reconcile.reconcile(make_bucket(tmp_path, keys).list_keys("")))
    assert set(rows) == {"S1"}  # no phantom orphan "S1.csq"


def test_a_run_closed_as_failed_at_an_unknown_step_is_still_a_problem(tmp_path, capsys, monkeypatch):
    # The state machine closed the run as failed (Read_Regions' S3 error): no stage failed, the run did.
    import provenance

    monkeypatch.delenv("SFN_ARN", raising=False)
    store = make_bucket(tmp_path, full_sample("S1"))
    put_parts(store, "S1", [("QualityControl", "Success"), ("Align", "Success")])
    provenance._put_json(
        store,
        "runs/S1.provenance/Finalize.json",
        {"task": "Finalize", "run_name": "run", "finished": "2099-01-01T00:00:00Z", "status": "failed"}
        | {"caught": {"Error": "S3.NoSuchKeyException", "Cause": "The specified key does not exist."}},
    )
    assert reconcile.main(["--local-dir", str(tmp_path), "--json", "--fail-on-problems"]) == 1
    row = {r["sample_id"]: r for r in json.loads(capsys.readouterr().out)["samples"]}["S1"]
    assert "run record: run failed (step unknown)" in row["note"] and row["problem"] == "run failed"


def test_a_close_naming_a_stage_that_wrote_success_says_which(tmp_path, capsys, monkeypatch):
    # Align's job wrote Success, then its task failed (e.g. polling the job): the close names Align.
    import provenance

    monkeypatch.delenv("SFN_ARN", raising=False)
    store = make_bucket(tmp_path, full_sample("S1"))
    put_parts(store, "S1", [("QualityControl", "Success"), ("Align", "Success")])
    provenance._put_json(
        store,
        "runs/S1.provenance/Finalize.json",
        {"task": "Finalize", "run_name": "run", "finished": "2099-01-01T00:00:00Z", "status": "failed"}
        | {"caught": {"Error": "States.Timeout", "Cause": json.dumps({"JobName": "AlignGenome-S1-run"})}},
    )
    reconcile.main(["--local-dir", str(tmp_path), "--json"])
    row = {r["sample_id"]: r for r in json.loads(capsys.readouterr().out)["samples"]}["S1"]
    assert "run record: run failed (Align)" in row["note"]


def put_caller_parts(store, sample_id, stages, caller=None):
    """Parts of one run; the stage named by CALLER_STAGES[caller] carries the caller (PIPELINE_CALLER)."""
    import provenance

    for i, (task, status) in enumerate(stages):
        at = f"2026-11-02T10:0{i}:00Z"
        part = {
            "task": task,
            "status": status,
            "run_name": "run",
            "started": at,
            "finished": at,
            "inputs": [],
            "outputs": [],
            "tools": {},
            "stats": {},
        }
        if caller and task == provenance.CALLER_STAGES[caller]:
            part["caller"] = caller
        provenance._put_json(store, f"runs/{sample_id}.provenance/{task}.json", part)


def report(store):
    keys = list(store.list_keys(""))  # a generator: run_records would use it up
    return by_sample(reconcile.reconcile(keys, records=reconcile.run_records(store, keys)))


DV_SAMPLE = [
    "raw_reads/S1.fastq.gz",
    "qc_reports/S1.fastp.html",
    "qc_reports/S1.fastp.json",
    "alignments/S1.cram",
    "variants_deepvariant/S1.vcf.gz",
]


def test_a_deepvariant_runs_sample_is_complete_with_only_its_vcf(tmp_path):
    store = make_bucket(tmp_path, DV_SAMPLE)
    put_caller_parts(
        store, "S1", [("QualityControl", "Success"), ("Align", "Success"), ("DeepVariant", "Success")], "deepvariant"
    )
    row = report(store)["S1"]
    assert (row["status"], row["problem"]) == ("complete", None)
    assert row["outputs"]["vcf"] and row["extra_outputs"]["dv"]


def test_a_deepvariant_vcf_without_a_recorded_caller_counts_when_bcftools_never_ran(tmp_path):
    # e.g. a --caller deepvariant run from before PIPELINE_CALLER was recorded: its VCF holds the only calls.
    store = make_bucket(tmp_path, DV_SAMPLE)
    put_caller_parts(store, "S1", [("QualityControl", "Success"), ("Align", "Success"), ("DeepVariant", "Success")])
    assert report(store)["S1"]["status"] == "complete"


def test_a_deepvariant_vcf_does_not_complete_a_sample_whose_bcftools_call_failed(tmp_path):
    store = make_bucket(tmp_path, DV_SAMPLE)
    put_caller_parts(
        store,
        "S1",
        [("QualityControl", "Success"), ("Align", "Success"), ("CallVariants", "Failure"), ("DeepVariant", "Success")],
    )
    row = report(store)["S1"]
    assert row["status"] == "incomplete" and not row["outputs"]["vcf"] and row["extra_outputs"]["dv"]


# ---- --publish-metrics (O13) ----


def test_problem_counts_by_kind_include_zeros_and_the_total(tmp_path):
    keys = KEYS + ["raw_reads/P1_R1.fastq.gz"]  # a lone mate
    result = reconcile.reconcile(make_bucket(tmp_path, keys).list_keys(""), Counter({"S2": 2}))
    counts = reconcile.problem_counts(result)
    assert counts == {
        "Ignored": 1,  # notes.txt: not .gz, starts nothing
        "Refused": 0,
        "WillFail": 3,
        "LoneMate": 1,
        "BothLayouts": 0,
        "NotStarted": 0,
        "Incomplete": 2,  # S3, S10
        "RunFailed": 0,
        "ConcurrentRuns": 1,  # S2 twice: the trigger's dedupe missed
        "Total": result["problem_count"],
    }
    assert counts["Total"] == sum(v for k, v in counts.items() if k != "Total")


def test_an_unknown_problem_kind_is_still_counted():
    result = {"upload_problems": [], "samples": [{"problem": "brand new"}], "problem_count": 1}
    assert reconcile.problem_counts(result)["BrandNew"] == 1


def test_publish_metrics_writes_the_counts_beside_the_local_bucket(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("SFN_ARN", raising=False)
    for name in ("METRICS_BACKEND", "METRICS_FILE", "STORAGE_BACKEND", "LOCAL_STORAGE_DIR"):
        monkeypatch.delenv(name, raising=False)
    make_bucket(tmp_path, full_sample("S1") + ["raw_reads/P1_R2.fastq.gz"])
    assert reconcile.main(["--local-dir", str(tmp_path), "--publish-metrics", "--fail-on-problems"]) == 1
    lines = [json.loads(x) for x in (tmp_path / "metrics" / "metrics.jsonl").read_text().splitlines()]
    published = {r["dimensions"]["Problem"]: r["value"] for r in lines if r.get("metric") == "ReconcileProblems"}
    assert published["LoneMate"] == 1 and published["Total"] == 1 and published["Ignored"] == 0
    assert "Published ReconcileProblems: LoneMate 1, Total 1" in capsys.readouterr().err
    # The metrics file doesn't change the reconcile itself.
    assert reconcile.main(["--local-dir", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["problem_count"] == 1


def test_without_the_option_nothing_is_published(tmp_path, monkeypatch):
    monkeypatch.delenv("SFN_ARN", raising=False)
    make_bucket(tmp_path, full_sample("S1"))
    backend = mock.Mock()
    reconcile.main(["--local-dir", str(tmp_path)], metrics=backend)
    backend.record_counts.assert_not_called()


def test_a_metrics_failure_is_a_warning_not_a_failed_reconcile(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("SFN_ARN", raising=False)
    make_bucket(tmp_path, full_sample("S1"))
    backend = mock.Mock()
    backend.record_counts.side_effect = RuntimeError("CloudWatch down")
    assert (
        reconcile.main(["--local-dir", str(tmp_path), "--publish-metrics", "--fail-on-problems"], metrics=backend) == 0
    )
    assert "WARNING: couldn't publish ReconcileProblems: RuntimeError: CloudWatch down" in capsys.readouterr().err
    name, dimension, counts = backend.record_counts.call_args.args
    assert (name, dimension, counts["Total"]) == ("ReconcileProblems", "Problem", 0)


def test_the_backend_follows_where_the_data_lake_is(tmp_path):
    from metrics import CloudWatchMetrics, LocalMetrics, NullMetrics

    local = reconcile.metrics_backend(reconcile.parse_args(["--local-dir", str(tmp_path)]), env={})
    assert isinstance(local, LocalMetrics) and local.path.startswith(str(tmp_path))
    with mock.patch("boto3.client") as client:
        cloud = reconcile.metrics_backend(reconcile.parse_args(["--bucket", "b", "--region", "eu-west-1"]), env={})
    assert isinstance(cloud, CloudWatchMetrics) and client.call_args.kwargs["region_name"] == "eu-west-1"
    off = reconcile.metrics_backend(reconcile.parse_args(["--bucket", "b"]), env={"METRICS_BACKEND": "none"})
    assert isinstance(off, NullMetrics)  # METRICS_BACKEND always wins
