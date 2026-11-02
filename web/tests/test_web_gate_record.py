"""The quality-gate verdict, the run record and the new downloads on the sample page (#148, #67, G5)."""

import json

from samples.gate import read_gate

from .web_helpers import fastq_gz, put, vcf_gz

VERDICT_KEY = "qc_reports/S1.gate.json"


def failed_verdict(**extra):
    return {
        "schema": 1,
        "gate_version": "0.1.0",
        "sample_id": "S1",
        "run_name": "S1-20261102T100000Z-local",
        "verdict": "fail",
        "reasons": ["mean_depth 3.1 below 25"],
        "config": "built-in default",
        "checks": [
            {"name": "mapped_pct", "status": "pass", "value": 99.9, "min": 90.0, "max": None},
            {"name": "mean_depth", "status": "fail", "value": 3.1, "min": 25.0, "max": None, "reason": "below 25"},
        ],
        **extra,
    }


def sample_with(storage, verdict=None, record=None):
    put(storage, "raw_reads/S1.fastq.gz", fastq_gz())
    put(storage, "qc_reports/S1.fastp.html", b"<html></html>")
    put(storage, "alignments/S1.cram", b"x")
    put(storage, "variants/S1.vcf.gz", vcf_gz([(100, "A", "G")]))
    if verdict is not None:
        put(storage, VERDICT_KEY, json.dumps(verdict) if isinstance(verdict, dict) else verdict)
    if record is not None:
        put(storage, "runs/S1.provenance.json", json.dumps(record))


# ------------------------------------------------------------------ read_gate


def test_no_verdict_file_means_no_gate_panel(storage):
    assert read_gate(storage, VERDICT_KEY) is None


def test_pass(storage):
    put(storage, VERDICT_KEY, json.dumps({"verdict": "pass", "reasons": [], "gate_version": "0.1.0"}))
    gate = read_gate(storage, VERDICT_KEY)
    assert (gate.state, gate.label, gate.css, gate.mode) == ("pass", "Passed", "succeeded", "warn")


def test_failed_verdict_in_warn_mode_is_a_warning_not_a_failure(storage):
    put(storage, VERDICT_KEY, json.dumps(failed_verdict()))
    gate = read_gate(storage, VERDICT_KEY, {"gate": {"mode": "warn"}})
    assert (gate.state, gate.label) == ("warn", "Warnings")
    assert gate.reasons == ["mean_depth 3.1 below 25"]
    assert [c["name"] for c in gate.failed_checks] == ["mean_depth"]  # only the failed checks


def test_failed_verdict_in_fail_mode_is_a_failure(storage):
    put(storage, VERDICT_KEY, json.dumps(failed_verdict()))
    gate = read_gate(storage, VERDICT_KEY, {"gate": {"mode": "fail"}})
    assert (gate.state, gate.label, gate.css) == ("fail", "Failed", "failed")


def test_mode_defaults_to_warn_without_a_record(storage):
    put(storage, VERDICT_KEY, json.dumps(failed_verdict()))
    assert read_gate(storage, VERDICT_KEY).state == "warn"


def test_null_verdict_is_not_run_with_its_error_never_a_pass(storage):
    put(
        storage, VERDICT_KEY, json.dumps({"verdict": None, "mode": "warn", "error": "geyser-gate timed out after 300s"})
    )
    gate = read_gate(storage, VERDICT_KEY)
    assert (gate.state, gate.label) == ("not_run", "Gate not run")
    assert gate.error == "geyser-gate timed out after 300s"


def test_unreadable_verdict_is_not_run(storage):
    put(storage, VERDICT_KEY, b"{not json")
    gate = read_gate(storage, VERDICT_KEY)
    assert gate.state == "not_run" and "unreadable verdict" in gate.error


# ------------------------------------------------------------------ the sample page


def test_page_shows_a_warn_mode_failure_as_warnings_and_the_sample_not_failed(client, storage):
    sample_with(storage, failed_verdict(), {"status": "succeeded", "gate": {"mode": "warn"}})
    body = client.get("/samples/S1/").content.decode()
    assert "Quality gate" in body and "Warnings" in body
    assert "mean_depth 3.1 below 25" in body and "≥ 25.0" in body
    assert "warn mode, so the run still counts as succeeded" in body
    assert 'class="badge failed"' not in body  # neither the gate nor the run is shown as failed


def test_page_shows_a_gate_that_did_not_run_with_its_error(client, storage):
    sample_with(storage, {"verdict": None, "mode": "warn", "error": "geyser-gate is not installed"})
    body = client.get("/samples/S1/").content.decode()
    assert "Gate not run" in body and "geyser-gate is not installed" in body and "This is not a pass" in body
    assert "Passed" not in body


def test_page_without_a_verdict_has_no_gate_panel(client, storage):
    sample_with(storage)
    assert "Quality gate" not in client.get("/samples/S1/").content.decode()


RECORD = {
    "schema": 1,
    "sample_id": "S1",
    "run_name": "S1-20261102T100000Z-local",
    "status": "succeeded",
    "app_version": "local:geyser-app",
    "layout": "single",
    "aligner": "bwa-mem2",
    "reference": {"name": "synthetic_ref.fa", "md5": "0123456789abcdef", "bytes": 50000},
    "tools": {"samtools": "1.21", "bcftools": "1.21", "fastp": "0.24.0"},
    "stages": {
        "Align": {
            "status": "Success",
            "started": "2026-11-02T10:00:05Z",
            "duration_seconds": 12.34,
            "resources": {"cpu_seconds": 30.5, "peak_memory_mib": 812.4},
        },
        "QualityControl": {"status": "Success", "started": "2026-11-02T10:00:01Z", "duration_seconds": 4.2},
        "CallVariants": {
            "status": "Failure",
            "started": "2026-11-02T10:00:20Z",
            "duration_seconds": 1.0,
            "error": "bcftools exited 1",
        },
    },
}


def test_page_shows_the_run_record(client, storage):
    sample_with(storage, record=RECORD)
    body = client.get("/samples/S1/").content.decode()
    assert "Run record" in body and "S1-20261102T100000Z-local" in body and "0123456789abcdef" in body
    assert "bwa-mem2" in body and "local:geyser-app" in body
    # Stages in the order they started, with time, CPU and peak memory where recorded.
    assert body.index("QualityControl") < body.index(">Align<") < body.index("CallVariants")
    assert "12.3 s" in body and "30.5 s" in body and "812 MiB" in body
    assert "bcftools exited 1" in body
    assert "Tool versions (3)" in body and "0.24.0" in body


def test_page_without_a_record_has_no_record_panel(client, storage):
    sample_with(storage)
    assert "Run record" not in client.get("/samples/S1/").content.decode()


def test_gate_verdict_and_run_record_are_downloadable(client, storage):
    sample_with(storage, failed_verdict(), RECORD)
    body = client.get("/samples/S1/").content.decode()
    assert "/samples/S1/files/gate_verdict/" in body and "/samples/S1/files/run_record/" in body
    assert json.loads(client.get("/samples/S1/files/gate_verdict/").getvalue())["verdict"] == "fail"
    assert json.loads(client.get("/samples/S1/files/run_record/").getvalue())["run_name"] == RECORD["run_name"]


def test_annotated_vcf_index_is_offered_with_the_annotated_vcf(client, storage):
    sample_with(storage)
    put(storage, "variants/S1.csq.vcf.gz", vcf_gz([(100, "A", "G")]))
    put(storage, "variants/S1.csq.vcf.gz.tbi", b"tbi")
    body = client.get("/samples/S1/").content.decode()
    assert "/samples/S1/files/csq_vcf/" in body and "/samples/S1/files/csq_tbi/" in body
    assert client.get("/samples/S1/files/csq_tbi/").getvalue() == b"tbi"
