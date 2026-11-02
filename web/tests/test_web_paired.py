"""Paired-end samples in the web UI: upload both mates, list them as one sample, show both mates' files."""
import json

from django.core.files.uploadedfile import SimpleUploadedFile

from .web_helpers import FASTP_PAIRED, fastq_gz, put, vcf_gz
from samples import status
from samples.worker import Worker

R1, R2 = "raw_reads/P1_R1.fastq.gz", "raw_reads/P1_R2.fastq.gz"


def gz_file(name, reads=2):
    return SimpleUploadedFile(name, fastq_gz(reads), content_type="application/gzip")


def upload_pair(client, r1="P1_R1.fastq.gz", r2="P1_R2.fastq.gz", sample_id=""):
    return client.post("/upload/", {"reads": gz_file(r1, 2), "reads_r2": gz_file(r2, 3), "sample_id": sample_id})


def finished_pair(storage):
    for key in (R1, R2):
        put(storage, key, fastq_gz())
    put(storage, "qc_reports/P1.fastp.html", "<html>fastp report for the pair</html>")
    put(storage, "qc_reports/P1.fastp.json", json.dumps(FASTP_PAIRED))
    put(storage, "alignments/P1.bam", b"bam")
    put(storage, "variants/P1.vcf.gz", vcf_gz([(100, "A", "G")]))


# --- upload -------------------------------------------------------------------

def test_paired_upload_stores_both_mates_under_one_id(client, storage):
    resp = upload_pair(client)
    assert resp.status_code == 302 and resp["Location"] == "/samples/P1/"
    with storage.open_read(R1) as fh:
        assert fh.read() == fastq_gz(2)
    with storage.open_read(R2) as fh:
        assert fh.read() == fastq_gz(3)  # second file is R2, whatever it was called
    assert not storage.exists("raw_reads/P1.fastq.gz")


def test_paired_upload_takes_the_id_from_r1_or_the_form(client, storage):
    assert upload_pair(client, r1="reads_1.fq.gz", r2="reads_2.fq.gz", sample_id="Pair-7").status_code == 302
    assert storage.exists("raw_reads/Pair-7_R1.fastq.gz") and storage.exists("raw_reads/Pair-7_R2.fastq.gz")


def test_paired_upload_validates_r2_like_r1(client, storage):
    bad = SimpleUploadedFile("P1_R2.fastq.gz", b"@r\nA\n+\nI\n", content_type="application/gzip")
    resp = client.post("/upload/", {"reads": gz_file("P1_R1.fastq.gz"), "reads_r2": bad})
    assert resp.status_code == 400
    assert list(storage.list_keys("")) == []


def test_upload_refuses_an_id_already_used_by_the_other_layout(client, storage):
    put(storage, "raw_reads/P1.fastq.gz", fastq_gz())
    assert upload_pair(client).status_code == 400
    assert not storage.exists(R1)

    put(storage, "raw_reads/Q1_R2.fastq.gz", fastq_gz())  # even a lone mate claims the ID
    resp = client.post("/upload/", {"reads": gz_file("Q1.fastq.gz")})
    assert resp.status_code == 400 and not storage.exists("raw_reads/Q1.fastq.gz")


def test_upload_form_offers_an_r2_field(client, storage):
    assert b'name="reads_r2"' in client.get("/").content


# --- status and worker -------------------------------------------------------------

def test_lone_mate_is_not_a_sample_until_its_partner_arrives(client, storage):
    put(storage, R1, fastq_gz())
    assert status.list_sample_ids(storage) == []
    assert client.get("/samples/P1/").status_code == 404

    put(storage, R2, fastq_gz())
    assert status.list_sample_ids(storage) == ["P1"]
    assert client.get("/samples/P1/").status_code == 200


def test_sample_uploaded_both_ways_is_not_listed(storage):
    for key in ("raw_reads/P1.fastq.gz", R1, R2):
        put(storage, key, fastq_gz())
    assert status.list_sample_ids(storage) == []


def test_paired_qc_is_one_report_for_the_pair(storage):
    for key in (R1, R2):
        put(storage, key, fastq_gz())
    (sample,) = status.load_all(storage)
    assert sample.layout == "paired"
    assert sample.stages[0].state != "done"

    put(storage, "qc_reports/P1.fastp.html", b"data")
    assert status.load_one(storage, "P1").stages[0].state == "done"


def test_finished_paired_sample_is_complete(storage):
    finished_pair(storage)
    sample = status.load_one(storage, "P1")
    assert sample.state == "succeeded"
    assert all(s.state == "done" for s in sample.stages)


def test_worker_runs_a_pair_once_when_both_mates_exist(storage):
    calls = []

    class Runner:
        def run(self, sample_id, on_output):
            calls.append(sample_id)
            return 0

    worker = Worker(storage, Runner(), log_flush_seconds=0, say=lambda msg: None)
    put(storage, R1, fastq_gz())
    assert worker.run_once() == 0
    put(storage, R2, fastq_gz())
    assert worker.run_once() == 1 and calls == ["P1"]


# --- detail page and downloads -------------------------------------------------------

def test_detail_page_shows_both_mates_reads_and_one_qc_report(client, storage):
    finished_pair(storage)
    body = client.get("/samples/P1/").content.decode()
    assert 'src="/samples/P1/files/qc_html/?inline=1"' in body and "fastp report (both mates)" in body
    assert "Insert size (peak)" in body and "218 bp" in body
    for mate in ("R1", "R2"):
        assert f"/samples/P1/files/raw_{mate}/" in body
        assert f"/samples/P1/files/qc_html_{mate}/" not in body
    for kind in ("qc_html", "qc_json", "bam", "vcf"):
        assert f"/samples/P1/files/{kind}/" in body
    assert "/samples/P1/files/raw/" not in body


def test_mate_files_are_served_and_qc_inline_is_sandboxed(client, storage):
    finished_pair(storage)
    resp = client.get("/samples/P1/files/raw_R2/")
    assert resp.status_code == 200 and b"".join(resp.streaming_content) == fastq_gz()
    assert 'filename="P1_R2.fastq.gz"' in resp["Content-Disposition"]

    qc = client.get("/samples/P1/files/qc_html/?inline=1")
    assert qc["Content-Security-Policy"] == "sandbox"
    assert b"fastp report for the pair" in b"".join(qc.streaming_content)
    assert client.get("/samples/P1/files/qc_html_R1/").status_code == 404  # no per-mate QC any more


def test_mate_kinds_are_404_for_single_end_and_vice_versa(client, storage):
    finished_pair(storage)
    put(storage, "raw_reads/S1.fastq.gz", fastq_gz())
    assert client.get("/samples/S1/files/raw_R1/").status_code == 404
    assert client.get("/samples/P1/files/raw/").status_code == 404
    assert client.get("/samples/P1/files/bam_R1/").status_code == 404  # one BAM per sample
    assert client.get("/samples/P1/files/raw_R3/").status_code == 404


def test_explicit_ids_ending_in_a_mate_suffix_are_refused(client, storage):
    # raw_reads/foo_R1.fastq.gz would be read as mate R1 of 'foo' and never run.
    for sample_id in ("foo_R1", "foo_R2"):
        resp = client.post("/upload/", {"reads": gz_file("reads.fastq.gz"), "sample_id": sample_id})
        assert resp.status_code == 400 and b"can&#x27;t end in _R1 or _R2" in resp.content
    assert list(storage.list_keys("")) == []


def test_paired_upload_replaces_a_lone_mate_left_by_a_failed_upload(client, storage):
    put(storage, R2, b"stale")  # e.g. the R1 write failed after R2 was stored
    assert upload_pair(client).status_code == 302
    with storage.open_read(R2) as fh:
        assert fh.read() == fastq_gz(3)
    assert storage.exists(R1)


def test_demo_ids_skip_ids_taken_in_either_layout(storage):
    from samples import demo

    put(storage, f"raw_reads/{demo.demo_id(1)}_R1.fastq.gz", fastq_gz())  # DEMO001 taken as a (lone) mate
    sample_id = demo.create_demo_sample(storage)
    assert sample_id == demo.demo_id(2)
    assert not storage.exists(f"raw_reads/{demo.demo_id(1)}.fastq.gz")
