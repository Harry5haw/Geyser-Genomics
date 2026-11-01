import importlib.util
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "app"))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
sys.path.insert(0, os.path.join(REPO_ROOT, "app-deepvariant"))


def load_module(name, *path_parts):
    spec = importlib.util.spec_from_file_location(name, os.path.join(REPO_ROOT, *path_parts))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def trigger():
    return load_module("geyser_trigger", "lambda", "trigger", "index.py")


@pytest.fixture
def local_env(tmp_path, monkeypatch):
    """Point tasks.py at a fresh local storage root, with no AWS variables visible."""
    import tasks

    for var in ("BUCKET_NAME", "AWS_REGION", "METRICS_BACKEND", "METRICS_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path / "bucket"))
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setattr(tasks, "_storage", None)
    monkeypatch.setattr(tasks, "_metrics", None)
    return tmp_path / "bucket"
