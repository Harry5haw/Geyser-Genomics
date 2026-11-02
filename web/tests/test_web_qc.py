"""The fastp summary on the sample page (samples/qc.py)."""
import json

from .web_helpers import FASTP_PAIRED, FASTP_SINGLE, put
from samples import qc


def test_single_end_summary(storage):
    put(storage, "qc_reports/S1.fastp.json", json.dumps(FASTP_SINGLE))
    rows = dict(qc.read_qc_summary(storage, "qc_reports/S1.fastp.json"))
    assert rows == {"Reads": "10,000", "Bases": "1,500,000", "Mean read length": "150",
                    "Bases ≥ Q20": "99.1%", "Bases ≥ Q30": "93.4%", "GC content": "49.5%",
                    "Duplication (fastp estimate)": "3.6%"}


def test_paired_end_summary_has_both_mates_and_the_insert_size(storage):
    put(storage, "qc_reports/P1.fastp.json", json.dumps(FASTP_PAIRED))
    rows = dict(qc.read_qc_summary(storage, "qc_reports/P1.fastp.json"))
    assert rows["Reads"] == "10,000 (both mates)"
    assert rows["Mean read length"] == "150 / 150 (R1 / R2)"
    assert rows["Insert size (peak)"] == "218 bp"


def test_missing_malformed_or_oversized_reports_show_nothing(storage, monkeypatch):
    assert qc.read_qc_summary(storage, "qc_reports/NONE.fastp.json") is None
    put(storage, "qc_reports/BAD.fastp.json", "not json")
    assert qc.read_qc_summary(storage, "qc_reports/BAD.fastp.json") is None
    put(storage, "qc_reports/ODD.fastp.json", json.dumps({"summary": {}}))
    assert qc.read_qc_summary(storage, "qc_reports/ODD.fastp.json") is None

    class Failing:
        def open_read(self, key):
            raise RuntimeError("AccessDenied")

    assert qc.read_qc_summary(Failing(), "qc_reports/S1.fastp.json") is None  # never a 500 on the page
    monkeypatch.setattr(qc, "MAX_QC_JSON_BYTES", 10)
    put(storage, "qc_reports/BIG.fastp.json", json.dumps(FASTP_SINGLE))
    assert qc.read_qc_summary(storage, "qc_reports/BIG.fastp.json") is None
