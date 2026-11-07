"""Storage backends for the pipeline.

Stages only ever talk to each other through object keys such as
``raw_reads/<id>.fastq.gz`` or ``variants/<id>.vcf.gz``. This module hides where
those keys live:

* ``S3Storage``    - keys are objects in an S3 bucket (the cloud deployment), on AWS or on any
                     S3-compatible service via ``S3_ENDPOINT_URL``.
* ``LocalStorage`` - keys are files under a local directory (offline development).

Select one with ``STORAGE_BACKEND=s3|local``. See ``storage_from_env``.
"""

import hashlib
import json
import os
import shutil
from collections.abc import Iterator
from typing import BinaryIO


class ObjectNotFound(Exception):
    """Raised when a key does not exist in the backend."""


# Presigned URLs (the streaming merge and region-streamed calling read objects in place over HTTPS) must outlive
# the longest job that reads them: the merge's 4 h timeout.
PRESIGN_SECONDS = 6 * 3600


class LocalStorage:
    """Maps keys onto files beneath ``root``. ``raw_reads/x.fastq.gz`` -> ``<root>/raw_reads/x.fastq.gz``."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)

    def describe(self, key: str) -> str:
        return self._path(key)

    def _path(self, key: str) -> str:
        path = os.path.abspath(os.path.join(self.root, *key.split("/")))
        if os.path.commonpath([path, self.root]) != self.root:
            raise ValueError(f"Key escapes storage root: {key!r}")
        return path

    def exists(self, key: str) -> bool:
        return os.path.isfile(self._path(key))

    def download(self, key: str, local_path: str, parallel: bool = False) -> None:
        """parallel: many concurrent ranged GETs (S3 only; a local copy is one read anyway)."""
        src = self._path(key)
        if not os.path.isfile(src):
            raise ObjectNotFound(key)
        shutil.copyfile(src, local_path)

    def upload(self, local_path: str, key: str, metadata: dict = None) -> None:
        """metadata: user metadata, as S3 keeps it with the object (here a hidden sidecar file that listings skip).
        An upload without metadata clears any earlier object's, as an S3 PUT does."""
        with open(local_path, "rb") as src:
            self.upload_stream(src, key)
        sidecar = self._meta_path(key)
        if metadata:
            with open(sidecar, "w", encoding="utf-8") as fh:
                json.dump({k.lower(): str(v) for k, v in metadata.items()}, fh)
        elif os.path.exists(sidecar):
            os.remove(sidecar)

    def _meta_path(self, key: str) -> str:
        path = self._path(key)
        return os.path.join(os.path.dirname(path), f".{os.path.basename(path)}.meta.json")

    def head(self, key: str) -> dict:
        """{"etag", "size", "metadata"} without reading the object's data into the caller. The ETag is the
        content's MD5, as S3 gives for a single-part upload."""
        path = self._path(key)
        if not os.path.isfile(path):
            raise ObjectNotFound(key)
        md5 = hashlib.md5()  # noqa: S324 - an ETag, not security
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                md5.update(block)
        metadata = {}
        if os.path.exists(self._meta_path(key)):
            with open(self._meta_path(key), encoding="utf-8") as fh:
                metadata = json.load(fh)
        return {"etag": md5.hexdigest(), "size": os.path.getsize(path), "metadata": metadata}

    def presigned_url(self, key: str, expires: int = PRESIGN_SECONDS) -> str:
        """Where a tool can read the object in place: locally, the file itself."""
        path = self._path(key)
        if not os.path.isfile(path):
            raise ObjectNotFound(key)
        return path

    def open_read(self, key: str) -> BinaryIO:
        path = self._path(key)
        if not os.path.isfile(path):
            raise ObjectNotFound(key)
        return open(path, "rb")

    def upload_stream(self, stream: BinaryIO, key: str) -> None:
        """Write to a hidden temp file, then rename, so a key only ever appears complete (as in S3).

        Without this, a reader polling the folder (the web UI, the local worker) could see a
        half-written upload or stage output under its final key.
        """
        dst = self._path(key)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = os.path.join(os.path.dirname(dst), f".{os.path.basename(dst)}.{os.getpid()}.part")
        try:
            with open(tmp, "wb") as out:
                shutil.copyfileobj(stream, out, length=1024 * 1024)
            os.replace(tmp, dst)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        sidecar = self._meta_path(key)
        if os.path.exists(sidecar):  # a new object has no metadata until upload() gives it some
            os.remove(sidecar)

    def list_keys(self, prefix: str = "") -> Iterator[str]:
        """Yield every key starting with ``prefix``, sorted, like an S3 listing."""
        # Walk only the deepest directory the prefix names; the rest of the prefix filters filenames.
        base = self._path(prefix.rsplit("/", 1)[0]) if "/" in prefix else self.root
        keys = []
        for dirpath, _, filenames in os.walk(base):
            for name in filenames:
                if name.startswith("."):
                    continue  # in-progress writes (see upload_stream)
                key = os.path.relpath(os.path.join(dirpath, name), self.root).replace(os.sep, "/")
                if key.startswith(prefix):
                    keys.append(key)
        yield from sorted(keys)


# Unknown-size (forward-only) uploads are split into parts of this size, and S3 allows at most
# 10,000 parts: 64 MiB caps one upload at 625 GiB (the 8 MiB default would cap it at 78 GiB, less
# than a decompressed 30x FASTQ). s3transfer holds at most _STREAM_MAX_CHUNKS parts in memory.
_STREAM_CHUNK_BYTES = 64 * 1024 * 1024
_STREAM_MAX_CHUNKS = 6  # <= 384 MiB of the 4 GB Batch job


class ForwardOnlyStream:
    """Read-only view of a stream that says it cannot seek, so uploaders read it front to back.

    Wrap any stream whose ``seekable()`` is wrong, above all ``gzip.GzipFile`` over a network body:
    it reports True, but seeking backwards rewinds the body underneath, which an S3 ``StreamingBody``
    cannot do (#71). ``read(n)`` keeps reading until it has ``n`` bytes or reaches EOF, because
    s3transfer turns each read into one multipart part and parts other than the last must be >= 5 MiB.
    """

    def __init__(self, stream: BinaryIO):
        self._stream = stream

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            return self._stream.read()
        chunks, remaining = [], size
        while remaining:
            chunk = self._stream.read(remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False


_stream_config = None
_parallel_config = None
# Concurrent ranged GETs for a parallel download: one S3 connection tops out at roughly 50-100 MB/s.
PARALLEL_DOWNLOAD_THREADS = 32


def _parallel_transfer_config():
    """Built once, lazily, so local runs never import boto3."""
    global _parallel_config
    if _parallel_config is None:
        from boto3.s3.transfer import TransferConfig

        _parallel_config = TransferConfig(
            multipart_threshold=_STREAM_CHUNK_BYTES,
            multipart_chunksize=_STREAM_CHUNK_BYTES,
            max_concurrency=PARALLEL_DOWNLOAD_THREADS,
        )
    return _parallel_config


def _stream_transfer_config():
    """Built once, lazily, so local runs never import boto3."""
    global _stream_config
    if _stream_config is None:
        from boto3.s3.transfer import TransferConfig

        _stream_config = TransferConfig(multipart_chunksize=_STREAM_CHUNK_BYTES)
        _stream_config.max_in_memory_upload_chunks = _STREAM_MAX_CHUNKS
    return _stream_config


ADDRESSING_STYLES = ("auto", "path", "virtual")


# Checked in this order. The last two are boto3's own variables: boto3 obeys them even if we don't, so
# they must also switch on the S3-compatible settings below and the metrics default in metrics.py.
S3_ENDPOINT_VARS = ("S3_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3", "AWS_ENDPOINT_URL")


def s3_endpoint_from_env(env) -> str:
    """The S3 endpoint the environment asks for (see ``S3_ENDPOINT_VARS``), or None for AWS's default."""
    for var in S3_ENDPOINT_VARS:
        value = (env.get(var) or "").strip()
        if value:
            return value
    return None


def is_aws_endpoint(endpoint_url) -> bool:
    """True for no endpoint (boto3's default) or an AWS one (regional, FIPS, VPC interface endpoint)."""
    if not endpoint_url:
        return True
    from urllib.parse import urlparse

    host = (urlparse(endpoint_url).hostname or "").lower()
    return host == "amazonaws.com" or host.endswith((".amazonaws.com", ".amazonaws.com.cn"))


class S3Storage:
    """Keys are objects in one S3 bucket. boto3 is imported lazily so local runs never need it.

    ``endpoint_url`` points the client at an S3-compatible service instead of AWS (OVHcloud Object
    Storage, MinIO, LocalStack); ``addressing_style`` is botocore's ``path`` / ``virtual`` / ``auto``,
    for services that only accept one. Both default to boto3's AWS behaviour.

    Outside AWS, checksums are only sent when an operation requires them: since 1.36 boto3 adds CRC32
    checksums (and aws-chunked uploads) to every PutObject/UploadPart by default, which many
    S3-compatible services reject. Sign with the service's own region (``AWS_REGION``, e.g. ``gra``).
    """

    def __init__(self, bucket: str, region: str, endpoint_url: str = None, addressing_style: str = None):
        import boto3
        from botocore.config import Config

        endpoint_url = (endpoint_url or "").strip() or None
        if endpoint_url and not endpoint_url.lower().startswith(("http://", "https://")):
            raise ValueError(f"S3 endpoint must start with http:// or https://: {endpoint_url!r}")
        addressing_style = (addressing_style or "").strip().lower() or None
        if addressing_style and addressing_style not in ADDRESSING_STYLES:
            raise ValueError(f"Unknown S3 addressing style {addressing_style!r} (expected one of {ADDRESSING_STYLES})")
        self.bucket = bucket
        kwargs = {"region_name": region}
        config = {}
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url
            if not is_aws_endpoint(endpoint_url):
                config.update(
                    request_checksum_calculation="when_required", response_checksum_validation="when_required"
                )
        if addressing_style:
            config["s3"] = {"addressing_style": addressing_style}
        if config:
            kwargs["config"] = Config(**config)
        self._client = boto3.client("s3", **kwargs)

    def describe(self, key: str) -> str:
        return f"s3://{self.bucket}/{key}"

    @staticmethod
    def _is_not_found(err) -> bool:
        code = err.response.get("Error", {}).get("Code", "")
        return code in ("404", "NoSuchKey", "NotFound")

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as err:
            if self._is_not_found(err):
                return False
            raise

    def download(self, key: str, local_path: str, parallel: bool = False) -> None:
        """parallel: PARALLEL_DOWNLOAD_THREADS concurrent ranged GETs of 64 MiB parts (the large route's
        pre-flight); otherwise boto3's defaults."""
        from botocore.exceptions import ClientError

        config = {"Config": _parallel_transfer_config()} if parallel else {}
        try:
            self._client.download_file(self.bucket, key, local_path, **config)
        except ClientError as err:
            if self._is_not_found(err):
                raise ObjectNotFound(key) from err
            raise

    def upload(self, local_path: str, key: str, metadata: dict = None) -> None:
        extra = {"Metadata": {k.lower(): str(v) for k, v in metadata.items()}} if metadata else None
        self._client.upload_file(local_path, self.bucket, key, ExtraArgs=extra)

    def head(self, key: str) -> dict:
        """{"etag", "size", "metadata"} from HeadObject (the ETag without its quotes)."""
        from botocore.exceptions import ClientError

        try:
            head = self._client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as err:
            if self._is_not_found(err):
                raise ObjectNotFound(key) from err
            raise
        return {
            "etag": (head.get("ETag") or "").strip('"'),
            "size": head.get("ContentLength"),
            "metadata": {k.lower(): v for k, v in (head.get("Metadata") or {}).items()},
        }

    def presigned_url(self, key: str, expires: int = PRESIGN_SECONDS) -> str:
        """A time-limited HTTPS GET URL, signed with this process's own credentials (the job role), so htslib
        can range-read the object without S3 credentials of its own."""
        return self._client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires
        )

    def open_read(self, key: str) -> BinaryIO:
        from botocore.exceptions import ClientError

        try:
            return self._client.get_object(Bucket=self.bucket, Key=key)["Body"]
        except ClientError as err:
            if self._is_not_found(err):
                raise ObjectNotFound(key) from err
            raise

    def upload_stream(self, stream: BinaryIO, key: str) -> None:
        """Upload a stream. A seekable one is sized and sent in parallel parts; wrap a stream whose
        ``seekable()`` lies (a GzipFile over an S3 body) in ForwardOnlyStream so it is never seeked."""
        self._client.upload_fileobj(stream, self.bucket, key, Config=_stream_transfer_config())

    def list_keys(self, prefix: str = "") -> Iterator[str]:
        """Yield every key starting with ``prefix`` (S3 returns them in sorted order)."""
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                yield obj["Key"]


def storage_from_env(env=None):
    """Build the storage backend described by environment variables.

    STORAGE_BACKEND     s3 (default) or local
    BUCKET_NAME         required for s3
    AWS_REGION          s3 region, default eu-west-2 (an S3-compatible service's own region name, e.g. gra)
    S3_ENDPOINT_URL     s3 only, optional: an S3-compatible endpoint instead of AWS (boto3's
                        AWS_ENDPOINT_URL_S3 / AWS_ENDPOINT_URL count too, in that order after it)
    S3_ADDRESSING_STYLE s3 only, optional: path, virtual or auto (botocore's default)
    LOCAL_STORAGE_DIR   root directory for local, default ./local_bucket

    Credentials come from boto3's usual chain (AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, profile, role),
    which is also how an S3-compatible service's access keys are supplied.
    """
    env = os.environ if env is None else env
    backend = env.get("STORAGE_BACKEND", "s3").lower()
    if backend == "local":
        return LocalStorage(env.get("LOCAL_STORAGE_DIR", "local_bucket"))
    if backend == "s3":
        bucket = env.get("BUCKET_NAME")
        if not bucket:
            raise RuntimeError("BUCKET_NAME must be set when STORAGE_BACKEND=s3")
        return S3Storage(
            bucket,
            env.get("AWS_REGION", "eu-west-2"),
            endpoint_url=s3_endpoint_from_env(env),
            addressing_style=env.get("S3_ADDRESSING_STYLE"),
        )
    raise RuntimeError(f"Unknown STORAGE_BACKEND {backend!r} (expected 's3' or 'local')")
