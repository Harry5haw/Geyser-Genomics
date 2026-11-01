import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from .web_helpers import fastq_gz, put


def upload(client, name="SRR062634.fastq.gz", data=None, sample_id=""):
    f = SimpleUploadedFile(name, fastq_gz() if data is None else data, content_type="application/gzip")
    return client.post("/upload/", {"reads": f, "sample_id": sample_id})


def test_upload_stores_raw_key_with_id_from_filename(client, storage):
    resp = upload(client)
    assert resp.status_code == 302 and resp["Location"] == "/samples/SRR062634/"
    with storage.open_read("raw_reads/SRR062634.fastq.gz") as fh:
        assert fh.read() == fastq_gz()


def test_upload_with_explicit_sample_id(client, storage):
    resp = upload(client, name="reads.fq.gz", sample_id="My_Sample-1")
    assert resp.status_code == 302
    assert storage.exists("raw_reads/My_Sample-1.fastq.gz")


@pytest.mark.parametrize("sample_id", ["../evil", "a.b", "a/b", "sp ace", "x" * 65, "<script>"])
def test_rejects_unsafe_sample_ids(client, storage, sample_id):
    resp = upload(client, sample_id=sample_id)
    assert resp.status_code == 400
    assert list(storage.list_keys("")) == []


def test_rejects_filename_that_gives_no_valid_id(client, storage):
    assert upload(client, name="my reads.fastq.gz").status_code == 400
    assert list(storage.list_keys("")) == []


def test_rejects_wrong_extension_and_non_gzip(client, storage):
    assert upload(client, name="S1.fastq").status_code == 400
    resp = upload(client, name="S1.fastq.gz", data=b"@r1\nACGT\n+\nIIII\n")
    assert resp.status_code == 400
    assert b"isn&#x27;t gzip-compressed" in resp.content
    assert list(storage.list_keys("")) == []


def test_rejects_oversized_file(client, storage, settings):
    settings.GEYSER_MAX_UPLOAD_MB = 1
    resp = upload(client, data=b"\x1f\x8b" + b"0" * (1024 * 1024 + 10))
    assert resp.status_code == 400
    assert b"1 MB limit" in resp.content
    assert not storage.exists("raw_reads/SRR062634.fastq.gz")


def test_rejects_oversized_body_before_parsing(client, storage, settings):
    settings.GEYSER_MAX_UPLOAD_MB = 1
    resp = upload(client, data=b"\x1f\x8b" + b"0" * (3 * 1024 * 1024))
    assert resp.status_code == 302 and resp["Location"] == "/"
    assert not storage.exists("raw_reads/SRR062634.fastq.gz")


def test_rejects_duplicate_sample(client, storage):
    put(storage, "raw_reads/SRR062634.fastq.gz", b"original")
    resp = upload(client)
    assert resp.status_code == 400 and b"already exists" in resp.content
    with storage.open_read("raw_reads/SRR062634.fastq.gz") as fh:
        assert fh.read() == b"original"


def test_csrf_is_enforced(storage):
    client = Client(enforce_csrf_checks=True)
    assert upload(client).status_code == 403
    assert client.post("/demo/").status_code == 403
    assert list(storage.list_keys("")) == []


def test_upload_and_demo_are_post_only(client, storage):
    assert client.get("/upload/").status_code == 405
    assert client.get("/demo/").status_code == 405
