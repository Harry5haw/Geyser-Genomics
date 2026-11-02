"""The quality gate stage (tasks.gate_task, #144): inputs fetched, binary run, verdict stored and recorded.

The geyser-gate binary itself is tested in gate/ (cargo test); here it is faked, so these tests run
anywhere. The Docker end-to-end test runs the real binary in the image.
"""

import json
import os
import subprocess

import pytest

import provenance
import tasks

STATS = os.path.join(os.path.dirname(__file__), "fixtures", "stats")


def put(root, key, data=b"x"):
    path = root.joinpath(*key.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())


def write_part(root, task, run_name="S1-run", finished="2026-11-02T10:00:00Z", **fields):
    part = {
        "schema": 1,
        "task": task,
        "sample_id": "S1",
        "run_name": run_name,
        "started": finished,
        "finished": finished,
        "duration_seconds": 1.0,
        "status": "Success",
        "error": None,
        "inputs": [],
        "outputs": [],
        "tools": {},
        "stats": {},
        **fields,
    }
    put(root, f"runs/S1.provenance/{task}.json", json.dumps(part))


@pytest.fixture
def sample(local_env, monkeypatch):
    """S1 after a successful core run: stats, a fastp report, and the three stage parts."""
    for var in ("GATE_MODE", "GATE_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(provenance, "_current", None)
    keys = tasks.keys_for("S1")
    for name, fixture in (
        ("flagstat", "single.flagstat.txt"),
        ("mosdepth_summary", "single.mosdepth.summary.txt"),
        ("mosdepth_dist", "single.mosdepth.global.dist.txt"),
        ("bcftools_stats", "single.bcftools.stats.txt"),
    ):
        with open(os.path.join(STATS, fixture), "rb") as fh:
            put(local_env, keys[name], fh.read())
    put(local_env, keys["qc_json"], '{"summary": {}}')
    for i, task in enumerate(provenance.CORE_STAGES):
        write_part(local_env, task, finished=f"2026-11-02T10:0{i}:00Z", layout="single")
    return local_env


@pytest.fixture
def fake_gate(monkeypatch):
    """Replace the geyser-gate binary: records each call and its --root contents, writes `verdict`."""
    state = {"calls": [], "verdict": "pass", "returncode": None, "raise": None, "write": True}

    def run(cmd, capture_output=False, text=False, timeout=None, **kwargs):
        if cmd[0] != tasks.GATE_BINARY:
            raise AssertionError(f"unexpected command {cmd}")
        if state["raise"]:
            raise state["raise"]
        args = dict(zip(cmd[1::2], cmd[2::2], strict=False))
        root = args["--root"]
        files = sorted(
            os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/") for d, _, fs in os.walk(root) for f in fs
        )
        with open(os.path.join(root, "runs", "S1.provenance.json")) as fh:
            record = json.load(fh)
        state["calls"].append({"cmd": cmd, "files": files, "record": record, "timeout": timeout})
        failed = state["verdict"] == "fail"
        if state["write"]:
            with open(args["--output"], "w") as fh:
                json.dump(
                    {
                        "schema": 1,
                        "gate_version": "0.1.0",
                        "sample_id": "S1",
                        "verdict": state["verdict"],
                        "reasons": ["tstv 1.5 is below the minimum 1.7"] if failed else [],
                        "config": args.get("--config", "built-in default"),
                    },
                    fh,
                )
        code = state["returncode"] if state["returncode"] is not None else (1 if failed else 0)
        return subprocess.CompletedProcess(cmd, code, stdout="", stderr="geyser-gate: S1: summary")

    monkeypatch.setattr(tasks.subprocess, "run", run)
    return state


def record(root):
    return provenance.read_record(tasks.storage(), "S1")


def test_the_gate_reads_this_runs_files_and_stores_its_verdict(sample, fake_gate):
    tasks.gate_task("S1")

    (call,) = fake_gate["calls"]
    assert call["cmd"][:2] == ["geyser-gate", "--root"]
    assert call["cmd"][3:6] == ["--sample", "S1", "--output"]
    assert "--config" not in call["cmd"], "no GATE_CONFIG: the built-in thresholds"
    assert call["timeout"] == tasks.GATE_TIMEOUT_SECONDS
    # Laid out like the bucket, as the binary expects.
    assert call["files"] == [
        "qc_reports/S1.fastp.json",
        "runs/S1.provenance.json",
        "stats/S1/bcftools.stats.txt",
        "stats/S1/flagstat.txt",
        "stats/S1/mosdepth.global.dist.txt",
        "stats/S1/mosdepth.summary.txt",
    ]
    assert (sample / "qc_reports" / "S1.gate.json").exists()

    r = record(sample)
    assert r["gate"]["verdict"] == "pass" and r["gate"]["mode"] == "warn"
    assert r["gate"]["key"] == "qc_reports/S1.gate.json"
    assert r["stages"]["QualityGate"]["status"] == "Success"
    assert r["status"] == "succeeded", "the gate doesn't change the core pipeline's status"
    assert {"key": "qc_reports/S1.gate.json"}.items() <= next(
        o for o in r["outputs"] if o["key"] == "qc_reports/S1.gate.json"
    ).items()
    assert not any((sample.parent / "work").iterdir())


def test_the_record_is_assembled_from_the_parts_not_the_stored_copy(sample, fake_gate):
    # A stored copy left behind by a failed re-assembly says "failed"; the parts say the run succeeded.
    put(sample, "runs/S1.provenance.json", json.dumps({"schema": 1, "sample_id": "S1", "status": "failed"}))
    tasks.gate_task("S1")
    given = fake_gate["calls"][0]["record"]
    assert given["status"] == "succeeded"
    assert set(given["stages"]) == set(provenance.CORE_STAGES)


def test_missing_inputs_are_left_for_the_gate_to_report(sample, fake_gate):
    os.remove(sample / "stats" / "S1" / "bcftools.stats.txt")
    tasks.gate_task("S1")
    assert "stats/S1/bcftools.stats.txt" not in fake_gate["calls"][0]["files"]


def test_warn_mode_records_a_failed_verdict_and_continues(sample, fake_gate, capsys):
    fake_gate["verdict"] = "fail"
    tasks.gate_task("S1")  # no exception
    assert "S1 failed the quality gate: tstv 1.5 is below the minimum 1.7 (GATE_MODE=warn" in capsys.readouterr().out
    r = record(sample)
    assert r["gate"]["verdict"] == "fail" and r["gate"]["reasons"] == ["tstv 1.5 is below the minimum 1.7"]
    assert r["stages"]["QualityGate"]["status"] == "Success"


def test_fail_mode_fails_the_stage_with_the_reasons_after_storing_the_verdict(sample, fake_gate, monkeypatch):
    monkeypatch.setenv("GATE_MODE", "fail")
    fake_gate["verdict"] = "fail"
    with pytest.raises(tasks.GateFailed, match="S1 failed the quality gate: tstv 1.5 is below the minimum 1.7"):
        tasks.gate_task("S1")
    assert (sample / "qc_reports" / "S1.gate.json").exists()
    r = record(sample)
    assert r["stages"]["QualityGate"]["status"] == "Failure"
    assert r["gate"]["verdict"] == "fail" and r["gate"]["mode"] == "fail"
    assert r["status"] == "succeeded", "the core stages succeeded; the failed gate shows in its own stage"


def test_fail_mode_passes_a_passing_run(sample, fake_gate, monkeypatch):
    monkeypatch.setenv("GATE_MODE", "fail")
    tasks.gate_task("S1")
    assert record(sample)["gate"]["verdict"] == "pass"


def test_gate_config_is_passed_to_the_binary(sample, fake_gate, monkeypatch):
    monkeypatch.setenv("GATE_CONFIG", "/opt/geyser-gate/thresholds/synthetic.toml")
    tasks.gate_task("S1")
    cmd = fake_gate["calls"][0]["cmd"]
    assert cmd[cmd.index("--config") + 1] == "/opt/geyser-gate/thresholds/synthetic.toml"
    assert record(sample)["gate"]["config"] == "/opt/geyser-gate/thresholds/synthetic.toml"


def test_a_missing_binary_warns_in_warn_mode_and_fails_in_fail_mode(sample, fake_gate, monkeypatch, capsys):
    fake_gate["raise"] = FileNotFoundError("geyser-gate")
    tasks.gate_task("S1")
    assert "geyser-gate is not installed" in capsys.readouterr().out
    r = record(sample)
    assert r["gate"]["verdict"] is None and "not installed" in r["gate"]["error"]
    # The verdict key says so too, so an earlier run's verdict there can't pass for this one.
    stored = json.loads((sample / "qc_reports" / "S1.gate.json").read_text())
    assert stored["verdict"] is None and "not installed" in stored["error"]

    monkeypatch.setenv("GATE_MODE", "fail")
    with pytest.raises(RuntimeError, match="geyser-gate is not installed"):
        tasks.gate_task("S1")


def test_a_timeout_is_a_problem_like_a_missing_binary(sample, fake_gate):
    fake_gate["raise"] = subprocess.TimeoutExpired("geyser-gate", tasks.GATE_TIMEOUT_SECONDS)
    tasks.gate_task("S1")
    assert "timed out" in record(sample)["gate"]["error"]


def test_a_config_error_fails_even_in_warn_mode(sample, fake_gate):
    # Exit 2 (e.g. a GATE_CONFIG with a typo) is an operator mistake, not a quality problem.
    fake_gate["returncode"], fake_gate["write"] = 2, False
    with pytest.raises(RuntimeError, match="could not run \\(exit 2\\)"):
        tasks.gate_task("S1")


def test_a_mistyped_gate_mode_fails_before_anything_runs(sample, fake_gate, monkeypatch):
    monkeypatch.setenv("GATE_MODE", "strict")
    with pytest.raises(ValueError, match="GATE_MODE must be one of"):
        tasks.gate_task("S1")
    assert fake_gate["calls"] == []


def test_a_gate_rerun_by_hand_keeps_the_core_stages_in_the_record(sample, fake_gate):
    # No RUN_NAME (by hand): the gate's part is its own "run", and still inherits the core stages.
    tasks.gate_task("S1")
    r = record(sample)
    assert r["run_name"] is None
    assert set(r["stages"]) == {*provenance.CORE_STAGES, "QualityGate"}
    assert r["complete"] and r["status"] == "succeeded"


def test_main_runs_the_gate_without_a_reference(sample, fake_gate):
    assert tasks.main(["gate", "S1"]) == 0
    assert len(fake_gate["calls"]) == 1


def test_a_crashed_binary_only_warns_in_warn_mode(sample, fake_gate, monkeypatch, capsys):
    # The release build aborts on a panic: a negative exit code, and no verdict.
    fake_gate["returncode"], fake_gate["write"] = -6, False
    tasks.gate_task("S1")
    assert "geyser-gate exited -6" in capsys.readouterr().out
    assert record(sample)["stages"]["QualityGate"]["status"] == "Success"
    monkeypatch.setenv("GATE_MODE", "fail")
    with pytest.raises(RuntimeError, match="exited -6"):
        tasks.gate_task("S1")


def test_an_unreadable_verdict_only_warns_in_warn_mode(sample, fake_gate, monkeypatch):
    def broken(cmd, **kwargs):
        out = cmd[cmd.index("--output") + 1]
        with open(out, "w") as fh:
            fh.write('{"verdict": "maybe"}')
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(tasks.subprocess, "run", broken)
    tasks.gate_task("S1")
    assert "not a gate verdict" in record(sample)["gate"]["error"]


def test_an_earlier_verdict_is_replaced_when_the_gate_gives_none(sample, fake_gate):
    tasks.gate_task("S1")
    assert json.loads((sample / "qc_reports" / "S1.gate.json").read_text())["verdict"] == "pass"
    fake_gate["returncode"], fake_gate["write"] = 2, False
    with pytest.raises(RuntimeError):
        tasks.gate_task("S1")
    assert json.loads((sample / "qc_reports" / "S1.gate.json").read_text())["verdict"] is None


def test_gate_config_variables_are_expanded(sample, fake_gate, monkeypatch):
    # Batch overrides and docker -e pass values unexpanded.
    monkeypatch.setenv("GATE_THRESHOLDS_DIR", "/opt/geyser-gate/thresholds")
    monkeypatch.setenv("GATE_CONFIG", "$GATE_THRESHOLDS_DIR/synthetic.toml")
    tasks.gate_task("S1")
    cmd = fake_gate["calls"][0]["cmd"]
    assert cmd[cmd.index("--config") + 1] == "/opt/geyser-gate/thresholds/synthetic.toml"
