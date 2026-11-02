# app/provenance.py
"""Provenance run record (#67): what produced a sample's outputs, as one JSON per sample.

    runs/{id}.provenance/{Task}.json   one part per stage (QualityControl, Align, CallVariants, DeepVariant ...)
    runs/{id}.provenance.json          the record: the parts of the sample's latest run, assembled

Each stage writes its own part and then re-assembles the record. qc and align run side by side in
the cloud, so a single shared object would lose one of their updates; the variants stage runs after
both, so its assembly is the complete one. The record sits next to runs/{id}.json and runs/{id}.log,
which belong to the local web worker (web/samples/runs.py).

Parts belong to the same run when they share RUN_NAME (run_local.py sets one per run; in the cloud,
the Step Functions execution name once the infrastructure passes it to the jobs). Without RUN_NAME
every part counts as one run, so after a partial re-run the record can mix runs; "run_name": null
says that this is possible.

Everything here is best effort: a provenance problem prints a warning and never fails a stage.

Record (schema 1):
    schema, sample_id, run_name, status (succeeded | failed | incomplete), complete, updated,
    app_version (APP_VERSION: the image tag in the cloud), layout, aligner,
    reference {name, md5, bytes}, inputs [{key, bytes}], outputs [{key, bytes}],
    tools {name: version}, stats {flagstat, mosdepth, bcftools_stats: the parsed headline numbers},
    stages {Task: {status, started, finished, duration_seconds, error}}
"""

import hashlib
import io
import json
import os
import re
import subprocess
import time
from functools import lru_cache

SCHEMA_VERSION = 1
RUN_PREFIX = "runs/"
# The stages every pipeline run has (metric names, as in tasks.timed_stage). DeepVariant parts are extra.
CORE_STAGES = ("QualityControl", "Align", "CallVariants")
# name -> (command, regex on its combined output). Run once per process, outside the pipeline's
# subprocess helpers, so a missing or odd tool can't affect a stage.
TOOL_VERSION_COMMANDS = {
    "samtools": (["samtools", "--version"], r"samtools (\S+)"),
    "bcftools": (["bcftools", "--version"], r"bcftools (\S+)"),
    "bwa-mem2": (["bwa-mem2", "version"], r"(?m)^(\d+\.\d+[\w.]*)\s*$"),
    "bwa": (["bwa"], r"Version: (\S+)"),
    "fastqc": (["fastqc", "--version"], r"FastQC v(\S+)"),
    "mosdepth": (["mosdepth", "--version"], r"mosdepth (\S+)"),
}
# The image exports the release it pinned where the binary reports something else (bwa-mem2 2.3
# says 2.2.1); recorded as "<tool> release" next to the reported version.
TOOL_RELEASE_ENV = {"bwa-mem2": "GEYSER_BWA_MEM2_RELEASE", "mosdepth": "GEYSER_MOSDEPTH_RELEASE"}

_current = None  # the part being built for the stage running in this process (one stage per process)


def record_key(sample_id: str) -> str:
    return f"{RUN_PREFIX}{sample_id}.provenance.json"


def part_prefix(sample_id: str) -> str:
    return f"{RUN_PREFIX}{sample_id}.provenance/"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _warn(what, err):
    print(f"WARNING: provenance: could not {what}: {err}", flush=True)


def begin(task: str, sample_id: str) -> None:
    """Start the part for one stage. Called by tasks.timed_stage."""
    global _current
    _current = {"schema": SCHEMA_VERSION, "task": task, "sample_id": sample_id,
                "run_name": os.environ.get("RUN_NAME") or None,
                "app_version": os.environ.get("APP_VERSION") or None,
                "started": _now(), "inputs": [], "outputs": [], "tools": {}, "stats": {}}


def note(**fields) -> None:
    """Add plain fields to the current part (e.g. aligner=..., layout=...)."""
    if _current is not None:
        _current.update(fields)


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def note_input(key: str, local_path: str = None) -> None:
    if _current is not None:
        _current["inputs"].append({"key": key, "bytes": _size(local_path) if local_path else None})


def note_output(key: str, local_path: str = None) -> None:
    if _current is not None:
        _current["outputs"].append({"key": key, "bytes": _size(local_path) if local_path else None})


def note_stats(name: str, values: dict) -> None:
    if _current is not None:
        _current["stats"][name] = values


def file_md5(path: str) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def note_reference(name: str, local_path: str) -> None:
    """The FASTA's name, size and MD5 (seconds even for a whole genome; it identifies the exact build)."""
    if _current is None:
        return
    try:
        _current["reference"] = {"name": name, "md5": file_md5(local_path), "bytes": _size(local_path)}
    except OSError as err:
        _warn("hash the reference", err)


@lru_cache(maxsize=None)
def tool_version(name: str):
    """The version a tool reports, or None if it isn't installed or its output isn't recognised."""
    command, pattern = TOOL_VERSION_COMMANDS[name]
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _ = proc.communicate(timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(pattern, out.decode("utf-8", errors="replace"))
    return match.group(1) if match else None


def note_tools(*names: str) -> None:
    if _current is None:
        return
    for name in names:
        _current["tools"][name] = tool_version(name)
        release = os.environ.get(TOOL_RELEASE_ENV.get(name, ""), "")
        if release:
            _current["tools"][f"{name} release"] = release


def _put_json(storage, key, data) -> None:
    storage.upload_stream(io.BytesIO(json.dumps(data, indent=2, sort_keys=True).encode("utf-8")), key)


def _read_json(storage, key):
    from storage import ObjectNotFound  # noqa: PLC0415 - tasks imports this module; keep imports one-way
    try:
        with storage.open_read(key) as fh:
            return json.loads(fh.read().decode("utf-8"))
    except (ObjectNotFound, ValueError):
        return None


def finish(storage, status: str, duration: float, error: str = None) -> None:
    """Close the current part: write it, then re-assemble the record. Never raises."""
    global _current
    part, _current = _current, None
    if part is None:
        return
    part.update(status=status, finished=_now(), duration_seconds=round(duration, 2), error=error)
    try:
        _put_json(storage, f"{part_prefix(part['sample_id'])}{part['task']}.json", part)
        assemble(storage, part["sample_id"])
    except Exception as err:  # noqa: BLE001 - provenance must never fail a stage
        _warn("write the run record", err)


def assemble(storage, sample_id: str) -> dict:
    """Build runs/{id}.provenance.json from the parts of the latest run, write it and return it."""
    parts = [p for p in (_read_json(storage, k) for k in storage.list_keys(part_prefix(sample_id))
                         if k.endswith(".json")) if p]
    if not parts:
        return None
    latest = max(parts, key=lambda p: p.get("finished") or "")
    run = [p for p in parts if p.get("run_name") == latest.get("run_name")]
    stages = {p["task"]: {k: p.get(k) for k in ("status", "started", "finished", "duration_seconds", "error")}
              for p in run}
    complete = all(stages.get(t, {}).get("status") == "Success" for t in CORE_STAGES)
    failed = any(s["status"] != "Success" for s in stages.values())
    record = {
        "schema": SCHEMA_VERSION, "sample_id": sample_id, "run_name": latest.get("run_name"),
        "status": "failed" if failed else "succeeded" if complete else "incomplete", "complete": complete,
        "updated": _now(), "app_version": latest.get("app_version"), "stages": stages,
        "inputs": _union(p["inputs"] for p in run), "outputs": _union(p["outputs"] for p in run),
        "tools": {}, "stats": {},
    }
    for p in sorted(run, key=lambda p: p.get("finished") or ""):  # later stages win on a clash
        record["tools"].update(p.get("tools") or {})
        record["stats"].update(p.get("stats") or {})
        for field in ("reference", "layout", "aligner"):
            if p.get(field) is not None:
                record[field] = p[field]
    _put_json(storage, record_key(sample_id), record)
    return record


def _union(lists) -> list:
    """Entries of several stages' input/output lists, one per key (the last stage's size wins)."""
    by_key = {}
    for entries in lists:
        for entry in entries or []:
            by_key[entry["key"]] = entry
    return [by_key[k] for k in sorted(by_key)]


def read_record(storage, sample_id: str):
    """The sample's run record, or None if no stage has written one."""
    return _read_json(storage, record_key(sample_id))


def summary(record: dict) -> dict:
    """The short form reconcile and the web UI show: status, run name, per-stage status."""
    return {"status": record.get("status"), "complete": record.get("complete"), "run_name": record.get("run_name"),
            "app_version": record.get("app_version"),
            "stages": {task: s.get("status") for task, s in (record.get("stages") or {}).items()}}
