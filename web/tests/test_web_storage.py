"""The web UI builds its storage from settings: an S3-compatible endpoint has to reach it too (#80)."""
import pytest


@pytest.fixture(autouse=True)
def no_ambient_aws_endpoint(monkeypatch, tmp_path):
    for var in ("AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3", "AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-aws-config"))
    # boto3 caches one default session (and the config it read) for the process: use a fresh one
    # here, and monkeypatch puts the old one back afterwards.
    import boto3

    monkeypatch.setattr(boto3, "DEFAULT_SESSION", None)


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
