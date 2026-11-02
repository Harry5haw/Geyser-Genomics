import io

import pytest

from storage import LocalStorage, ObjectNotFound, storage_from_env


def test_upload_download_round_trip(tmp_path):
    s = LocalStorage(tmp_path / "root")
    src = tmp_path / "in.txt"
    src.write_bytes(b"hello")
    s.upload(str(src), "alignments/x.bam")
    assert s.exists("alignments/x.bam")
    dst = tmp_path / "out.txt"
    s.download("alignments/x.bam", str(dst))
    assert dst.read_bytes() == b"hello"


def test_stream_round_trip(tmp_path):
    s = LocalStorage(tmp_path)
    s.upload_stream(io.BytesIO(b"abc" * 1000), "decompressed/x.fastq")
    with s.open_read("decompressed/x.fastq") as fh:
        assert fh.read() == b"abc" * 1000


def test_missing_key_raises_object_not_found(tmp_path):
    s = LocalStorage(tmp_path)
    assert not s.exists("nope")
    with pytest.raises(ObjectNotFound):
        s.download("nope", str(tmp_path / "x"))
    with pytest.raises(ObjectNotFound):
        s.open_read("nope")


def test_keys_cannot_escape_root(tmp_path):
    s = LocalStorage(tmp_path / "root")
    with pytest.raises(ValueError):
        s.exists("../outside.txt")


def test_storage_from_env_local(tmp_path):
    s = storage_from_env({"STORAGE_BACKEND": "local", "LOCAL_STORAGE_DIR": str(tmp_path)})
    assert isinstance(s, LocalStorage)


def test_storage_from_env_s3_requires_bucket():
    with pytest.raises(RuntimeError, match="BUCKET_NAME"):
        storage_from_env({"STORAGE_BACKEND": "s3"})


def test_storage_from_env_rejects_unknown_backend():
    with pytest.raises(RuntimeError):
        storage_from_env({"STORAGE_BACKEND": "ftp"})


def test_list_keys_filters_by_prefix_and_sorts(tmp_path):
    s = LocalStorage(tmp_path)
    for key in ["raw_reads/B.fastq.gz", "raw_reads/A.fastq.gz", "raw_reads_old/C.fastq.gz",
                "variants/A.vcf.gz", "metrics/metrics.jsonl"]:
        s.upload_stream(io.BytesIO(b"x"), key)
    assert list(s.list_keys("raw_reads/")) == ["raw_reads/A.fastq.gz", "raw_reads/B.fastq.gz"]
    assert list(s.list_keys("raw_reads")) == [
        "raw_reads/A.fastq.gz", "raw_reads/B.fastq.gz", "raw_reads_old/C.fastq.gz"]
    assert list(s.list_keys("variants/A")) == ["variants/A.vcf.gz"]
    assert len(list(s.list_keys())) == 5


def test_list_keys_missing_prefix_is_empty(tmp_path):
    assert list(LocalStorage(tmp_path / "nothing-here").list_keys("raw_reads/")) == []


def test_list_keys_cannot_escape_root(tmp_path):
    with pytest.raises(ValueError):
        list(LocalStorage(tmp_path / "root").list_keys("../"))


def test_s3_list_keys_follows_pagination():
    from botocore.stub import Stubber

    from storage import S3Storage

    s = S3Storage("bucket", "eu-west-2")
    with Stubber(s._client) as stub:
        stub.add_response(
            "list_objects_v2",
            {"Contents": [{"Key": "raw_reads/A.fastq.gz"}], "IsTruncated": True, "NextContinuationToken": "t"},
            {"Bucket": "bucket", "Prefix": "raw_reads/"},
        )
        stub.add_response(
            "list_objects_v2",
            {"Contents": [{"Key": "raw_reads/B.fastq.gz"}], "IsTruncated": False},
            {"Bucket": "bucket", "Prefix": "raw_reads/", "ContinuationToken": "t"},
        )
        assert list(s.list_keys("raw_reads/")) == ["raw_reads/A.fastq.gz", "raw_reads/B.fastq.gz"]


class _NonSeekable(io.RawIOBase):
    """Like botocore's StreamingBody (an S3 GetObject body): forward reads only."""

    def __init__(self, data):
        self._buf = io.BytesIO(data)

    def readable(self):
        return True

    def readinto(self, b):
        return self._buf.readinto(b)

    def seekable(self):
        return False


def test_s3_upload_stream_never_seeks_a_gzip_over_a_non_seekable_body():
    # #71: the first cloud run died in decompress. GzipFile says seekable() even over a StreamingBody;
    # s3transfer seeked it to measure the size, and the backward seek raised io.UnsupportedOperation.
    import gzip

    from botocore.stub import ANY, Stubber

    from storage import S3Storage

    fastq = b"@r1\nACGTACGT\n+\nIIIIIIII\n" * 500
    s = S3Storage("bucket", "eu-west-2")
    uploaded = []

    def capture(params, **kwargs):
        uploaded.append(params["Body"].read())
        params["Body"].seek(0)

    s._client.meta.events.register("before-parameter-build.s3.PutObject", capture)
    with Stubber(s._client) as stub:
        stub.add_response("put_object", {}, {"Bucket": "bucket", "Key": "decompressed/X.fastq",
                                             "Body": ANY, "ChecksumAlgorithm": ANY})
        with gzip.GzipFile(fileobj=_NonSeekable(gzip.compress(fastq)), mode="rb") as unzipped:
            s.upload_stream(unzipped, "decompressed/X.fastq")
        stub.assert_no_pending_responses()
    assert uploaded == [fastq]


def test_s3_upload_stream_large_gzip_goes_multipart_without_seeking():
    # Real FASTQs exceed the 8 MiB multipart threshold, which takes s3transfer's other (multipart) path.
    import gzip
    import os

    from botocore.stub import ANY, Stubber

    from storage import S3Storage

    data = os.urandom(1024) * (9 * 1024)  # 9 MiB, compresses well
    s = S3Storage("bucket", "eu-west-2")
    parts = []

    def capture(params, **kwargs):
        parts.append(params["Body"].read())
        params["Body"].seek(0)

    s._client.meta.events.register("before-parameter-build.s3.UploadPart", capture)
    key = "decompressed/BIG.fastq"
    with Stubber(s._client) as stub:
        stub.add_response("create_multipart_upload", {"UploadId": "u"}, {"Bucket": "bucket", "Key": key,
                                                                           "ChecksumAlgorithm": ANY})
        stub.add_response("upload_part", {"ETag": '"e1"'}, {"Bucket": "bucket", "Key": key, "UploadId": "u",
                                                           "PartNumber": 1, "Body": ANY,
                                                           "ChecksumAlgorithm": ANY})
        stub.add_response("complete_multipart_upload", {}, {"Bucket": "bucket", "Key": key, "UploadId": "u",
                                                             "MultipartUpload": ANY})
        with gzip.GzipFile(fileobj=_NonSeekable(gzip.compress(data)), mode="rb") as unzipped:
            s.upload_stream(unzipped, key)
        stub.assert_no_pending_responses()
    assert b"".join(parts) == data


def test_s3_stream_chunk_size_allows_a_decompressed_30x_fastq():
    # An unknown-size upload is split into chunks of this size, and S3 allows at most 10,000 parts.
    from storage import _STREAM_CHUNK_BYTES

    assert _STREAM_CHUNK_BYTES * 10_000 >= 500 * 2**30


class _BrokenStream(io.RawIOBase):
    def readinto(self, buf):
        raise IOError("connection dropped")


def test_failed_write_leaves_no_key_and_no_temp_file(tmp_path):
    s = LocalStorage(tmp_path)
    with pytest.raises(IOError):
        s.upload_stream(_BrokenStream(), "raw_reads/X.fastq.gz")
    assert not s.exists("raw_reads/X.fastq.gz")
    assert list((tmp_path / "raw_reads").iterdir()) == []


def test_overwrite_replaces_whole_object_and_hides_temp_files(tmp_path):
    s = LocalStorage(tmp_path)
    s.upload_stream(io.BytesIO(b"first version"), "runs/X.json")
    s.upload_stream(io.BytesIO(b"v2"), "runs/X.json")
    with s.open_read("runs/X.json") as fh:
        assert fh.read() == b"v2"
    (tmp_path / "runs" / ".Y.json.123.part").write_bytes(b"partial")
    assert list(s.list_keys("runs/")) == ["runs/X.json"]


def _client_error(code):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": code}}, "op")


@pytest.mark.parametrize("code", ["404", "NoSuchKey", "NotFound"])
def test_s3_missing_object_maps_to_object_not_found(code, monkeypatch, tmp_path):
    from storage import S3Storage

    s = S3Storage("bucket", "eu-west-2")

    def raise_missing(*a, **k):
        raise _client_error(code)

    for method in ("head_object", "download_file", "get_object"):
        monkeypatch.setattr(s._client, method, raise_missing)

    assert s.exists("raw_reads/A.fastq.gz") is False
    with pytest.raises(ObjectNotFound):
        s.download("raw_reads/A.fastq.gz", str(tmp_path / "a"))
    with pytest.raises(ObjectNotFound):
        s.open_read("raw_reads/A.fastq.gz")


def test_s3_permission_errors_are_not_mistaken_for_missing_objects(monkeypatch, tmp_path):
    # A 403 means bad credentials or IAM, not "file absent": it must surface, not become False/ObjectNotFound.
    from botocore.exceptions import ClientError

    from storage import S3Storage

    s = S3Storage("bucket", "eu-west-2")

    def raise_denied(*a, **k):
        raise _client_error("403")

    for method in ("head_object", "download_file", "get_object"):
        monkeypatch.setattr(s._client, method, raise_denied)

    with pytest.raises(ClientError):
        s.exists("k")
    with pytest.raises(ClientError):
        s.download("k", str(tmp_path / "k"))
    with pytest.raises(ClientError):
        s.open_read("k")


def test_s3_describe_is_an_s3_url():
    from storage import S3Storage

    assert S3Storage("bucket", "eu-west-2").describe("variants/S1.vcf.gz") == "s3://bucket/variants/S1.vcf.gz"
