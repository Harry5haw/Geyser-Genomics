import json

from .web_helpers import FASTP_SINGLE, fastq_gz, metric, put, vcf_gz, write_metrics
from samples.runs import write_run

FINISHED = ["qc_reports/S1.fastp.html", "alignments/S1.bam"]


def finished_sample(storage, truth=True):
    put(storage, "raw_reads/S1.fastq.gz", fastq_gz())
    for key in FINISHED:
        put(storage, key, b"<html><body>fastp report</body></html>" if key.endswith(".html") else b"data")
    put(storage, "qc_reports/S1.fastp.json", json.dumps(FASTP_SINGLE))
    put(storage, "variants/S1.vcf.gz", vcf_gz([(100, "A", "G"), (300, "C", "T"), (500, "G", "A")]))
    write_metrics(storage, [metric("QualityControl", "S1", duration=7.2),
                            metric("Align", "S1", duration=2.5), metric("CallVariants", "S1", duration=1.1)])
    if truth:
        put(storage, "truth/S1.json", json.dumps({"snps": [{"pos": 100, "ref": "A", "alt": "G"},
                                                          {"pos": 300, "ref": "C", "alt": "T"},
                                                          {"pos": 900, "ref": "T", "alt": "C"}]}))


def test_empty_list_page(client, storage):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"No samples yet" in resp.content
    assert b"csrfmiddlewaretoken" in resp.content
    assert b"hx-trigger" not in resp.content  # nothing to poll


def test_list_shows_samples_and_polls_only_while_active(client, storage):
    finished_sample(storage)
    put(storage, "raw_reads/S2.fastq.gz", fastq_gz())
    write_run(storage, "S2", {"state": "queued", "queued": 1})
    resp = client.get("/")
    body = resp.content.decode()
    assert "/samples/S1/" in body and "/samples/S2/" in body
    assert "Complete" in body and "Queued" in body
    assert 'hx-trigger="every' in body

    write_run(storage, "S2", {"state": "failed", "queued": 1, "started": 2, "finished": 3, "exit_code": 1})
    rows = client.get("/samples/rows/").content.decode()
    assert "Failed" in rows and "hx-trigger" not in rows


def test_detail_page_shows_variants_truth_qc_and_downloads(client, storage):
    finished_sample(storage)
    body = client.get("/samples/S1/").content.decode()
    assert "2/3 planted SNPs recovered" in body
    assert "1 unexpected call" in body
    assert body.count('class="planted"') == 2
    assert "Variants" in body and "3 calls" in body
    assert 'src="/samples/S1/files/qc_html/?inline=1"' in body
    assert "Bases ≥ Q30" in body and "93.4%" in body and "Duplication (fastp estimate)" in body
    for kind in ("raw", "qc_html", "qc_json", "bam", "vcf", "truth"):
        assert f"/samples/S1/files/{kind}/" in body
    assert "/samples/S1/files/log/" not in body  # no run log for this sample
    assert "10.8 s" in body  # total of the stage durations


def test_detail_page_for_running_sample_polls_and_shows_live_log(client, storage):
    put(storage, "raw_reads/S1.fastq.gz", fastq_gz())
    write_run(storage, "S1", {"state": "running", "started": 1})
    put(storage, "runs/S1.log", ">>> python tasks.py qc S1\n")
    body = client.get("/samples/S1/").content.decode()
    assert "?was_active=1" in body
    assert "python tasks.py qc S1" in body
    assert "Variants" not in body


def test_status_partial_asks_for_reload_when_run_finishes(client, storage):
    finished_sample(storage)
    resp = client.get("/samples/S1/status/?was_active=1")
    assert resp.status_code == 204 and resp["HX-Refresh"] == "true"
    resp = client.get("/samples/S1/status/")
    assert resp.status_code == 200 and b"run-status" in resp.content


def test_unknown_or_invalid_sample_is_404(client, storage):
    assert client.get("/samples/NOPE/").status_code == 404
    assert client.get("/samples/a.b/").status_code == 404
    assert client.get("/samples/NOPE/status/").status_code == 404


def test_downloads_are_whitelisted(client, storage):
    finished_sample(storage)
    resp = client.get("/samples/S1/files/bam/")
    assert resp.status_code == 200
    assert resp["Content-Disposition"].startswith('attachment; filename="S1.bam"')
    assert b"".join(resp.streaming_content) == b"data"
    assert client.get("/samples/S1/files/metrics/").status_code == 404
    assert client.get("/samples/S1/files/..%2F..%2Fetc/").status_code == 404
    assert client.get("/samples/..%2Fraw_reads/files/raw/").status_code == 404
    assert client.get("/samples/S1/files/log/").status_code == 404  # allowed kind, but doesn't exist


def test_qc_report_is_sandboxed_and_frameable_only_by_us(client, storage):
    finished_sample(storage)
    resp = client.get("/samples/S1/files/qc_html/?inline=1")
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/html")
    assert resp["Content-Security-Policy"] == "sandbox"
    assert resp["X-Frame-Options"] == "SAMEORIGIN"
    assert client.get("/")["X-Frame-Options"] == "DENY"


def test_list_handles_failure_metric(client, storage):
    put(storage, "raw_reads/S1.fastq.gz", fastq_gz())
    write_metrics(storage, [metric("QualityControl", "S1", "Failure")])
    body = client.get("/").content.decode()
    assert "Failed" in body
