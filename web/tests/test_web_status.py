from .web_helpers import fastq_gz, metric, put, write_metrics
from samples import status
from samples.runs import write_run


def states(sample):
    return [s.state for s in sample.stages]


def test_lists_only_real_pipeline_inputs(storage):
    for key in ["raw_reads/A.fastq.gz", "raw_reads/B.fastq.gz", "raw_reads/notes.txt",
                "raw_reads/C.D.fastq.gz", "raw_reads/bad id.fastq.gz"]:
        put(storage, key, b"x")
    assert status.list_sample_ids(storage) == ["A", "B"]


def test_fresh_upload_without_run_record_is_in_progress(storage):
    put(storage, "raw_reads/S1.fastq.gz", fastq_gz())
    sample = status.load_one(storage, "S1")
    assert sample.state == "running"
    assert sample.state_label == "In progress"  # the cloud case: no run records
    assert states(sample) == ["running", "running", "pending"]  # QC and align side by side


def test_completed_stages_come_from_keys_and_durations_from_metrics(storage):
    put(storage, "raw_reads/S1.fastq.gz", fastq_gz())
    put(storage, "qc_reports/S1_fastqc.html", b"x")
    put(storage, "alignments/S1.cram", b"x")
    write_metrics(storage, [metric("QualityControl", "S1", duration=5.5), metric("Align", "S1", duration=2.0),
                            metric("QualityControl", "OTHER", duration=99)])
    sample = status.load_one(storage, "S1")
    assert states(sample) == ["done", "done", "running"]  # variants once qc and align are done
    assert [s.duration for s in sample.stages] == [5.5, 2.0, None]
    assert sample.duration == 7.5


def test_align_can_finish_before_qc(storage):
    # qc and align run side by side in the cloud: a BAM without a QC report is progress, not a gap.
    put(storage, "raw_reads/S1.fastq.gz", fastq_gz())
    put(storage, "alignments/S1.bam", b"x")
    sample = status.load_one(storage, "S1")
    assert sample.state == "running"
    assert states(sample) == ["running", "done", "pending"]  # variants waits for both branches


def test_local_run_shows_one_stage_at_a_time(storage):
    # run_local.py (the local worker) runs qc, then align, then variants.
    put(storage, "raw_reads/S1.fastq.gz", fastq_gz())
    write_run(storage, "S1", {"state": "running", "started": 1})
    assert states(status.load_one(storage, "S1")) == ["running", "pending", "pending"]


def test_all_outputs_present_is_succeeded(storage):
    for key in ["raw_reads/S1.fastq.gz", "qc_reports/S1_fastqc.html",
                "alignments/S1.bam", "variants/S1.vcf.gz"]:
        put(storage, key, b"x")
    assert status.load_one(storage, "S1").state == "succeeded"


def test_failure_metric_marks_stage_failed(storage):
    put(storage, "raw_reads/S1.fastq.gz", fastq_gz())
    put(storage, "alignments/S1.bam", b"x")
    write_metrics(storage, [metric("Align", "S1"), metric("QualityControl", "S1", "Failure", 3.0)])
    sample = status.load_one(storage, "S1")
    assert sample.state == "failed"
    assert states(sample) == ["failed", "done", "pending"]


def test_run_record_queued_and_failed_without_metric(storage):
    put(storage, "raw_reads/S1.fastq.gz", fastq_gz())
    write_run(storage, "S1", {"state": "queued", "queued": 10})
    assert status.load_one(storage, "S1").state == "queued"
    assert states(status.load_one(storage, "S1")) == ["pending"] * 3

    # e.g. the Docker image is missing: the run fails before any stage records a metric
    write_run(storage, "S1", {"state": "failed", "queued": 10, "started": 11, "finished": 12, "exit_code": 125})
    sample = status.load_one(storage, "S1")
    assert sample.state == "failed"
    assert states(sample) == ["failed", "pending", "pending"]


def test_metrics_from_an_earlier_run_are_ignored(storage):
    put(storage, "raw_reads/S1.fastq.gz", fastq_gz())
    write_metrics(storage, [metric("QualityControl", "S1", "Failure", timestamp=1000.0)])
    write_run(storage, "S1", {"state": "running", "started": 5000.0})
    sample = status.load_one(storage, "S1")
    assert sample.state == "running"
    assert states(sample) == ["running", "pending", "pending"]  # one at a time: there is a run record


def test_corrupt_metric_lines_are_skipped(storage):
    put(storage, "raw_reads/S1.fastq.gz", fastq_gz())
    put(storage, "qc_reports/S1_fastqc.html", b"x")
    put(storage, "metrics/metrics.jsonl", 'not json\n{"task": "QualityControl"}\n'
        '{"timestamp": 1, "task": "QualityControl", "sample_id": "S1", "status": "Success", "duration_seconds": 4}\n')
    assert status.load_one(storage, "S1").stages[0].duration == 4


def test_load_one_rejects_unknown_and_invalid_ids(storage):
    assert status.load_one(storage, "NOPE") is None
    assert status.load_one(storage, "../etc") is None


def test_load_all_orders_most_recent_first(storage):
    for sid in ("OLD", "NEW"):
        put(storage, f"raw_reads/{sid}.fastq.gz", fastq_gz())
    write_run(storage, "OLD", {"state": "succeeded", "finished": 100})
    write_run(storage, "NEW", {"state": "queued", "queued": 200})
    assert [s.sample_id for s in status.load_all(storage)] == ["NEW", "OLD"]


def test_a_bam_from_before_cram_still_counts_as_aligned(storage):
    put(storage, "raw_reads/S1.fastq.gz", fastq_gz())
    put(storage, "qc_reports/S1_fastqc.html", b"x")
    put(storage, "alignments/S1.bam", b"x")
    assert states(status.load_one(storage, "S1"))[1] == "done"
    assert states(status.load_all(storage)[0])[1] == "done"  # the listing scans the same prefixes


def test_a_cram_index_alone_is_not_an_alignment(storage):
    put(storage, "raw_reads/S1.fastq.gz", fastq_gz())
    put(storage, "alignments/S1.cram.crai", b"x")
    assert states(status.load_one(storage, "S1"))[1] == "running"


def test_run_records_ignore_the_stages_provenance_files(storage):
    # runs/<id>.provenance.json (app/provenance.py) sits next to the worker's runs/<id>.json.
    from samples.runs import read_all_runs

    write_run(storage, "S1", {"state": "succeeded"})
    put(storage, "runs/S1.provenance.json", b'{"status": "succeeded"}')
    put(storage, "runs/S1.provenance/Align.json", b'{"status": "Success"}')
    assert read_all_runs(storage) == {"S1": {"state": "succeeded"}}
