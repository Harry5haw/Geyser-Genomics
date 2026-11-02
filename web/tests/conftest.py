"""Web UI tests: Django test client against a fresh LocalStorage folder per test. No Docker, no AWS."""

import pytest


@pytest.fixture
def storage(tmp_path, settings):
    """Point the app at an empty local storage root and return that backend."""
    from geyser_web.pipeline import get_storage

    settings.GEYSER_STORAGE_BACKEND = "local"
    settings.GEYSER_LOCAL_STORAGE_DIR = tmp_path / "bucket"
    return get_storage()
