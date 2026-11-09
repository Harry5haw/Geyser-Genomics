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


def test_the_default_limit_matches_the_hosted_upload_page():
    # 256 GiB per file, as the hosted upload page (web_max_upload_gb); still set by the env var.
    from django.conf import settings

    from samples.forms import UploadForm

    assert settings.GEYSER_MAX_UPLOAD_MB == 256 * 1024
    assert UploadForm.max_bytes() == 256 * 1024**3
    assert UploadForm.limit_label() == "256 GB"


@pytest.mark.parametrize(
    "mb,label",
    [
        (1, "1 MB"),
        (200, "200 MB"),
        (1023, "1023 MB"),
        (1024, "1 GB"),
        (1536, "1.5 GB"),
        (65536, "64 GB"),
        (262144, "256 GB"),
    ],
)
def test_the_limit_reads_in_gb_from_1024_mb(mb, label):
    from samples.forms import upload_limit_label

    assert upload_limit_label(mb) == label


def test_the_list_page_shows_the_limit_in_gb(client, storage, settings):
    settings.GEYSER_MAX_UPLOAD_MB = 256 * 1024
    page = client.get("/").content.decode()
    assert "(up to 256 GB)" in page and f'data-max-bytes="{256 * 1024**3}"' in page


def test_an_oversized_file_names_the_limit_in_gb(client, storage, settings):
    settings.GEYSER_MAX_UPLOAD_MB = 1024
    resp = client.post("/upload/", {"sample_id": ""}, CONTENT_LENGTH=str(3 * 1024**3))
    assert resp.status_code == 302 and resp["Location"] == "/"
    assert "larger than the 1 GB limit" in str([m.message for m in resp.wsgi_request._messages])
