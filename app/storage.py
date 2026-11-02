"""Storage backends for the pipeline.

Stages only ever talk to each other through object keys such as
``raw_reads/<id>.fastq.gz`` or ``variants/<id>.vcf.gz``. This module hides where
those keys live:

* ``S3Storage``    - keys are objects in an S3 bucket (the cloud deployment).
* ``LocalStorage`` - keys are files under a local directory (offline development).

Select one with ``STORAGE_BACKEND=s3|local``. See ``storage_from_env``.
"""

import os
import shutil
from typing import BinaryIO, Iterator


class ObjectNotFound(Exception):
    """Raised when a key does not exist in the backend."""


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

    def download(self, key: str, local_path: str) -> None:
        src = self._path(key)
        if not os.path.isfile(src):
            raise ObjectNotFound(key)
        shutil.copyfile(src, local_path)

    def upload(self, local_path: str, key: str) -> None:
        with open(local_path, "rb") as src:
            self.upload_stream(src, key)

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


_STREAM_CHUNK_BYTES = 64 * 1024 * 1024


class _ForwardOnly:
    """Read-only view of a stream that reports itself as not seekable (see S3Storage.upload_stream)."""

    def __init__(self, stream: BinaryIO):
        self._stream = stream

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False


class S3Storage:
    """Keys are objects in one S3 bucket. boto3 is imported lazily so local runs never need it."""

    def __init__(self, bucket: str, region: str):
        import boto3

        self.bucket = bucket
        self._client = boto3.client("s3", region_name=region)

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

    def download(self, key: str, local_path: str) -> None:
        from botocore.exceptions import ClientError

        try:
            self._client.download_file(self.bucket, key, local_path)
        except ClientError as err:
            if self._is_not_found(err):
                raise ObjectNotFound(key) from err
            raise

    def upload(self, local_path: str, key: str) -> None:
        self._client.upload_file(local_path, self.bucket, key)

    def open_read(self, key: str) -> BinaryIO:
        from botocore.exceptions import ClientError

        try:
            return self._client.get_object(Bucket=self.bucket, Key=key)["Body"]
        except ClientError as err:
            if self._is_not_found(err):
                raise ObjectNotFound(key) from err
            raise

    def upload_stream(self, stream: BinaryIO, key: str) -> None:
        """Upload by reading ``stream`` forward only; it is never seeked.

        s3transfer seeks any stream that reports ``seekable()`` (to measure its size), and
        ``gzip.GzipFile`` reports True even over an S3 ``StreamingBody``; seeking back then
        rewinds the body, which raises ``io.UnsupportedOperation`` (#71). Wrapping the stream
        makes s3transfer buffer it in chunks instead. The size is unknown up front, so the chunk
        size sets the largest possible upload (10,000 parts): 64 MiB allows ~640 GiB, enough for
        a decompressed 30x FASTQ, at up to 10 chunks (640 MiB) in memory.
        """
        from boto3.s3.transfer import TransferConfig

        config = TransferConfig(multipart_chunksize=_STREAM_CHUNK_BYTES)
        self._client.upload_fileobj(_ForwardOnly(stream), self.bucket, key, Config=config)

    def list_keys(self, prefix: str = "") -> Iterator[str]:
        """Yield every key starting with ``prefix`` (S3 returns them in sorted order)."""
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                yield obj["Key"]


def storage_from_env(env=None):
    """Build the storage backend described by environment variables.

    STORAGE_BACKEND   s3 (default) or local
    BUCKET_NAME       required for s3
    AWS_REGION        s3 region, default eu-west-2
    LOCAL_STORAGE_DIR root directory for local, default ./local_bucket
    """
    env = os.environ if env is None else env
    backend = env.get("STORAGE_BACKEND", "s3").lower()
    if backend == "local":
        return LocalStorage(env.get("LOCAL_STORAGE_DIR", "local_bucket"))
    if backend == "s3":
        bucket = env.get("BUCKET_NAME")
        if not bucket:
            raise RuntimeError("BUCKET_NAME must be set when STORAGE_BACKEND=s3")
        return S3Storage(bucket, env.get("AWS_REGION", "eu-west-2"))
    raise RuntimeError(f"Unknown STORAGE_BACKEND {backend!r} (expected 's3' or 'local')")
