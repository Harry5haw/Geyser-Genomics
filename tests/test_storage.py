import io

import pytest

from storage import LocalStorage, ObjectNotFound, storage_from_env


@pytest.fixture(scope="module", autouse=True)
def no_ambient_aws(tmp_path_factory):
    """Keep the developer's AWS setup out of these tests: boto3 also reads endpoints, S3 settings and
    credentials from env vars and the AWS config file (this session's AWS_PROFILE included). Dummy keys,
    because clients resolve credentials when they are built. boto3 caches one default session and the
    config it read, so the module gets a fresh one and the old one comes back afterwards."""
    import boto3

    from storage import S3_ENDPOINT_VARS

    empty = tmp_path_factory.mktemp("no-aws")
    with pytest.MonkeyPatch.context() as mp:
        for var in (*S3_ENDPOINT_VARS, "AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN"):
            mp.delenv(var, raising=False)
        mp.setenv("AWS_CONFIG_FILE", str(empty / "config"))
        mp.setenv("AWS_SHARED_CREDENTIALS_FILE", str(empty / "credentials"))
        mp.setenv("AWS_ACCESS_KEY_ID", "test")
        mp.setenv("AWS_SECRET_ACCESS_KEY", "test")
        mp.setenv("AWS_EC2_METADATA_DISABLED", "true")
        mp.setattr(boto3, "DEFAULT_SESSION", None)
        yield


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


def _gzip_over_s3_body(data):
    """What decompress uploads: a GzipFile over an S3 body, wrapped so it is read front to back."""
    import gzip

    from storage import ForwardOnlyStream

    return ForwardOnlyStream(gzip.GzipFile(fileobj=_NonSeekable(gzip.compress(data)), mode="rb"))


def _capture_bodies(s3, operation):
    bodies = []

    def capture(params, **kwargs):
        bodies.append(params["Body"].read())
        params["Body"].seek(0)

    s3._client.meta.events.register(f"before-parameter-build.s3.{operation}", capture)
    return bodies


def test_s3_upload_of_gzip_over_s3_body_never_seeks():
    # #71: the first cloud run died in decompress. GzipFile says seekable() even over a StreamingBody;
    # s3transfer seeked it to measure the size, and the backward seek raised io.UnsupportedOperation.
    # Responses are stubbed without expected params, so the test doesn't depend on which extra
    # arguments (checksums etc.) the installed boto3 adds.
    from botocore.stub import Stubber

    from storage import S3Storage

    fastq = b"@r1\nACGTACGT\n+\nIIIIIIII\n" * 500
    s = S3Storage("bucket", "eu-west-2")
    bodies = _capture_bodies(s, "PutObject")
    with Stubber(s._client) as stub:
        stub.add_response("put_object", {})
        s.upload_stream(_gzip_over_s3_body(fastq), "decompressed/X.fastq")
        stub.assert_no_pending_responses()
    assert bodies == [fastq]


def test_s3_upload_of_large_gzip_over_s3_body_goes_multipart_without_seeking():
    # Real FASTQs exceed the 8 MiB multipart threshold, which takes s3transfer's other (multipart) path.
    import os

    from botocore.stub import Stubber

    from storage import S3Storage

    data = os.urandom(1024) * (9 * 1024)  # 9 MiB, compresses well
    s = S3Storage("bucket", "eu-west-2")
    parts = _capture_bodies(s, "UploadPart")
    with Stubber(s._client) as stub:
        stub.add_response("create_multipart_upload", {"UploadId": "u"})
        stub.add_response("upload_part", {"ETag": '"e1"'})
        stub.add_response("complete_multipart_upload", {})
        s.upload_stream(_gzip_over_s3_body(data), "decompressed/BIG.fastq")
        stub.assert_no_pending_responses()
    assert b"".join(parts) == data


def test_s3_upload_stream_passes_the_large_part_config(monkeypatch):
    # An unknown-size upload is cut into multipart_chunksize parts, max 10,000: this sets the size cap.
    from storage import S3Storage

    s = S3Storage("bucket", "eu-west-2")
    seen = {}
    monkeypatch.setattr(s._client, "upload_fileobj", lambda f, b, k, Config=None: seen.update(config=Config))
    s.upload_stream(io.BytesIO(b"x"), "k")
    config = seen["config"]
    assert config.multipart_chunksize * 10_000 >= 600 * 2**30  # a decompressed 30x FASTQ fits
    assert config.multipart_chunksize * config.max_in_memory_upload_chunks <= 512 * 2**20  # 4 GB job


def test_forward_only_stream_fills_each_read():
    # s3transfer makes one multipart part per read; a short read would make a part under S3's 5 MiB minimum.
    from storage import ForwardOnlyStream

    class Trickle(io.RawIOBase):
        def __init__(self, data):
            self._buf = io.BytesIO(data)

        def readable(self):
            return True

        def readinto(self, b):
            return self._buf.readinto(memoryview(b)[:3])

    stream = ForwardOnlyStream(Trickle(b"abcdefghij" * 3))
    assert not stream.seekable()
    assert stream.read(10) == b"abcdefghij"
    assert stream.read(25) == b"abcdefghij" * 2
    assert stream.read(10) == b""


class _BrokenStream(io.RawIOBase):
    def readinto(self, buf):
        raise OSError("connection dropped")


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


# --- S3-compatible endpoints (#80): OVHcloud Object Storage, MinIO, LocalStack ----------------------

OVH_ENDPOINT = "https://s3.gra.io.cloud.ovh.net"


class _Sent(Exception):
    """Raised from botocore's before-send hook, so a test sees the real request and nothing goes out."""


def _sent_request(s3, call):
    """The request botocore would send for ``call(s3)`` (dummy credentials from the fixture), never sent."""
    seen = {}

    def capture(request, **kwargs):
        seen["request"] = request
        raise _Sent()

    s3._client.meta.events.register("before-send.s3", capture)
    with pytest.raises(_Sent):
        call(s3)
    return seen["request"]


def _checksum_headers(request):
    """Flexible-checksum headers (x-amz-checksum-*, x-amz-sdk-checksum-algorithm) on a request."""
    return sorted(h for h in request.headers if h.lower().startswith(("x-amz-checksum", "x-amz-sdk-checksum")))


def test_s3_defaults_to_aws_when_no_endpoint_is_set():
    s = storage_from_env({"STORAGE_BACKEND": "s3", "BUCKET_NAME": "bucket"})
    assert s._client.meta.endpoint_url == "https://s3.eu-west-2.amazonaws.com"
    assert s._client.meta.region_name == "eu-west-2"


def test_s3_empty_endpoint_and_style_mean_unset():
    # The web UI passes every variable, empty when not configured.
    s = storage_from_env({"STORAGE_BACKEND": "s3", "BUCKET_NAME": "bucket",
                          "S3_ENDPOINT_URL": "", "S3_ADDRESSING_STYLE": ""})
    assert s._client.meta.endpoint_url == "https://s3.eu-west-2.amazonaws.com"


def test_s3_endpoint_url_and_region_reach_the_client():
    s = storage_from_env({"STORAGE_BACKEND": "s3", "BUCKET_NAME": "bucket", "AWS_REGION": "gra",
                          "S3_ENDPOINT_URL": OVH_ENDPOINT})
    assert s._client.meta.endpoint_url == OVH_ENDPOINT
    assert s._client.meta.region_name == "gra"
    assert s.describe("variants/S1.vcf.gz") == "s3://bucket/variants/S1.vcf.gz"


def test_s3_requests_go_to_the_custom_endpoint_path_style():
    s = storage_from_env({"STORAGE_BACKEND": "s3", "BUCKET_NAME": "bucket", "AWS_REGION": "gra",
                          "S3_ENDPOINT_URL": OVH_ENDPOINT, "S3_ADDRESSING_STYLE": "PATH"})
    url = _sent_request(s, lambda st: st.exists("raw_reads/S1.fastq.gz")).url
    assert url == f"{OVH_ENDPOINT}/bucket/raw_reads/S1.fastq.gz"


def test_s3_requests_go_to_the_custom_endpoint_virtual_style():
    s = storage_from_env({"STORAGE_BACKEND": "s3", "BUCKET_NAME": "bucket", "AWS_REGION": "gra",
                          "S3_ENDPOINT_URL": OVH_ENDPOINT, "S3_ADDRESSING_STYLE": "virtual"})
    url = _sent_request(s, lambda st: st.exists("raw_reads/S1.fastq.gz")).url
    assert url == "https://bucket.s3.gra.io.cloud.ovh.net/raw_reads/S1.fastq.gz"


def test_s3_without_endpoint_still_sends_to_aws():
    s = storage_from_env({"STORAGE_BACKEND": "s3", "BUCKET_NAME": "bucket"})
    url = _sent_request(s, lambda st: st.exists("raw_reads/S1.fastq.gz")).url
    assert url == "https://bucket.s3.eu-west-2.amazonaws.com/raw_reads/S1.fastq.gz"


def test_s3_rejects_an_unknown_addressing_style():
    with pytest.raises(ValueError, match="addressing style"):
        storage_from_env({"STORAGE_BACKEND": "s3", "BUCKET_NAME": "bucket", "S3_ADDRESSING_STYLE": "dns"})


def test_s3_compatible_uploads_send_no_default_checksums():
    # boto3 >= 1.36 adds CRC32 checksums to every upload by default; many S3-compatible services reject them.
    s = storage_from_env({"STORAGE_BACKEND": "s3", "BUCKET_NAME": "bucket", "AWS_REGION": "gra",
                          "S3_ENDPOINT_URL": OVH_ENDPOINT})
    request = _sent_request(s, lambda st: st._client.put_object(Bucket="bucket", Key="k", Body=b"x"))
    assert _checksum_headers(request) == []


def test_aws_uploads_keep_boto3s_default_checksums():
    # The control for the test above: on AWS nothing changes, and the headers are visible to the check.
    s = storage_from_env({"STORAGE_BACKEND": "s3", "BUCKET_NAME": "bucket"})
    request = _sent_request(s, lambda st: st._client.put_object(Bucket="bucket", Key="k", Body=b"x"))
    assert _checksum_headers(request) != []


def test_an_explicit_aws_endpoint_keeps_aws_behaviour():
    from storage import is_aws_endpoint

    s = storage_from_env({"STORAGE_BACKEND": "s3", "BUCKET_NAME": "bucket",
                          "S3_ENDPOINT_URL": "https://bucket.vpce-0abc.s3.eu-west-2.vpce.amazonaws.com"})
    assert s._client.meta.config.request_checksum_calculation != "when_required"
    assert is_aws_endpoint("https://s3.eu-west-2.amazonaws.com")
    assert is_aws_endpoint(None)
    assert not is_aws_endpoint(OVH_ENDPOINT)
    assert not is_aws_endpoint("https://amazonaws.com.evil.example")


def test_boto3s_own_endpoint_variable_is_honoured():
    s = storage_from_env({"STORAGE_BACKEND": "s3", "BUCKET_NAME": "bucket", "AWS_REGION": "gra",
                          "AWS_ENDPOINT_URL_S3": OVH_ENDPOINT})
    assert s._client.meta.endpoint_url == OVH_ENDPOINT
    assert s._client.meta.config.request_checksum_calculation == "when_required"


def test_s3_settings_are_trimmed_and_case_insensitive():
    from storage import S3Storage

    s = S3Storage("bucket", "gra", endpoint_url=f" {OVH_ENDPOINT}\n", addressing_style=" Path ")
    assert s._client.meta.endpoint_url == OVH_ENDPOINT
    assert s._client.meta.config.s3["addressing_style"] == "path"


def test_boto3s_generic_endpoint_variable_counts_too():
    from storage import s3_endpoint_from_env

    assert s3_endpoint_from_env({"AWS_ENDPOINT_URL": "http://localhost:9000"}) == "http://localhost:9000"
    assert s3_endpoint_from_env({"S3_ENDPOINT_URL": OVH_ENDPOINT, "AWS_ENDPOINT_URL_S3": "http://x",
                                 "AWS_ENDPOINT_URL": "http://y"}) == OVH_ENDPOINT
    assert s3_endpoint_from_env({"S3_ENDPOINT_URL": "  ", "AWS_ENDPOINT_URL_S3": "http://x"}) == "http://x"


def test_s3_endpoint_without_a_scheme_is_rejected():
    with pytest.raises(ValueError, match="http"):
        storage_from_env({"STORAGE_BACKEND": "s3", "BUCKET_NAME": "bucket",
                          "S3_ENDPOINT_URL": "s3.eu-west-2.amazonaws.com"})
