# app/provenance.py
"""Provenance run record (#67): what produced a sample's outputs, as one JSON per sample.

    runs/{id}.provenance/{Task}.json   one part per stage (QualityControl, Align, CallVariants, DeepVariant ...)
    runs/{id}.provenance.json          the record: the parts of the sample's latest run, assembled

Each stage writes only its own part, so stages running side by side (qc and align in the cloud)
can't lose each other's updates. The parts are the source of truth: read_record() assembles them
on every read. Each stage also writes the assembled record as a convenience copy for people and
other tools; two stages finishing at once can leave that copy one part behind until the next stage
writes it. The record sits next to runs/{id}.json and runs/{id}.log, which belong to the local web
worker (web/samples/runs.py).

Parts belong to the same run when they share RUN_NAME (run_local.py sets one per run; in the cloud,
the Step Functions execution name once the infrastructure passes it to the jobs). The record is the
latest run; an upstream stage that run didn't include (run_local --skip-to-variants, variants
re-run by hand) comes from the run that last ran it (UPSTREAM), and every stage entry names its run. Without RUN_NAME
every part counts as one run, so after a partial re-run the record can mix runs; "run_name": null
says that this is possible.

Everything here is best effort: a provenance problem prints a warning and never fails a stage.

Record (schema 1):
    schema, sample_id, run_name, status (succeeded | failed | incomplete), complete, updated,
    caller (PIPELINE_CALLER: deepvariant | pangenome when one of them was the run's caller, else absent),
    variant_stage (CallVariants | DeepVariant | PangenomeDeepVariant: the stage whose calls are the sample's),
    vcf (that stage's VCF key: variants/, variants_deepvariant/ or variants_pangenome/<id>.vcf.gz),
    app_version (APP_VERSION: the image tag in the cloud), layout, aligner,
    reference {name, md5, bytes}, inputs [{key, bytes}], outputs [{key, bytes}],
    tools {name: version}, stats {flagstat, mosdepth, bcftools_stats: the parsed headline numbers},
    stages {Task: {status, started, finished, duration_seconds, error, run_name}},
    failure {stage, error, reason, exit_code, job_id, log_stream} (only for a run closed as failed, below)
    gate {verdict (pass | fail | null when the gate couldn't run), reasons, mode, config, gate_version, key}:
        the quality gate's verdict (tasks.gate_task), once the QualityGate stage has run. It doesn't
        change the status: in GATE_MODE=fail a failed verdict fails the QualityGate stage itself.
Parts only: resources {cpu_seconds, peak_memory_mib} (tasks.timed_stage); DeepVariant parts on a GPU also
    gpu {model, sample_seconds, samples, gpus, memory_total_mib, utilization_mean_pct, utilization_peak_pct,
    memory_peak_mib, steps {make_examples | call_variants | postprocess_variants: the same four figures}}
    (deepvariant_task.note_gpu, #139).

Closing a failed run: a stage that fails in Python writes its own Failure part, but a job that is killed (out of
memory, timeout), never starts, or a step outside the stages (the region planner, reading the regions, a shard)
leaves the record without a final status. A closing part, runs/{id}.provenance/Finalize.json, fixes that: the
record is then failed, with the failing stage (from the Batch job name in the Catch's Cause) and why. The state
machine's failure path writes it itself, straight to S3 with the Catch output as "caught" (parsed on read), and
can't re-assemble: the stored runs/{id}.provenance.json keeps the last stage's view until something assembles
it, while every reader that uses read_record() (reconcile, the web UI, the next stage) sees the close. By hand or
for a local run: `tasks.py finalize <id> --status failed --run-name <run> [--caught JSON]` (close_run).
"""

import hashlib
import io
import json
import os
import re
import subprocess
import time
from functools import cache

SCHEMA_VERSION = 1
RUN_PREFIX = "runs/"
# The stages every pipeline run has (metric names, as in tasks.timed_stage). DeepVariant parts are extra.
CORE_STAGES = ("QualityControl", "Align", "CallVariants")
# A run whose caller is DeepVariant (PIPELINE_CALLER: run_local.py sets it for a DeepVariant-only run,
# and the cloud's DeepVariant branch once G2, #125, is applied) has no CallVariants of its own: its
# variant calling is that caller's stage, or a CallVariants started after it (the cloud's bcftools
# fallback when DeepVariant fails), whichever started last (#158). It inherits no CallVariants from an
# earlier run: not its stage, VCF or stats. Without a caller (bcftools runs, --caller both, a caller
# added with --skip-to-variants) the core pipeline is CORE_STAGES, as before.
CALLER_STAGES = {"deepvariant": "DeepVariant", "pangenome": "PangenomeDeepVariant"}
# The VCF each variant-calling stage writes (tasks.keys_for "vcf", deepvariant_task.deepvariant_keys "vcf";
# tests/test_provenance.py checks they agree). The record's "vcf" is the sample's current calls: what
# reconcile and the web UI show, whichever caller made them.
STAGE_VCF = {
    "CallVariants": "variants/{id}.vcf.gz",
    "DeepVariant": "variants_deepvariant/{id}.vcf.gz",
    "PangenomeDeepVariant": "variants_pangenome/{id}.vcf.gz",
}
# Stages whose outputs a stage builds on. When the latest run starts part-way (run_local
# --skip-to-variants, a stage re-run by hand), its upstream stages come from the run that last ran
# them; downstream ones never do, so a run still in progress never borrows an old run's later stages.
# The extra callers read the same alignment; a run of only them (run_local --caller deepvariant
# --skip-to-variants) is an add-on to the sample's core run, so they inherit all three core stages.
UPSTREAM = {
    "CallVariants": ("QualityControl", "Align"),
    "DeepVariant": CORE_STAGES,
    "PangenomeDeepVariant": CORE_STAGES,
    # The quality gate judges the run's calls, whichever caller made them; a gate re-run by hand keeps
    # them in the record. The gate runs last (run_local.py, the state machine), so no run in progress
    # can borrow an older caller stage this way.
    "QualityGate": CORE_STAGES + ("DeepVariant", "PangenomeDeepVariant"),
    # AlphaGenome annotation (3.0) reads the run's calls after the gate: an annotation re-run by hand keeps the
    # run it annotates in the record.
    "Annotate": CORE_STAGES + ("DeepVariant", "PangenomeDeepVariant", "QualityGate"),
}
# name -> (command, regex on its combined output). Run once per process, outside the pipeline's
# subprocess helpers, so a missing or odd tool can't affect a stage.
TOOL_VERSION_COMMANDS = {
    "samtools": (["samtools", "--version"], r"samtools (\S+)"),
    "bcftools": (["bcftools", "--version"], r"bcftools (\S+)"),
    "bwa-mem2": (["bwa-mem2", "version"], r"(?m)^(\d+\.\d+[\w.]*)\s*$"),
    "bwa": (["bwa"], r"Version: (\S+)"),
    "fastp": (["fastp", "--version"], r"fastp (\S+)"),
    "mosdepth": (["mosdepth", "--version"], r"mosdepth (\S+)"),
    "geyser-gate": (["geyser-gate", "--version"], r"geyser-gate (\S+)"),
    # ALIGNER=giraffe (the geyser-giraffe image): vg prints "vg version v1.55.0 ..."; kmc with no
    # arguments prints its usage, headed "K-Mer Counter (KMC) ver. 3.2.4 (...)".
    "vg": (["vg", "version"], r"vg version v?(\S+)"),
    "kmc": (["kmc"], r"KMC\) ver\. (\S+)"),
}
# The image exports the release it pinned where the binary reports something else (bwa-mem2 2.3
# says 2.2.1); recorded as "<tool> release" next to the reported version.
TOOL_RELEASE_ENV = {
    "bwa-mem2": "GEYSER_BWA_MEM2_RELEASE",
    "mosdepth": "GEYSER_MOSDEPTH_RELEASE",
    "vg": "GEYSER_VG_RELEASE",
    "kmc": "GEYSER_KMC_RELEASE",
}

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
    _current = {
        "schema": SCHEMA_VERSION,
        "task": task,
        "sample_id": sample_id,
        "run_name": os.environ.get("RUN_NAME") or None,
        "app_version": os.environ.get("APP_VERSION") or None,
        # Set only for the job of the run's own caller (deepvariant_task.pipeline_caller); see CALLER_STAGES.
        "caller": os.environ.get("PIPELINE_CALLER", "").strip().lower() or None,
        "started": _now(),
        "inputs": [],
        "outputs": [],
        "tools": {},
        "stats": {},
    }


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
    digest = hashlib.md5(usedforsecurity=False)  # identifies the build; FIPS builds reject plain md5()
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


@cache
def tool_version(name: str):
    """The version a tool reports, or None if it isn't installed or its output isn't recognised."""
    if name not in TOOL_VERSION_COMMANDS:
        return None
    command, pattern = TOOL_VERSION_COMMANDS[name]
    # Popen, not subprocess.run: tests replace subprocess.run for the stages' own tool calls.
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError:
        return None
    try:
        out, _ = proc.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()  # a hung --version must not keep running for the rest of the stage
        proc.communicate()
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


def note_tool_as(label: str, name: str) -> None:
    """Record tool `name`'s version under `label`: a second copy of a tool in another image (the DeepVariant
    image's bcftools) must not overwrite the version of the one that did the main work."""
    if _current is not None:
        _current["tools"][label] = tool_version(name)


def _put_json(storage, key, data) -> None:
    storage.upload_stream(io.BytesIO(json.dumps(data, indent=2, sort_keys=True).encode("utf-8")), key)


def _read_json(storage, key):
    from storage import ObjectNotFound  # noqa: PLC0415 - tasks imports this module; keep imports one-way

    try:
        with storage.open_read(key) as fh:
            return json.loads(fh.read().decode("utf-8"))
    except (ObjectNotFound, ValueError):
        return None


def finish(storage, status: str, duration: float, error: str = None, usage: dict = None) -> None:
    """Close the current part: write it, then re-assemble the record. Never raises.

    usage: the stage's resource use (metrics.usage_since), stored as the part's "resources".
    """
    global _current
    part, _current = _current, None
    if part is None:
        return
    part.update(status=status, finished=_now(), duration_seconds=round(duration, 2), error=error, resources=usage)
    try:
        _put_json(storage, f"{part_prefix(part['sample_id'])}{part['task']}.json", part)
        assemble(storage, part["sample_id"])
    except Exception as err:  # noqa: BLE001 - provenance must never fail a stage
        _warn("write the run record", err)


# The state machine's job names start with these (state_machine.asl.json); the stage (metric) name each runs.
# Longest first: CallVariantsShard- also starts with CallVariants.
JOB_NAME_STAGES = (
    ("CallVariantsShard-", "CallVariantsShard"),
    ("CallVariants-", "CallVariants"),
    ("QualityControl-", "QualityControl"),
    ("AlignGenome-", "Align"),
    ("PlanRegions-", "PlanRegions"),
    ("DeepVariant-", "DeepVariant"),  # G2: the GPU caller; an extra caller, so its failure doesn't fail the run
    ("QualityGate-", "QualityGate"),  # #162: the gate after either caller
)
REASON_LIMIT = 1000  # characters of a failure reason kept in the record
EXTRA_CALLERS = ("DeepVariant", "PangenomeDeepVariant")  # their failure never fails the core run
# Stages whose failure never fails the run: the extra callers, and AlphaGenome annotation (3.0, opt-in).
EXTRA_STAGES = EXTRA_CALLERS + ("Annotate",)
FINALIZE_TASK = "Finalize"


def parse_failure(error: str = None, cause: str = None) -> dict:
    """What failed, from a Step Functions Catch's Error and Cause.

    A failed Batch task's Cause is the job's description (JSON): its job name gives the stage, and its status
    reason, exit code and log stream say why and where to look. Any other Cause (a timeout, an S3 error, plain
    text) is kept as the reason, and the stage stays unknown (None).
    """
    failure = {"stage": None, "error": error or None, "reason": None, "exit_code": None, "job_id": None}
    failure["log_stream"] = None
    try:
        job = json.loads(cause) if cause else None
    except ValueError:
        job = None
    if isinstance(job, dict) and job.get("JobName"):
        name = job["JobName"]
        failure["stage"] = next((stage for prefix, stage in JOB_NAME_STAGES if name.startswith(prefix)), None)
        if failure["stage"] == "CallVariantsShard":  # CallVariantsShard-<index>-<execution>: which region failed
            index = name.split("-")[1]
            failure["shard_index"] = int(index) if index.isdigit() else None
        container = job.get("Container") or {}
        failure["reason"] = "; ".join(r for r in (job.get("StatusReason"), container.get("Reason")) if r) or None
        failure["exit_code"] = container.get("ExitCode")
        failure["job_id"] = job.get("JobId")
        failure["log_stream"] = container.get("LogStreamName")
    elif cause:
        failure["reason"] = cause
    if failure["reason"] and len(failure["reason"]) > REASON_LIMIT:
        failure["reason"] = failure["reason"][:REASON_LIMIT] + "..."
    return failure


def _second(timestamp):
    """A timestamp to the second, as the stages write them: the state machine's $$.State.EnteredTime has
    milliseconds ('...:48.429Z'), which would sort before the same second written without them ('...:48Z')."""
    if timestamp and timestamp.endswith("Z") and "." in timestamp:
        return timestamp.split(".")[0] + "Z"
    return timestamp


def _failure_of(final) -> dict:
    """A closing part's failure: parsed by close_run, or (the state machine writes the Catch output as it is)
    parsed here from its "caught" {Error, Cause}."""
    if final.get("failure"):
        return final["failure"]
    caught = final.get("caught")
    if final.get("status") == "failed" and isinstance(caught, dict):
        return parse_failure(caught.get("Error"), caught.get("Cause"))
    return None


def close_run(
    storage, sample_id: str, status: str = "failed", error: str = None, cause: str = None, run_name: str = None
):
    """Close a run with a final status (tasks.py finalize), so a run whose failing job never wrote its own part
    (killed, out of memory, timed out, or a step outside the stages) still ends as failed, with the failing stage
    and why. Writes the Finalize part, then re-assembles the record. Never raises; returns the record (or None).

    run_name: the run to close (default RUN_NAME). Never guessed from the parts: the job that failed has often
    written none, so the latest part can belong to an earlier, successful run.

    The state machine's failure path writes the same part itself, straight to S3 with the Catch output as
    "caught" (no Batch job: a retried job's Cause can be too big for a command line), and build() parses it.
    """
    run_name = run_name or os.environ.get("RUN_NAME") or None
    part = {
        "schema": SCHEMA_VERSION,
        "task": FINALIZE_TASK,
        "sample_id": sample_id,
        "run_name": run_name,
        "app_version": os.environ.get("APP_VERSION") or None,
        "finished": _now(),
        "status": status,
        "failure": parse_failure(error, cause) if status == "failed" else None,
        "inputs": [],
        "outputs": [],
        "tools": {},
        "stats": {},
    }
    try:
        _put_json(storage, f"{part_prefix(sample_id)}{FINALIZE_TASK}.json", part)
        return assemble(storage, sample_id)
    except Exception as err:  # noqa: BLE001 - provenance must never fail the failure path
        _warn("close the run record", err)
        return None


def build(storage, sample_id: str, part_keys=None) -> dict:
    """The record from the parts (see the module docstring), or None if there are none. Doesn't write.

    part_keys: the sample's part keys if the caller already listed them (reconcile), saving a LIST.
    """
    if part_keys is None:
        part_keys = storage.list_keys(part_prefix(sample_id))
    parts = [p for p in (_read_json(storage, k) for k in part_keys if k.endswith(".json")) if p]
    if not parts:
        return None
    for p in parts:
        if p.get("task") == FINALIZE_TASK:
            p["finished"] = _second(p.get("finished"))
    latest = max(parts, key=lambda p: p.get("finished") or "")
    run = [p for p in parts if p.get("run_name") == latest.get("run_name")]
    # Upstream stages the latest run didn't include come from whichever run last ran them (one part per task).
    in_run = {p["task"] for p in run}
    upstream = {u for task in in_run for u in UPSTREAM.get(task, ())} - in_run
    own_caller = run_caller(run)
    if own_caller:  # it called the variants itself: an earlier run's CallVariants isn't this run's
        upstream.discard("CallVariants")
    inherited = [p for p in parts if p["task"] in upstream]
    run += inherited
    caller = own_caller or run_caller(inherited)  # a gate re-run by hand judges the inherited caller's calls
    # The run's closing part (the state machine's failure path, or tasks.py finalize), if it has one. Not a stage.
    final = next((p for p in run if p["task"] == FINALIZE_TASK), None)
    run = [p for p in run if p["task"] != FINALIZE_TASK]
    if final and final.get("run_name") is None and any((p.get("started") or "") > final["finished"] for p in run):
        # Without a run name every part is one run: a stage that *started* after the close is a new run, so the old
        # close doesn't fail it. (A sibling that started before and finished after the close is the same run.)
        final = None
    stages = {
        p["task"]: {
            k: p.get(k) for k in ("status", "started", "finished", "duration_seconds", "error", "run_name", "resources")
        }
        for p in run
    }
    failure = _failure_of(final) if final else None
    if failure and failure.get("stage") and failure["stage"] not in stages:
        # The failing job never wrote its part (killed, out of memory, timed out before Python could record it).
        stages[failure["stage"]] = {
            "status": "Failure",
            "started": None,
            "finished": final.get("finished"),
            "duration_seconds": None,
            "error": failure.get("reason") or failure.get("error"),
            "run_name": final.get("run_name"),
            "resources": None,
        }
    complete, failed = _core_status(stages, caller)
    # A run closed as failed is failed whichever step failed (the planner, a shard, reading the regions, or
    # unknown), unless that step is an extra caller: the same rule as for its own part (_core_status). The run's
    # own caller (PIPELINE_CALLER) is its variant calling, so its failure does fail the run.
    extra = set(EXTRA_STAGES) - {CALLER_STAGES.get(caller)}
    failed = failed or ((final or {}).get("status") == "failed" and (failure or {}).get("stage") not in extra)
    record = {
        "schema": SCHEMA_VERSION,
        "sample_id": sample_id,
        "run_name": latest.get("run_name"),
        "status": "failed" if failed else "succeeded" if complete else "incomplete",
        "complete": complete,
        "updated": _now(),
        # The state machine's closing part has no app_version: the latest part of the run that knows it.
        "app_version": next(
            (
                p["app_version"]
                for p in sorted(run, key=lambda p: p.get("finished") or "", reverse=True)
                if p.get("app_version")
            ),
            latest.get("app_version"),
        ),
        "stages": stages,
        "inputs": _union(p["inputs"] for p in run),
        "outputs": _union(p["outputs"] for p in run),
        "tools": {},
        "stats": {},
    }
    if failure:
        record["failure"] = failure
    for p in sorted(run, key=lambda p: p.get("finished") or ""):  # later stages win on a clash
        record["tools"].update(p.get("tools") or {})
        record["stats"].update(p.get("stats") or {})
        for field in ("reference", "layout", "aligner", "annotation", "gate", "alphagenome"):
            if p.get(field) is not None:
                record[field] = p[field]
    if caller:
        record["caller"] = caller
    record["variant_stage"] = variant_stage(stages, caller) if caller else _calls_stage(stages, parts)
    record["vcf"] = STAGE_VCF[record["variant_stage"]].format(id=sample_id)
    return record


def _calls_stage(stages: dict, parts: list) -> str:
    """The variant stage of a run without a caller of its own: CallVariants when the record has one (its own
    or inherited). An add-on run (run_local --caller pangenome --skip-to-variants) after a run whose caller was
    DeepVariant has none: the sample's calls are still that run's, i.e. the latest successful part of a
    caller's own stage (PIPELINE_CALLER set), whichever run it belongs to.

    Reaching past CallVariants means the sample has no bcftools calls at all (an earlier CallVariants part
    would have been inherited into stages). One part is kept per stage, so a re-run of the same caller as an
    add-on replaces the caller's part and its mark: then the latest successful caller-stage part, marked or
    not, holds the only calls there are."""
    if "CallVariants" in stages:
        return "CallVariants"
    done = [p for p in parts if p["task"] in CALLER_STAGES.values() and p.get("status") == "Success"]
    owned = [p for p in done if p.get("caller") in CALLER_STAGES and p["task"] == CALLER_STAGES[p["caller"]]]
    if not (owned or done):
        return "CallVariants"
    return max(owned or done, key=lambda p: p.get("finished") or "")["task"]


def sample_vcf_key(record, sample_id: str) -> str:
    """The sample's current VCF: the record's "vcf", or the bcftools VCF without a record (or an old record)."""
    vcf = (record or {}).get("vcf")
    return (
        vcf
        if vcf in {v.format(id=sample_id) for v in STAGE_VCF.values()}
        else STAGE_VCF["CallVariants"].format(id=sample_id)
    )


def run_caller(parts: list):
    """The caller these parts name (PIPELINE_CALLER, CALLER_STAGES), or None (none, or more than one)."""
    callers = {p.get("caller") for p in parts if p.get("caller") in CALLER_STAGES}
    return callers.pop() if len(callers) == 1 else None


def variant_stage(stages: dict, caller) -> str:
    """The stage whose calls are the run's: CallVariants without a caller; with one, the later-started of
    the caller's stage and CallVariants (a CallVariants after a failed DeepVariant is the cloud's fallback)."""
    if not caller:
        return "CallVariants"
    attempts = [t for t in (CALLER_STAGES[caller], "CallVariants") if t in stages]
    return max(attempts, key=lambda t: stages[t].get("started") or "") if attempts else CALLER_STAGES[caller]


def _core_status(stages: dict, caller) -> tuple:
    """(complete, failed) of the core pipeline: QualityControl, Align and the variant calling.

    Without a caller the variant calling is CallVariants (CORE_STAGES); a failed extra caller (DeepVariant
    added to a bcftools run) shows in its stage entry but doesn't make the run failed. With one, it is the
    later-started of the caller's stage and CallVariants: a CallVariants after a failed DeepVariant is the
    cloud's fallback, and one before it belongs to an earlier run (which matters only when parts have no
    run name to tell runs apart). Until the fallback finishes, a failed DeepVariant reads as failed.
    """
    core = ("QualityControl", "Align", variant_stage(stages, caller))
    complete = all(stages.get(t, {}).get("status") == "Success" for t in core)
    failed = any(stages[t].get("status") != "Success" for t in core if t in stages)
    return complete, failed


def assemble(storage, sample_id: str) -> dict:
    """build() the record and store it as runs/{id}.provenance.json (the convenience copy)."""
    record = build(storage, sample_id)
    if record is not None:
        _put_json(storage, record_key(sample_id), record)
    return record


def _union(lists) -> list:
    """Entries of several stages' input/output lists, one per key (the last stage's size wins)."""
    by_key = {}
    for entries in lists:
        for entry in entries or []:
            by_key[entry["key"]] = entry
    return [by_key[k] for k in sorted(by_key)]


def read_part(storage, sample_id: str, task: str):
    """One stage's part (the last run of that stage), or None. Each task has exactly one, overwritten per run."""
    return _read_json(storage, f"{part_prefix(sample_id)}{task}.json")


def read_record(storage, sample_id: str, part_keys=None):
    """The sample's run record, assembled from its parts (or the stored copy if there are none), or None."""
    return build(storage, sample_id, part_keys) or _read_json(storage, record_key(sample_id))


def summary(record: dict) -> dict:
    """The short form reconcile and the web UI show: status, run name, per-stage status and run."""
    stages = record.get("stages") or {}
    return {
        "status": record.get("status"),
        "complete": record.get("complete"),
        "run_name": record.get("run_name"),
        "app_version": record.get("app_version"),
        "stages": {task: s.get("status") for task, s in stages.items()},
        "stage_runs": {task: s.get("run_name") for task, s in stages.items()},
        "failed_stage": (record.get("failure") or {}).get("stage"),
        "caller": record.get("caller"),
        "vcf": record.get("vcf"),
    }
