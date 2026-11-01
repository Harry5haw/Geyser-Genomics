import os
import sys

import pytest
from django.core.management import CommandError, call_command

from .web_helpers import fastq_gz, put
from samples.runs import read_run, read_text, write_run
from samples.worker import RunLocalRunner, Worker


class FakeRunner:
    def __init__(self, exit_codes=None, error=None):
        self.exit_codes = exit_codes or {}
        self.error = error
        self.calls = []

    def run(self, sample_id, on_output):
        self.calls.append(sample_id)
        if self.error:
            raise self.error
        on_output(f">>> python tasks.py decompress {sample_id}\n")
        on_output("done\n")
        return self.exit_codes.get(sample_id, 0)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        self.t += 1
        return self.t


def make_worker(storage, runner):
    return Worker(storage, runner, log_flush_seconds=0, clock=Clock(), say=lambda msg: None)


def test_runs_new_uploads_once_and_records_outcome(storage):
    put(storage, "raw_reads/A.fastq.gz", fastq_gz())
    put(storage, "raw_reads/B.fastq.gz", fastq_gz())
    runner = FakeRunner(exit_codes={"B": 1})
    worker = make_worker(storage, runner)

    assert worker.run_once() == 2
    assert runner.calls == ["A", "B"]
    a, b = read_run(storage, "A"), read_run(storage, "B")
    assert a["state"] == "succeeded" and a["exit_code"] == 0
    assert b["state"] == "failed" and b["exit_code"] == 1
    assert a["queued"] < a["started"] < a["finished"]
    assert "decompress A" in read_text(storage, "runs/A.log")

    assert worker.run_once() == 0  # nothing runs twice
    assert runner.calls == ["A", "B"]


def test_skips_samples_already_finished_outside_the_worker(storage):
    put(storage, "raw_reads/A.fastq.gz", fastq_gz())
    put(storage, "variants/A.vcf.gz", b"x")  # e.g. from scripts/run_local.py on the command line
    assert make_worker(storage, FakeRunner()).pending() == []


def test_runner_crash_marks_failed_and_keeps_going(storage):
    put(storage, "raw_reads/A.fastq.gz", fastq_gz())
    worker = make_worker(storage, FakeRunner(error=FileNotFoundError("docker")))
    worker.run_once()
    assert read_run(storage, "A")["state"] == "failed"
    assert "could not run the pipeline" in read_text(storage, "runs/A.log")


def test_recover_after_restart(storage):
    for sid in ("RUNNING", "QUEUED"):
        put(storage, f"raw_reads/{sid}.fastq.gz", fastq_gz())
    write_run(storage, "RUNNING", {"state": "running", "started": 1})
    write_run(storage, "QUEUED", {"state": "queued", "queued": 1})
    runner = FakeRunner()
    worker = make_worker(storage, runner)
    worker.recover()
    assert read_run(storage, "RUNNING")["state"] == "failed"
    assert "stopped" in read_run(storage, "RUNNING")["note"]
    worker.run_once()
    assert runner.calls == ["QUEUED"]


def test_run_local_command(tmp_path):
    runner = RunLocalRunner(tmp_path, runner="host", image="img", reference="ref.fa", python="py")
    cmd = runner.command("S1")
    assert cmd[0] == "py" and cmd[2].endswith(os.path.join("scripts", "run_local.py"))
    assert cmd[3:] == ["--root", str(tmp_path), "--sample", "S1", "--runner", "host", "--image", "img",
                       "--reference", "ref.fa"]


def test_run_local_runner_streams_output(tmp_path):
    """Swap run_local.py for a tiny script to check the subprocess plumbing without Docker."""
    script = tmp_path / "fake_run_local.py"
    script.write_text("import sys\nprint('stage one')\nprint('stage two')\nsys.exit(3)\n")
    runner = RunLocalRunner(tmp_path, python=sys.executable)
    runner.command = lambda sample_id: [sys.executable, str(script)]
    lines = []
    assert runner.run("S1", lines.append) == 3
    assert [line.strip() for line in lines] == ["stage one", "stage two"]


def test_runworker_refuses_non_local_storage(settings):
    settings.GEYSER_STORAGE_BACKEND = "s3"
    with pytest.raises(CommandError, match="STORAGE_BACKEND=local"):
        call_command("runworker", "--once")
