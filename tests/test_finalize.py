"""Closing a failed run's record: tasks.py finalize and provenance.close_run (the state machine's failure path)."""

import io
import json
import os

import pytest

import provenance
import tasks
from storage import LocalStorage

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "failures", "batch_job_failed.json")


@pytest.fixture
def store(tmp_path, monkeypatch):
    for var in ("RUN_NAME", "APP_VERSION"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(provenance, "_current", None)
    s = LocalStorage(str(tmp_path / "bucket"))
    monkeypatch.setattr(tasks, "storage", lambda: s)
    return s


def write_part(
    store, task, status="Success", run_name="run-1", finished="2026-11-02T10:00:00Z", started=None, **fields
):
    part = {
        "schema": 1,
        "task": task,
        "sample_id": "S1",
        "run_name": run_name,
        "started": started or finished,
        "finished": finished,
        "status": status,
        "error": None,
        "inputs": [],
        "outputs": [],
        "tools": {},
        "stats": {},
        **fields,
    }
    provenance._put_json(store, f"runs/S1.provenance/{task}.json", part)


def batch_failure():
    """A real failed Batch task's Catch output (CLOUD004, QC on a truncated upload; account and bucket anonymised)."""
    with open(FIXTURE) as fh:
        return json.load(fh)


def stored_record(store):
    with store.open_read("runs/S1.provenance.json") as fh:
        return json.loads(fh.read())


def test_a_batch_cause_gives_the_stage_reason_exit_code_and_log_stream():
    caught = batch_failure()
    failure = provenance.parse_failure(caught["Error"], caught["Cause"])
    assert failure["stage"] == "QualityControl" and failure["error"] == "States.TaskFailed"
    assert failure["reason"] == "Essential container in task exited" and failure["exit_code"] == 1
    assert failure["log_stream"].startswith("geyser/default/") and failure["job_id"]


@pytest.mark.parametrize(
    "job_name, stage",
    [
        ("QualityControl-S1-run", "QualityControl"),
        ("AlignGenome-S1-run", "Align"),
        ("PlanRegions-S1-run", "PlanRegions"),
        ("CallVariantsShard-3-run", "CallVariantsShard"),  # not taken for CallVariants
        ("CallVariants-S1-run", "CallVariants"),
        ("DeepVariant-S1-run", "DeepVariant"),  # G2's GPU caller
        ("SomethingElse-S1", None),
    ],
)
def test_every_state_machine_job_name_maps_to_its_stage(job_name, stage):
    assert provenance.parse_failure("States.TaskFailed", json.dumps({"JobName": job_name}))["stage"] == stage


def test_a_non_batch_cause_is_kept_as_the_reason_and_long_ones_are_cut():
    failure = provenance.parse_failure("States.Timeout", "The execution timed out")
    assert failure["stage"] is None and failure["reason"] == "The execution timed out"
    long = provenance.parse_failure("S3.NoSuchKey", "x" * 5000)
    assert len(long["reason"]) == provenance.REASON_LIMIT + 3 and long["reason"].endswith("...")
    assert provenance.parse_failure(None, None) == {
        "stage": None,
        "error": None,
        "reason": None,
        "exit_code": None,
        "job_id": None,
        "log_stream": None,
    }


def test_a_killed_stage_that_wrote_no_part_closes_the_run_as_failed(store, monkeypatch):
    # QC succeeded; align was killed (out of memory) before Python could write its part: "incomplete" until closed.
    write_part(store, "QualityControl")
    assert provenance.assemble(store, "S1")["status"] == "incomplete"
    monkeypatch.setenv("RUN_NAME", "run-1")
    cause = json.dumps(
        {
            "JobName": "AlignGenome-S1-run-1",
            "StatusReason": "Essential container in task exited",
            "Container": {"ExitCode": 137, "Reason": "OutOfMemoryError: Container killed due to memory usage"},
        }
    )
    record = provenance.close_run(store, "S1", "failed", "States.TaskFailed", cause)
    assert record["status"] == "failed" and not record["complete"]
    assert record["failure"]["stage"] == "Align" and record["failure"]["exit_code"] == 137
    align = record["stages"]["Align"]
    assert align["status"] == "Failure" and "OutOfMemoryError" in align["error"] and align["run_name"] == "run-1"
    assert "Finalize" not in record["stages"]  # the closing part is not a stage
    assert stored_record(store)["status"] == "failed"  # and the stored copy is rewritten


def test_a_stage_that_wrote_its_own_failure_keeps_its_error(store, monkeypatch):
    write_part(store, "QualityControl", status="Failure", error="CalledProcessError: fastp")
    monkeypatch.setenv("RUN_NAME", "run-1")
    caught = batch_failure()
    record = provenance.close_run(store, "S1", "failed", caught["Error"], caught["Cause"])
    assert record["stages"]["QualityControl"]["error"] == "CalledProcessError: fastp"
    assert record["failure"]["stage"] == "QualityControl"


def test_a_failure_outside_the_core_stages_still_fails_the_run(store, monkeypatch):
    # QC and align succeeded; the region planner failed: no core stage failed, but the run did.
    write_part(store, "QualityControl")
    write_part(store, "Align")
    monkeypatch.setenv("RUN_NAME", "run-1")
    record = provenance.close_run(
        store, "S1", "failed", "States.TaskFailed", json.dumps({"JobName": "PlanRegions-S1-run-1"})
    )
    assert record["status"] == "failed" and record["failure"]["stage"] == "PlanRegions"
    assert record["stages"]["PlanRegions"]["status"] == "Failure"
    assert provenance.summary(record)["failed_stage"] == "PlanRegions"


def test_a_later_run_is_not_failed_by_an_earlier_runs_close(store, monkeypatch):
    monkeypatch.setenv("RUN_NAME", "run-1")
    provenance.close_run(store, "S1", "failed", "States.Timeout", "timed out")
    write_part(store, "QualityControl", run_name="run-2", finished="2099-01-01T00:00:00Z")
    record = provenance.build(store, "S1")
    assert record["run_name"] == "run-2" and record["status"] == "incomplete" and "failure" not in record


def test_without_run_name_a_close_counts_only_until_a_stage_starts_again(store):
    write_part(store, "QualityControl", run_name=None, finished="2000-01-01T00:00:00Z")
    assert provenance.close_run(store, "S1", "failed", "States.Timeout", "timed out")["status"] == "failed"
    # A sibling that started before the close and finished after it is the same run: still failed.
    write_part(store, "Align", run_name=None, started="2000-01-01T00:00:01Z", finished="2099-01-01T00:00:00Z")
    assert provenance.build(store, "S1")["status"] == "failed"
    write_part(store, "QualityControl", run_name=None, finished="2099-01-02T00:00:00Z")  # re-run by hand later
    record = provenance.build(store, "S1")
    assert record["status"] == "incomplete" and "failure" not in record


def test_a_by_hand_close_names_its_run_and_never_guesses(store, capsys):
    # run-1 succeeded; run-2's QC was killed before writing any part. Guessing "the latest part's run" would fail
    # run-1, so finalize needs the run: --run-name, or RUN_NAME.
    for task in provenance.CORE_STAGES:
        write_part(store, task, run_name="run-1")
    with pytest.raises(SystemExit):
        tasks.main(["finalize", "S1", "--status", "failed"])
    assert "--run-name" in capsys.readouterr().err
    assert provenance.build(store, "S1")["status"] == "succeeded"  # run-1 untouched
    argv = ["finalize", "S1", "--status", "failed", "--run-name", "run-1", "--error", "States.Timeout"]
    assert tasks.main(argv) == 0  # closing run-1 itself, when that's what's meant, works
    record = provenance.build(store, "S1")
    assert record["run_name"] == "run-1" and set(provenance.CORE_STAGES) <= set(record["stages"])
    assert record["status"] == "failed"


def test_a_closed_runs_app_version_comes_from_its_stages(store):
    write_part(store, "QualityControl", app_version="main-660ec8f")
    provenance._put_json(
        store,
        "runs/S1.provenance/Finalize.json",
        {"task": "Finalize", "run_name": "run-1", "finished": "2026-11-02T11:00:00.123Z", "status": "failed"}
        | {"caught": {"Error": "States.Timeout", "Cause": "timed out"}},
    )
    assert provenance.build(store, "S1")["app_version"] == "main-660ec8f"  # not the closing part's (none)


def test_the_state_machines_raw_closing_part_is_parsed_on_read(store):
    # What Close_Run_Record writes straight to S3: the Catch output as it is, the time with milliseconds.
    write_part(store, "QualityControl", finished="2026-11-02T10:05:00Z")
    caught = batch_failure()
    provenance._put_json(
        store,
        "runs/S1.provenance/Finalize.json",
        {
            "schema": 1,
            "task": "Finalize",
            "sample_id": "S1",
            "run_name": "run-1",
            "finished": "2026-11-02T10:05:00.429Z",
            "status": "failed",
            "caught": caught,
        },
    )
    record = provenance.read_record(store, "S1")
    assert record["status"] == "failed" and record["failure"]["stage"] == "QualityControl"
    assert record["failure"]["exit_code"] == 1 and record["run_name"] == "run-1"
    assert "Finalize" not in record["stages"]


def test_a_closed_failure_of_an_extra_caller_does_not_fail_the_core_run(store):
    for task in provenance.CORE_STAGES:
        write_part(store, task)
    provenance._put_json(
        store,
        "runs/S1.provenance/Finalize.json",
        {"task": "Finalize", "run_name": "run-1", "finished": "2026-11-02T11:00:00Z", "status": "failed"}
        | {"failure": {"stage": "DeepVariant", "error": "States.TaskFailed", "reason": "exit 1"}},
    )
    record = provenance.build(store, "S1")
    assert record["status"] == "succeeded" and record["complete"]
    assert record["stages"]["DeepVariant"]["status"] == "Failure"  # still shown


def test_a_closed_failure_of_the_runs_own_caller_fails_it(store):
    # PIPELINE_CALLER=deepvariant (#166): DeepVariant is this run's variant calling, not an extra caller.
    write_part(store, "QualityControl", caller="deepvariant")
    write_part(store, "Align", caller="deepvariant")
    provenance._put_json(
        store,
        "runs/S1.provenance/Finalize.json",
        {"task": "Finalize", "run_name": "run-1", "finished": "2026-11-02T11:00:00Z", "status": "failed"}
        | {"failure": {"stage": "DeepVariant", "error": "States.TaskFailed", "reason": "exit 1"}},
    )
    record = provenance.build(store, "S1")
    assert record["caller"] == "deepvariant" and record["status"] == "failed"
    assert record["stages"]["DeepVariant"]["status"] == "Failure"


def test_a_failed_shard_keeps_its_index():
    cause = json.dumps({"JobName": "CallVariantsShard-17-S1-20261102T100000Z-abc"})
    failure = provenance.parse_failure("States.TaskFailed", cause)
    assert failure["stage"] == "CallVariantsShard" and failure["shard_index"] == 17


def test_close_run_never_raises(store, monkeypatch, capsys):
    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(provenance, "_put_json", broken)
    assert provenance.close_run(store, "S1", "failed", "E", "C") is None
    assert "could not close the run record" in capsys.readouterr().out


def test_tasks_finalize_closes_the_record_and_always_exits_0(store, monkeypatch, capsys):
    monkeypatch.setenv("RUN_NAME", "run-1")
    caught = batch_failure()
    argv = ["finalize", "S1", "--status", "failed", "--error", caught["Error"], "--cause", caught["Cause"]]
    assert tasks.main(argv) == 0
    assert stored_record(store)["failure"]["stage"] == "QualityControl"
    assert "closed as failed (stage: QualityControl" in capsys.readouterr().out
    monkeypatch.setattr(tasks, "storage", lambda: (_ for _ in ()).throw(RuntimeError("no bucket")))
    assert tasks.main(["finalize", "S1", "--status", "failed"]) == 0  # best effort: the failure path goes on


def test_tasks_finalize_reads_the_whole_catch_output(store, monkeypatch):
    # What the state machine passes: States.JsonToString($.error), i.e. {"Error": ..., "Cause": ...} as one string.
    monkeypatch.setenv("RUN_NAME", "run-1")
    caught = json.dumps(batch_failure())
    assert tasks.main(["finalize", "S1", "--status", "failed", "--caught", caught]) == 0
    failure = stored_record(store)["failure"]
    assert failure["stage"] == "QualityControl" and failure["error"] == "States.TaskFailed"
    # Explicit flags win; a Catch output that isn't a JSON object is kept as the cause.
    assert tasks.main(["finalize", "S1", "--status", "failed", "--caught", caught, "--error", "Custom"]) == 0
    assert stored_record(store)["failure"]["error"] == "Custom"
    for odd in ("not json", "[1, 2]"):
        assert tasks.main(["finalize", "S1", "--status", "failed", "--caught", odd]) == 0
        assert stored_record(store)["failure"]["reason"] == odd


def test_tasks_finalize_needs_a_status():
    with pytest.raises(SystemExit):
        tasks.main(["finalize", "S1"])


# ---- the closing part as Close_Run_Record really stored it (#155): a JSON string holding the JSON ----

DOUBLE_ENCODED = os.path.join(os.path.dirname(__file__), "fixtures", "failures", "finalize_double_encoded.json")


def put_raw(store, key, path):
    """Store a file's bytes unchanged (not through _put_json, which would re-encode them)."""
    with open(path, "rb") as fh:
        store.upload_stream(io.BytesIO(fh.read()), key)


def test_the_stored_double_encoded_close_is_read_as_a_part(store):
    # CLOUD018's Finalize.json as S3 holds it (bucket name anonymised, every other byte kept).
    with open(DOUBLE_ENCODED, encoding="utf-8") as fh:
        assert isinstance(json.load(fh), str)  # the fixture really is the double-encoded form
    put_raw(store, "runs/S1.provenance/Finalize.json", DOUBLE_ENCODED)
    write_part(store, "QualityControl", run_name="CLOUD018-20261103T225329Z-187c52f4", finished="2026-11-03T22:53:35Z")
    assert provenance.read_part(store, "S1", "Finalize")["caught"]["Error"] == "ReferenceNotFound"
    record = provenance.read_record(store, "S1")
    assert record["status"] == "failed" and record["run_name"] == "CLOUD018-20261103T225329Z-187c52f4"
    assert record["failure"]["error"] == "ReferenceNotFound"


def test_a_successful_run_after_a_double_encoded_close_reads_as_succeeded(store):
    # The grid's slots: a run rejected before any job left this close; the next run of the sample must read as
    # succeeded under its own run name (the gate crashed here, failing REAL-HG003-30x-chr20-p1's run, 4 Nov 2026).
    put_raw(store, "runs/S1.provenance/Finalize.json", DOUBLE_ENCODED)
    for task in provenance.CORE_STAGES:
        write_part(store, task, run_name="run-2", finished="2026-11-04T00:03:57Z")
    record = provenance.read_record(store, "S1")
    assert record["status"] == "succeeded" and record["run_name"] == "run-2" and "failure" not in record
    tasks.fetch_gate_inputs("S1", str(store.root) + "-gate")  # what the gate job runs first: no crash


@pytest.mark.parametrize(
    "body",
    [b"[1, 2]", b'"not json inside"', b'{"no_task": true}', b"42", b'"[\\"a list\\"]"'],
)
def test_a_part_that_is_not_a_part_is_skipped_with_a_warning(store, capsys, body):
    store.upload_stream(io.BytesIO(body), "runs/S1.provenance/Odd.json")
    for task in provenance.CORE_STAGES:
        write_part(store, task, run_name="run-1")
    record = provenance.read_record(store, "S1")
    assert record["status"] == "succeeded" and "Odd" not in record["stages"]
    assert "runs/S1.provenance/Odd.json" in capsys.readouterr().out
    assert provenance.read_part(store, "S1", "Odd") is None
