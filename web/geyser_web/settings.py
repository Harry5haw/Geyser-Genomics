"""Settings for the Geyser Genomics web UI.

Everything comes from environment variables. Safe defaults: DEBUG is off unless
DJANGO_DEBUG=1, and with DEBUG off a real DJANGO_SECRET_KEY is required.

There is no database: every piece of state (uploads, results, run status, metrics)
lives in pipeline storage, so the UI behaves the same against a local folder or S3.
"""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent  # web/
REPO_ROOT = BASE_DIR.parent


def _env_bool(name, default=False):
    return os.environ.get(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


DEBUG = _env_bool("DJANGO_DEBUG")

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("Set DJANGO_SECRET_KEY (or DJANGO_DEBUG=1 for local development).")
    SECRET_KEY = "insecure-dev-only-key"  # noqa: S105 - only reachable with DEBUG on

ALLOWED_HOSTS = [
    h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()
]

INSTALLED_APPS = [
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "samples",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "geyser_web.urls"
WSGI_APPLICATION = "geyser_web.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.messages.context_processors.messages",
                "samples.context_processors.geyser",
            ]
        },
    }
]

DATABASES = {}
MESSAGE_STORAGE = "django.contrib.messages.storage.cookie.CookieStorage"

STATIC_URL = "static/"
STATIC_ROOT = os.environ.get("DJANGO_STATIC_ROOT") or None  # only needed for collectstatic
WHITENOISE_USE_FINDERS = True  # serve app static files without a collectstatic step (development)
# With a STATIC_ROOT (set it, then run collectstatic once per release; dev.py --production does both):
# content-hashed names (geyser.<hash>.js), precompressed .gz (and .br with the brotli package) copies, and
# WhiteNoise serves the hashed files with "Cache-Control: max-age=315360000, public, immutable". Without one,
# plain names straight from the app folders, so nothing needs collecting (development, a quick DEBUG-off run).
STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
        if STATIC_ROOT
        else "django.contrib.staticfiles.storage.StaticFilesStorage"
    },
}

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "Europe/London"
USE_I18N = False
USE_TZ = True

# --- Security ---------------------------------------------------------------
CSRF_COOKIE_HTTPONLY = True
X_FRAME_OPTIONS = "DENY"  # the QC (fastp) report view relaxes this to SAMEORIGIN for its own iframe
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
if _env_bool("DJANGO_HTTPS"):  # set when served over TLS (not for plain-http local runs)
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Uploaded files above this size are spooled to disk rather than held in memory.
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024

# --- Geyser -----------------------------------------------------------------
# Same variable names as the pipeline (app/storage.py), but the UI defaults to local storage.
GEYSER_STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND", "local").lower()
GEYSER_LOCAL_STORAGE_DIR = Path(os.environ.get("LOCAL_STORAGE_DIR", REPO_ROOT / "local_bucket")).resolve()
GEYSER_BUCKET_NAME = os.environ.get("BUCKET_NAME", "")
GEYSER_AWS_REGION = os.environ.get("AWS_REGION", "eu-west-2")
# Same order as storage.S3_ENDPOINT_VARS (boto3 obeys its own two variables either way).
GEYSER_S3_ENDPOINT_URL = next(
    (
        v.strip()
        for v in map(os.environ.get, ("S3_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3", "AWS_ENDPOINT_URL"))
        if v and v.strip()
    ),
    "",
)
GEYSER_S3_ADDRESSING_STYLE = os.environ.get("S3_ADDRESSING_STYLE", "")

GEYSER_MAX_UPLOAD_MB = int(os.environ.get("GEYSER_MAX_UPLOAD_MB", "200"))
# Rows per page of the variant and AlphaGenome tables (the first page is inline; the rest are prerenderable links).
GEYSER_TABLE_ROWS = int(os.environ.get("GEYSER_TABLE_ROWS", "50"))
# The signature figures' derived CSVs (#189), drawn as 3D terrains on /figures/ (W1b).
GEYSER_FIGURES_DIR = Path(os.environ.get("GEYSER_FIGURES_DIR", REPO_ROOT / "docs" / "figures" / "csv"))
GEYSER_POLL_SECONDS = int(os.environ.get("GEYSER_POLL_SECONDS", "3"))

# Local worker (python web/manage.py runworker): the offline stand-in for the S3 -> Lambda trigger.
GEYSER_RUNNER = os.environ.get("GEYSER_RUNNER", "docker")  # docker | host (passed to run_local.py)
GEYSER_IMAGE = os.environ.get("GEYSER_IMAGE", "geyser-app")
GEYSER_REFERENCE_NAME = os.environ.get("REFERENCE_NAME", "synthetic_ref.fa")
