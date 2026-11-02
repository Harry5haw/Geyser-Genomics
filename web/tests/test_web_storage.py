"""The web UI builds its storage from settings: an S3-compatible endpoint has to reach it too (#80)."""

import pytest


@pytest.fixture(autouse=True)
def no_ambient_aws(monkeypatch, tmp_path):
    """As in tests/test_storage.py: no developer AWS endpoints, profile or credentials; a fresh boto3
    session; and a fresh storage cache, so each test builds its own client."""
    import boto3

    from geyser_web.pipeline import _build_storage
    from storage import S3_ENDPOINT_VARS

    for var in (*S3_ENDPOINT_VARS, "AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setattr(boto3, "DEFAULT_SESSION", None)
    _build_storage.cache_clear()
    yield
    _build_storage.cache_clear()


def test_web_storage_passes_the_s3_endpoint_through(settings):
    from geyser_web.pipeline import get_storage

    settings.GEYSER_STORAGE_BACKEND = "s3"
    settings.GEYSER_BUCKET_NAME = "bucket"
    settings.GEYSER_AWS_REGION = "gra"
    settings.GEYSER_S3_ENDPOINT_URL = "https://s3.gra.io.cloud.ovh.net"
    settings.GEYSER_S3_ADDRESSING_STYLE = "path"
    s = get_storage()
    assert s._client.meta.endpoint_url == "https://s3.gra.io.cloud.ovh.net"
    assert s._client.meta.config.s3["addressing_style"] == "path"


def test_web_storage_on_aws_by_default(settings):
    from geyser_web.pipeline import get_storage

    settings.GEYSER_STORAGE_BACKEND = "s3"
    settings.GEYSER_BUCKET_NAME = "bucket"
    settings.GEYSER_AWS_REGION = "eu-west-2"
    settings.GEYSER_S3_ENDPOINT_URL = ""
    settings.GEYSER_S3_ADDRESSING_STYLE = ""
    assert get_storage()._client.meta.endpoint_url == "https://s3.eu-west-2.amazonaws.com"
