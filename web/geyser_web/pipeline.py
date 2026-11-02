"""The one bridge between the web UI and the pipeline code.

The UI reuses the pipeline's own modules rather than copying them:
  * app/storage.py            - the storage layer (local folder or S3)
  * app/tasks.py              - keys_for()/keys_of(), the single source of truth for storage keys,
                                and layout_from_raw() (single- vs paired-end)
  * lambda/trigger/index.py   - parse_sample_id()/parse_raw_key(), exactly what the cloud trigger uses
  * scripts/make_test_data.py - synthetic demo data
  * scripts/run_local.py      - the local stand-in for Lambda + Step Functions + Batch

Importing tasks.py is side-effect free: its storage and metrics backends are created lazily.
"""

import importlib.util
import os
import sys
from functools import lru_cache

from django.conf import settings

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _sub in ("app", "scripts"):
    _path = os.path.join(REPO_ROOT, _sub)
    if _path not in sys.path:
        sys.path.insert(0, _path)

import make_test_data  # noqa: E402
import run_local  # noqa: E402
from storage import LocalStorage, ObjectNotFound, storage_from_env  # noqa: E402
from tasks import (MATES, PAIRED, PER_MATE_KEYS, SINGLE, LayoutError, all_raw_keys, keys_for, keys_of,  # noqa: E402
                   layout_from_raw, legacy_qc_keys, output_present)

RUN_LOCAL_PATH = os.path.join(REPO_ROOT, "scripts", "run_local.py")


def _load_trigger():
    # 'lambda' is a Python keyword, so load by path (as run_local.load_trigger does).
    path = os.path.join(REPO_ROOT, "lambda", "trigger", "index.py")
    spec = importlib.util.spec_from_file_location("geyser_trigger", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_trigger = _load_trigger()
parse_sample_id = _trigger.parse_sample_id
parse_raw_key = _trigger.parse_raw_key


@lru_cache(maxsize=4)
def _build_storage(env_items):
    return storage_from_env(dict(env_items))


def get_storage():
    """The storage backend described by settings, using the pipeline's variable names so every
    storage_from_env option reaches the UI. Cached per configuration, so tests that override the
    settings get a fresh backend automatically."""
    env = {
        "STORAGE_BACKEND": settings.GEYSER_STORAGE_BACKEND,
        "LOCAL_STORAGE_DIR": str(settings.GEYSER_LOCAL_STORAGE_DIR),
        "BUCKET_NAME": settings.GEYSER_BUCKET_NAME or "",
        "AWS_REGION": settings.GEYSER_AWS_REGION,
        "S3_ENDPOINT_URL": settings.GEYSER_S3_ENDPOINT_URL or "",
        "S3_ADDRESSING_STYLE": settings.GEYSER_S3_ADDRESSING_STYLE or "",
    }
    return _build_storage(tuple(sorted(env.items())))


__all__ = [
    "LayoutError", "LocalStorage", "all_raw_keys", "MATES", "ObjectNotFound", "PAIRED", "PER_MATE_KEYS", "RUN_LOCAL_PATH",
    "REPO_ROOT", "SINGLE", "get_storage", "keys_for", "keys_of", "layout_from_raw", "legacy_qc_keys", "output_present", "make_test_data",
    "parse_raw_key", "parse_sample_id", "run_local",
]
