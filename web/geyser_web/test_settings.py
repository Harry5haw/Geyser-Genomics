"""Settings for the test suite: the real settings with a throwaway secret key.

Tests point storage at a temporary folder per test (see web/tests/conftest.py).
"""

import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-secret-key")

from .settings import *  # noqa: E402,F401,F403

# No collectstatic in tests: plain names (the hashed-asset test runs collectstatic into a temporary folder itself).
STORAGES = {"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}
