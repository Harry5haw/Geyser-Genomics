"""Run records: the local worker's equivalent of a Step Functions execution status.

    runs/<id>.json  {"state": "queued|running|succeeded|failed", "queued": ts, "started": ts,
                     "finished": ts, "exit_code": int, "note": str}
    runs/<id>.log   combined output of scripts/run_local.py for that sample

Only the local worker writes these. In the cloud they don't exist and the UI infers status
from the stage outputs and metrics instead (see status.py).
"""

import io
import json
import re
import time

from geyser_web.pipeline import ObjectNotFound

RUN_PREFIX = "runs/"
SAMPLE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def is_valid_sample_id(sample_id: str) -> bool:
    """Sample IDs become storage keys and Docker arguments, so allow only a safe alphabet.

    No dots: the trigger takes everything before the first dot of the filename as the ID.
    """
    return bool(sample_id) and bool(SAMPLE_ID_RE.match(sample_id))


def run_key(sample_id: str) -> str:
    return f"{RUN_PREFIX}{sample_id}.json"


def log_key(sample_id: str) -> str:
    return f"{RUN_PREFIX}{sample_id}.log"


def put_bytes(storage, key: str, data: bytes, attempts: int = 5) -> None:
    """Write a small object. On Windows a rename onto a file a reader has open fails briefly, so retry."""
    for attempt in range(attempts):
        try:
            storage.upload_stream(io.BytesIO(data), key)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.1)


def read_text(storage, key: str):
    """Return the object as text, or None if it doesn't exist."""
    try:
        with storage.open_read(key) as fh:
            return fh.read().decode("utf-8", errors="replace")
    except ObjectNotFound:
        return None


def read_run(storage, sample_id: str):
    text = read_text(storage, run_key(sample_id))
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def write_run(storage, sample_id: str, record: dict) -> None:
    put_bytes(storage, run_key(sample_id), json.dumps(record, indent=2).encode("utf-8"))


def read_all_runs(storage) -> dict:
    runs = {}
    for key in storage.list_keys(RUN_PREFIX):
        if key.endswith(".json"):
            sample_id = key[len(RUN_PREFIX):-len(".json")]
            record = read_run(storage, sample_id)
            if record is not None:
                runs[sample_id] = record
    return runs
