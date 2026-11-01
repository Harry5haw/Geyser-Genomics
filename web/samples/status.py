"""Work out each sample's progress from storage alone.

* A stage is done when all its output keys exist (keys_for/keys_of in app/tasks.py: one per
  read file before alignment, so two for a paired-end sample).
* A paired-end sample only appears once both mates are uploaded, as the trigger only starts it then.
* A stage failed when its latest metric (metrics/metrics.jsonl, written by app/metrics.py
  LocalMetrics) says Failure and its output is missing. Durations come from the same file.
* A run record (runs/<id>.json, written by the local worker) adds queued vs running and
  catches failures that never reach a stage, e.g. the Docker image is missing.
* With no run record (the cloud: the S3 upload triggers the Lambda), an unfinished sample
  with no failure is shown as in progress.
"""

import json
from dataclasses import dataclass, field
from typing import NamedTuple, Optional

from geyser_web.pipeline import (SINGLE, LayoutError, ObjectNotFound, keys_for, keys_of, layout_from_raw,
                                 parse_raw_key)

from .runs import is_valid_sample_id, read_all_runs, read_run

METRICS_KEY = "metrics/metrics.jsonl"
RAW_PREFIX = "raw_reads/"
# Metrics older than the current run (minus clock slack between host and container) belong to an earlier run.
CLOCK_SLACK_SECONDS = 30


class Stage(NamedTuple):
    name: str      # CLI name, as in run_local.STAGES and the state machine
    metric: str    # TaskName recorded by @timed_stage in app/tasks.py
    label: str
    output: str    # keys_for() name (via keys_of) whose keys all existing means the stage finished


STAGES = (
    Stage("decompress", "Decompress", "Decompress", "fastq"),
    Stage("qc", "QualityControl", "Quality control", "qc_html"),
    Stage("align", "Align", "Alignment", "bam"),
    Stage("variants", "CallVariants", "Variant calling", "vcf"),
)

ACTIVE_STATES = ("queued", "running")
STATE_LABELS = {
    "queued": "Queued",
    "running": "Running",
    "succeeded": "Complete",
    "failed": "Failed",
}


@dataclass
class StageView:
    name: str
    label: str
    state: str  # done | running | failed | pending
    duration: Optional[float] = None


@dataclass
class SampleView:
    sample_id: str
    state: str  # queued | running | succeeded | failed
    stages: list
    run: Optional[dict] = None
    updated: Optional[float] = None
    duration: Optional[float] = None
    keys: dict = field(default_factory=dict)
    layout: str = SINGLE

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_STATES

    @property
    def state_label(self) -> str:
        if self.state == "running" and self.run is None:
            return "In progress"
        return STATE_LABELS[self.state]

    @property
    def done_count(self) -> int:
        return sum(1 for s in self.stages if s.state == "done")


def sample_layouts(raw_keys) -> dict:
    """{sample_id: 'single'|'paired'} for every complete pipeline input among raw_reads/ keys.

    Uses the trigger's own parsing. Keys that aren't pipeline inputs (wrong name or suffix) are
    ignored, as are samples the pipeline would refuse: a lone mate whose partner hasn't arrived
    yet, or a sample uploaded both single- and paired-end.
    """
    raw_keys = {k for k in raw_keys if k.startswith(RAW_PREFIX)}
    candidates = set()
    for key in raw_keys:
        try:
            sample_id = parse_raw_key(key)[0]
        except ValueError:
            continue
        if is_valid_sample_id(sample_id):
            candidates.add(sample_id)
    layouts = {}
    for sample_id in candidates:
        try:
            layouts[sample_id] = layout_from_raw(sample_id, raw_keys.__contains__)
        except LayoutError:
            continue
    return layouts


def list_sample_ids(storage) -> list:
    return sorted(sample_layouts(storage.list_keys(RAW_PREFIX)))


def read_metrics(storage) -> list:
    """All metric records, oldest first. Missing file (e.g. S3 mode, where metrics go to CloudWatch) -> []."""
    try:
        with storage.open_read(METRICS_KEY) as fh:
            raw = fh.read().decode("utf-8", errors="replace")
    except ObjectNotFound:
        return []
    records = []
    for line in raw.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and {"task", "sample_id", "status"} <= rec.keys():
            records.append(rec)
    return records


def build_sample(sample_id: str, present: set, metrics: list, run: Optional[dict], layout: str = SINGLE) -> SampleView:
    keys = keys_for(sample_id, layout)
    since = None
    if run and run.get("started"):
        since = run["started"] - CLOCK_SLACK_SECONDS

    latest = {}
    for rec in metrics:
        if rec.get("sample_id") != sample_id:
            continue
        ts = rec.get("timestamp") or 0
        if since is not None and ts < since:
            continue
        task = rec["task"]
        if task not in latest or ts >= (latest[task].get("timestamp") or 0):
            latest[task] = rec

    stages = []
    for st in STAGES:
        rec = latest.get(st.metric)
        duration = rec.get("duration_seconds") if rec else None
        if all(k in present for k in keys_of(keys, st.output)):
            state = "done"
            if rec and rec.get("status") != "Success":
                duration = None
        elif rec and rec.get("status") == "Failure":
            state = "failed"
        else:
            state, duration = "pending", None
        stages.append(StageView(st.name, st.label, state, duration))

    run_state = run.get("state") if run else None
    first_pending = next((s for s in stages if s.state == "pending"), None)
    if all(s.state == "done" for s in stages):
        state = "succeeded"
    elif any(s.state == "failed" for s in stages) or run_state in ("failed", "succeeded"):
        # 'succeeded' with outputs missing means they were removed afterwards; don't claim success.
        state = "failed"
        if not any(s.state == "failed" for s in stages) and first_pending:
            first_pending.state = "failed"  # failed before the stage could record a metric
    elif run_state == "queued":
        state = "queued"
    else:
        state = "running"
        if first_pending:
            first_pending.state = "running"

    times = [rec.get("timestamp") or 0 for rec in latest.values()]
    if run:
        times += [run.get(k) or 0 for k in ("queued", "started", "finished")]
    durations = [s.duration for s in stages if s.duration is not None]
    return SampleView(
        sample_id=sample_id,
        state=state,
        stages=stages,
        run=run,
        updated=max(times) if any(times) else None,
        duration=sum(durations) if durations else None,
        keys=keys,
        layout=layout,
    )


def _present_keys(storage) -> set:
    present = set()
    for prefix in {keys_for("x")[st.output].split("/", 1)[0] + "/" for st in STAGES}:
        present.update(storage.list_keys(prefix))
    return present


def load_all(storage) -> list:
    """Every sample, most recently active first."""
    layouts = sample_layouts(storage.list_keys(RAW_PREFIX))
    if not layouts:
        return []
    present, metrics, runs = _present_keys(storage), read_metrics(storage), read_all_runs(storage)
    samples = [build_sample(i, present, metrics, runs.get(i), layout) for i, layout in layouts.items()]
    samples.sort(key=lambda s: (s.updated or 0, s.sample_id), reverse=True)
    return samples


def load_one(storage, sample_id: str):
    """One sample, or None if it was never uploaded."""
    if not is_valid_sample_id(sample_id):
        return None
    try:
        layout = layout_from_raw(sample_id, storage.exists)
    except LayoutError:
        return None  # never uploaded, or not a complete sample yet
    keys = keys_for(sample_id, layout)
    present = {k for st in STAGES for k in keys_of(keys, st.output) if storage.exists(k)}
    return build_sample(sample_id, present, read_metrics(storage), read_run(storage, sample_id), layout)
