"""Settings are checked in a subprocess so each case gets a clean environment and a fresh import."""
import os
import subprocess
import sys

WEB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_settings(extra_env):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DJANGO_", "GEYSER_"))}
    env.update(extra_env)
    code = "import geyser_web.settings as s; print(s.DEBUG, s.ALLOWED_HOSTS, 'CsrfViewMiddleware' in str(s.MIDDLEWARE))"
    return subprocess.run([sys.executable, "-c", code], cwd=WEB_DIR, env=env, capture_output=True, text=True)


def test_debug_is_off_by_default_and_csrf_on():
    result = load_settings({"DJANGO_SECRET_KEY": "x"})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False ['localhost', '127.0.0.1'] True"


def test_secret_key_required_without_debug():
    result = load_settings({})
    assert result.returncode != 0
    assert "DJANGO_SECRET_KEY" in result.stderr


def test_debug_opt_in_works_without_secret():
    result = load_settings({"DJANGO_DEBUG": "1"})
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("True")


def test_s3_endpoint_follows_the_pipelines_variable_order():
    # #80: same order as storage.S3_ENDPOINT_VARS, so the UI applies the S3-compatible settings whenever
    # boto3 would send it somewhere other than AWS.
    code = "import geyser_web.settings as s; print(repr(s.GEYSER_S3_ENDPOINT_URL))"

    def endpoint(extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("DJANGO_", "GEYSER_", "S3_", "AWS_ENDPOINT_URL"))}
        env.update({"DJANGO_SECRET_KEY": "x", **extra})
        result = subprocess.run([sys.executable, "-c", code], cwd=WEB_DIR, env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    assert endpoint({}) == "''"
    assert endpoint({"AWS_ENDPOINT_URL": "http://y"}) == "'http://y'"
    assert endpoint({"AWS_ENDPOINT_URL_S3": " http://x ", "AWS_ENDPOINT_URL": "http://y"}) == "'http://x'"
    assert endpoint({"S3_ENDPOINT_URL": "https://s3.gra.io.cloud.ovh.net", "AWS_ENDPOINT_URL_S3": "http://x"}) \
        == "'https://s3.gra.io.cloud.ovh.net'"
