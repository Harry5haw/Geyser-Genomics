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
