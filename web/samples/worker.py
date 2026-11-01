"""The local trigger: the offline stand-in for 'S3 upload -> Lambda -> Step Functions'.

It polls raw_reads/ for samples that haven't been run, and runs each through
scripts/run_local.py (the same four containers the cloud runs). Progress goes into
run records (runs.py) so the UI can show it. The web app never calls this; it only
writes the upload, exactly as in the cloud.

The runner is swappable: anything with ``run(sample_id, on_output) -> exit_code``.
"""

import os
import subprocess
import sys
import time

from geyser_web.pipeline import RUN_LOCAL_PATH, keys_for

from .runs import log_key, put_bytes, read_all_runs, read_run, write_run
from .status import list_sample_ids


class RunLocalRunner:
    """Runs scripts/run_local.py for one sample as a subprocess."""

    def __init__(self, root, runner="docker", image="geyser-app", reference="synthetic_ref.fa", python=None):
        self.root = str(root)
        self.runner = runner
        self.image = image
        self.reference = reference
        self.python = python or sys.executable

    def command(self, sample_id):
        return [self.python, "-u", RUN_LOCAL_PATH, "--root", self.root, "--sample", sample_id,
                "--runner", self.runner, "--image", self.image, "--reference", self.reference]

    def run(self, sample_id, on_output):
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        proc = subprocess.Popen(self.command(sample_id), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env=env, text=True, encoding="utf-8", errors="replace", bufsize=1)
        for line in proc.stdout:
            on_output(line)
        return proc.wait()


class Worker:
    def __init__(self, storage, runner, log_flush_seconds=2.0, clock=time.time, say=print):
        self.storage = storage
        self.runner = runner
        self.log_flush_seconds = log_flush_seconds
        self.clock = clock
        self.say = say

    def recover(self):
        """After a restart, a run left 'running' was interrupted. Queued runs are simply picked up again."""
        for sample_id, record in read_all_runs(self.storage).items():
            if record.get("state") == "running":
                record.update(state="failed", finished=self.clock(), note="The worker stopped during this run.")
                write_run(self.storage, sample_id, record)

    def pending(self):
        """Samples to run: never attempted (and not already finished, e.g. by an earlier CLI run), or queued."""
        runs = read_all_runs(self.storage)
        out = []
        for sample_id in list_sample_ids(self.storage):
            record = runs.get(sample_id)
            if record is None:
                if not self.storage.exists(keys_for(sample_id)["vcf"]):
                    out.append(sample_id)
            elif record.get("state") == "queued":
                out.append(sample_id)
        return out

    def run_once(self):
        """Queue everything new, then run the queue in order. Returns how many samples ran."""
        pending = self.pending()
        for sample_id in pending:
            if read_run(self.storage, sample_id) is None:
                write_run(self.storage, sample_id, {"state": "queued", "queued": self.clock()})
        for sample_id in pending:
            self.process(sample_id)
        return len(pending)

    def process(self, sample_id):
        record = read_run(self.storage, sample_id) or {"queued": self.clock()}
        record.update(state="running", started=self.clock())
        write_run(self.storage, sample_id, record)
        self.say(f"[worker] running {sample_id}")

        lines = []
        last_flush = [self.clock()]

        def flush():
            put_bytes(self.storage, log_key(sample_id), "".join(lines).encode("utf-8"))
            last_flush[0] = self.clock()

        def on_output(line):
            lines.append(line)
            if self.clock() - last_flush[0] >= self.log_flush_seconds:
                flush()

        try:
            exit_code = self.runner.run(sample_id, on_output)
        except Exception as err:  # noqa: BLE001 - a broken runner must not kill the worker
            lines.append(f"\nThe worker could not run the pipeline: {err!r}\n")
            exit_code = -1
        flush()
        record.update(state="succeeded" if exit_code == 0 else "failed", finished=self.clock(), exit_code=exit_code)
        write_run(self.storage, sample_id, record)
        self.say(f"[worker] {sample_id}: {record['state']} (exit code {exit_code})")

    def serve_forever(self, interval=2.0):
        self.recover()
        while True:
            if not self.run_once():
                time.sleep(interval)
