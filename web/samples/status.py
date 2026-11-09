"""Work out each sample's progress from storage alone.

* A stage is done when all its output keys exist (keys_for/keys_of in app/tasks.py: QC writes
  one report per read file, so two for a paired-end sample).
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
from typing import NamedTuple

from geyser_web.pipeline import (
    ALIGNMENT_FORMS,
    SINGLE,
    STAGE_VCF,
    LayoutError,
    ObjectNotFound,
    keys_for,
    keys_of,
    layout_from_raw,
    legacy_qc_keys,
    output_present,
    parse_raw_key,
    read_record,
    sample_vcf_key,
)

from .runs import is_valid_sample_id, read_all_runs, read_run

METRICS_KEY = "metrics/metrics.jsonl"
RAW_PREFIX = "raw_reads/"
# Metrics older than the current run (minus clock slack between host and container) belong to an earlier run.
CLOCK_SLACK_SECONDS = 30


class Stage(NamedTuple):
    name: str  # CLI name, as in run_local.STAGES and the state machine
    metric: str  # TaskName recorded by @timed_stage in app/tasks.py
    label: str
    output: tuple  # keys_for() names (via keys_of): the stage finished when all keys of any one exist
    after: tuple = ()  # stages whose output this one reads; none means it starts from the raw reads


STAGES = (
    Stage("qc", "QualityControl", "Quality control", ("qc_html",)),
    # The CRAM the align stage writes, or a BAM (runs from before CRAM, fetched benchmark data).
    Stage("align", "Align", "Alignment", ALIGNMENT_FORMS),
    # Reads only the alignment, but the state machine starts it after its Parallel state, i.e. after both.
    Stage("variants", "CallVariants", "Variant calling", ("vcf",), after=("qc", "align")),
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
    duration: float | None = None


@dataclass
class SampleView:
    sample_id: str
    state: str  # queued | running | succeeded | failed
    stages: list
    run: dict | None = None
    updated: float | None = None
    duration: float | None = None
    keys: dict = field(default_factory=dict)
    layout: str = SINGLE
    annotated: bool = False  # the run record has an AlphaGenome Annotate part (opt-in per run)
    deepvariant: bool = False  # DeepVariant (linear or pangenome) ran in this sample's run (its part exists)

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_STATES

    @property
    def state_label(self) -> str:
        if self.state == "running" and self.run is None:
            return "In progress"
        return STATE_LABELS[self.state]

    @property
    def caller(self) -> str:
        """The run's variant caller, from where its calls are: deepvariant, pangenome or bcftools."""
        vcf = self.keys.get("vcf") or ""
        return (
            "pangenome"
            if vcf.startswith("variants_pangenome/")
            else ("deepvariant" if vcf.startswith("variants_deepvariant/") else "bcftools")
        )

    @property
    def tags(self) -> str:
        """Space-separated tags the list's filters match (data-tags): deepvariant, alphagenome, failed."""
        tags = ["deepvariant"] if self.deepvariant or self.caller != "bcftools" else []
        tags += ["alphagenome"] if self.annotated else []
        tags += ["failed"] if self.state == "failed" else []
        return " ".join(tags)

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


def build_sample(
    sample_id: str, present: set, metrics: list, run: dict | None, layout: str = SINGLE, record: dict | None = None
) -> SampleView:
    """record: the provenance run record, when there is one. When DeepVariant was the run's caller (#125) its
    VCF, not bcftools', is the variants stage's output, and its metric (DeepVariant) the stage's time."""
    keys = keys_for(sample_id, layout)
    vcf_key = sample_vcf_key(record, sample_id)
    variant_metric = (record or {}).get("variant_stage") or "CallVariants"
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

    # In the cloud (no worker record) there are no local metrics: a stage's time comes from its run record part.
    recorded = ((record or {}).get("stages") or {}) if run is None else {}
    from_record = False  # some stage's time came from the run record (a cloud run): no sequential total then
    stages = []
    for st in STAGES:
        variants = st.name == "variants" and vcf_key != keys["vcf"]
        metric = variant_metric if variants else st.metric
        rec = latest.get(metric)
        duration = rec.get("duration_seconds") if rec else None
        part = recorded.get(metric) or {}
        if rec is None and part.get("status") == "Success" and part.get("duration_seconds") is not None:
            duration = part["duration_seconds"]
            from_record = True
        if variants:
            done = vcf_key in present
        else:
            done = any(output_present(sample_id, layout, name, present.__contains__) for name in st.output)
        if done:
            state = "done"
            if rec and rec.get("status") != "Success":
                duration = None
        elif rec and rec.get("status") == "Failure":
            state = "failed"
        else:
            state, duration = "pending", None
        stages.append(StageView(st.name, st.label, state, duration))

    run_state = run.get("state") if run else None
    done = {s.name for s in stages if s.state == "done"}
    # Pending stages whose inputs exist: qc and align from the start (they run side by side in the
    # cloud), variants once both are done.
    ready = [
        view
        for view, st in zip(stages, STAGES, strict=True)
        if view.state == "pending" and all(dep in done for dep in st.after)
    ]
    first_pending = ready[0] if ready else None
    # In the cloud there are no local metrics or worker records: a failed run shows only in its run record, which
    # the state machine closes with status "failed" (#155). Without this it would look "in progress" for ever.
    # Only without a worker record: locally, a re-run's record is the previous run's until its first stage writes a
    # part, and the worker's own state (queued/running) is the truth. (A cloud re-run of a sample whose last run
    # failed shows failed until its first stage finishes.)
    # That holds even when every stage's output exists: a step after them (the quality gate in fail mode, the
    # finalising steps) can still fail the run, and the record's close is the run's real outcome.
    record_failed = run is None and (record or {}).get("status") == "failed"
    if all(s.state == "done" for s in stages) and not record_failed:
        state = "succeeded"
    elif any(s.state == "failed" for s in stages) or run_state in ("failed", "succeeded") or record_failed:
        # 'succeeded' with outputs missing means they were removed afterwards; don't claim success.
        state = "failed"
        if not any(s.state == "failed" for s in stages) and first_pending:
            first_pending.state = "failed"  # failed before the stage could record a metric
    elif run_state == "queued":
        state = "queued"
    else:
        state = "running"
        # A local run (run record) goes one stage at a time; in the cloud every ready stage may be running.
        for view in ready[:1] if run else ready:
            view.state = "running"

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
        # Local runs are sequential, so their metric times add up. In the cloud QC and align run side by side, so a sum
        # of the record's stage times would overstate the run: no total there (each stage shows its own time).
        duration=sum(durations) if durations and not from_record else None,
        keys={**keys, "vcf": vcf_key},
        layout=layout,
    )


# Every caller's VCF folder (variants/, variants_deepvariant/, variants_pangenome/).
CALLER_VCF_PREFIXES = {v.split("/", 1)[0] + "/" for v in STAGE_VCF.values()}


def _present_keys(storage) -> set:
    present = set()
    prefixes = {keys_for("x")[name].split("/", 1)[0] + "/" for st in STAGES for name in st.output}
    for prefix in prefixes | CALLER_VCF_PREFIXES:
        present.update(storage.list_keys(prefix))
    return present


def _load_record(storage, sample_id):
    try:
        return read_record(storage, sample_id)
    except Exception:  # noqa: BLE001 - the record only refines the status; never fail the page over it
        return None


# Run record parts that change what the list shows: a stage that can be a run's caller
# (runs/<id>.provenance/<Stage>.json), and Finalize, the part the state machine writes when it closes a failed run.
CALLER_PART_NAMES = tuple(f"/{stage}.json" for stage in STAGE_VCF if stage != "CallVariants")
RECORD_PART_NAMES = (*CALLER_PART_NAMES, "/Finalize.json")


def _samples_with_caller_parts(storage) -> tuple:
    """(samples whose run record changes their row, samples with an Annotate part, samples with a DeepVariant part). The first: a DeepVariant or
    pangenome part (DeepVariant's VCF can be their calls) or a Finalize part (the run failed; the cloud has no other
    sign of it). The second: AlphaGenome-annotated runs, for the list's badge and filter. One LIST of runs/, so the
    list page reads records for those samples only, not one per sample."""
    found, annotated, deepvariant = set(), set(), set()
    for key in storage.list_keys("runs/"):
        if ".provenance/" not in key:
            continue
        sample_id = key[len("runs/") :].split(".provenance/", 1)[0]
        if key.endswith(RECORD_PART_NAMES):
            found.add(sample_id)
            if key.endswith(CALLER_PART_NAMES):
                deepvariant.add(sample_id)
        elif key.endswith("/Annotate.json"):
            annotated.add(sample_id)
    return found, annotated, deepvariant


def load_all(storage) -> list:
    """Every sample, most recently active first."""
    layouts = sample_layouts(storage.list_keys(RAW_PREFIX))
    if not layouts:
        return []
    present, metrics, runs = _present_keys(storage), read_metrics(storage), read_all_runs(storage)
    with_callers, annotated, deepvariant = _samples_with_caller_parts(storage)
    samples = [
        build_sample(i, present, metrics, runs.get(i), layout, _load_record(storage, i) if i in with_callers else None)
        for i, layout in layouts.items()
    ]
    for s in samples:
        s.annotated = s.sample_id in annotated
        s.deepvariant = s.sample_id in deepvariant
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
    candidates = [k for st in STAGES for name in st.output for k in keys_of(keys, name)] + legacy_qc_keys(
        sample_id, layout
    )
    record = _load_record(storage, sample_id)
    candidates.append(sample_vcf_key(record, sample_id))
    present = {k for k in candidates if storage.exists(k)}
    return build_sample(sample_id, present, read_metrics(storage), read_run(storage, sample_id), layout, record)
