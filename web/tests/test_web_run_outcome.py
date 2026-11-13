"""A cloud run's page takes its outcome from the run record's close, not from which outputs exist.

Modelled on CLOUD021 (9 Nov 2026), the first published run: every stage succeeded and wrote its output, then the
state machine (gate_mode = fail) failed the run with QualityGateFailed, while the gate job itself ran pinned to
GATE_MODE=warn, so its verdict said "mode": "warn". The page said "Complete" and "runs in warn mode, so the run
still counts as succeeded", showed no stage times, and its footer named BWA-MEM.
"""

import json

from samples import gate as gate_mod
from samples import publish as publish_mod
from samples import status

from .web_helpers import FASTP_SINGLE, fastq_gz, put, vcf_gz

GATE_CAUSE = 'CLOUD021 failed the quality gate: ["mean_depth 0.0222 is below the minimum 20"]'
VERDICT = {
    "verdict": "fail",
    "mode": "warn",  # the job's own mode: pinned to warn by the state machine
    "reasons": ["mean_depth 0.0222 is below the minimum 20"],
    "checks": [{"name": "mean_depth", "status": "fail", "value": 0.0222, "min": 20.0, "max": None}],
}


def part(task, seconds):
    return {
        "task": task,
        "status": "Success",
        "run_name": "C21-run",
        "started": "2026-11-09T16:54:00Z",
        "finished": "2026-11-09T16:54:05Z",
        "duration_seconds": seconds,
        "inputs": [],
        "outputs": [],
        "tools": {},
        "stats": {},
    }


def gate_failed_run(storage, closed=True):
    """Every stage's output and part; the gate's verdict fail (mode warn); with closed, the Finalize part."""
    put(storage, "raw_reads/C21.fastq.gz", fastq_gz())
    for key in ("qc_reports/C21.fastp.html", "alignments/C21.cram", "alignments/C21.cram.crai"):
        put(storage, key, b"data")
    put(storage, "qc_reports/C21.fastp.json", json.dumps(FASTP_SINGLE))
    put(storage, "variants/C21.vcf.gz", vcf_gz([]))
    put(storage, "qc_reports/C21.gate.json", json.dumps(VERDICT))
    for task, seconds in (("QualityControl", 1.6), ("Align", 5.0), ("CallVariants", 2.3)):
        put(storage, f"runs/C21.provenance/{task}.json", json.dumps(part(task, seconds)))
    gate = part("QualityGate", 0.8) | {"gate": {"mode": "warn", "verdict": "fail", "reasons": VERDICT["reasons"]}}
    put(storage, "runs/C21.provenance/QualityGate.json", json.dumps(gate))
    if closed:
        final = {
            "task": "Finalize",
            "status": "failed",
            "run_name": "C21-run",
            "finished": "2026-11-09T16:58:08.572Z",
            "caught": {"Error": "QualityGateFailed", "Cause": GATE_CAUSE},
        }
        put(storage, "runs/C21.provenance/Finalize.json", json.dumps(final))


def test_a_run_the_gate_failed_is_failed_although_every_output_exists(storage):
    gate_failed_run(storage)
    sample = status.load_one(storage, "C21")
    assert sample.state == "failed" and sample.state_label == "Failed"
    assert [s.state for s in sample.stages] == ["done", "done", "done"]
    # The list reads the record for a sample with a Finalize part, so its row agrees with the page.
    assert {s.sample_id: s.state for s in status.load_all(storage)}["C21"] == "failed"


def test_cloud_stage_times_come_from_the_run_record_without_a_sum(storage):
    gate_failed_run(storage)
    sample = status.load_one(storage, "C21")
    assert [s.duration for s in sample.stages] == [1.6, 5.0, 2.3]
    assert sample.duration is None  # QC and align ran side by side: a sum would overstate the run


def test_the_same_run_before_its_close_is_still_complete(storage):
    gate_failed_run(storage, closed=False)  # warn mode, or the close not written yet
    assert status.load_one(storage, "C21").state == "succeeded"


def test_the_gate_shows_the_pipelines_enforced_mode(storage):
    gate_failed_run(storage)
    record = status._load_record(storage, "C21")
    gate = gate_mod.read_gate(storage, "qc_reports/C21.gate.json", record)
    assert (gate.state, gate.mode, gate.enforced, gate.label) == ("fail", "fail", True, "Failed")


def test_a_failed_verdict_in_a_run_that_succeeded_is_a_warning(storage):
    gate_failed_run(storage, closed=False)
    record = status._load_record(storage, "C21")
    gate = gate_mod.read_gate(storage, "qc_reports/C21.gate.json", record)
    assert (gate.state, gate.mode, gate.enforced) == ("warn", "warn", False)


def test_a_run_failed_by_another_step_keeps_a_warn_mode_verdict_a_warning(storage):
    gate_failed_run(storage)
    record = status._load_record(storage, "C21") | {"failure": {"error": "States.TaskFailed"}}
    assert gate_mod.read_gate(storage, "qc_reports/C21.gate.json", record).state == "warn"


def test_the_page_tells_one_story(client, storage):
    gate_failed_run(storage)
    body = client.get("/samples/C21/").content.decode()
    assert "Complete" not in body and "counts as succeeded" not in body
    assert "The gate failed this sample, so the run failed." in body and "set by the pipeline" in body
    assert "1.6 s" in body and "5.0 s" in body and "2.3 s" in body  # the stage cards' times


def test_the_footer_names_bwa_mem2_and_the_hosted_site_says_how_it_runs(client, storage, tmp_path):
    gate_failed_run(storage)
    local = client.get("/samples/C21/").content.decode()
    assert "BWA-MEM" not in local and "bwa-mem2" in local and "Storage:" in local
    out = tmp_path / "site"
    publish_mod.publish(sample_ids=["C21"], out=str(out), log=lambda m: None)
    hosted = (out / "samples" / "C21" / "index.html").read_text(encoding="utf-8")
    assert "BWA-MEM" not in hosted and "Storage:" not in hosted
    assert "Failed" in hosted
    # How it runs is on the How it works page, which every hosted page's header and footer link to.
    footer = hosted.split("<footer", 1)[1]
    assert 'href="/about/">How it works' in footer
    about = (out / "about" / "index.html").read_text(encoding="utf-8")
    assert "bwa-mem2" in about and "published here when the run ends" in about
