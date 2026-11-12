import importlib.util
import os
import shlex
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "app"))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "figures"))  # each figure imports its sibling figkit.py
sys.path.insert(0, os.path.join(REPO_ROOT, "app-deepvariant"))

import benchmark_history  # noqa: E402 - needs scripts/ on sys.path (above)
import tasks  # noqa: E402 - needs app/ on sys.path (above)


@pytest.fixture(autouse=True)
def _no_tracked_benchmark_history(tmp_path, monkeypatch):
    """No test may append to docs/benchmarks/history.csv: benchmark_happy.py records every run it scores."""
    monkeypatch.setattr(benchmark_history, "HISTORY", str(tmp_path / "benchmark-history.csv"))


def load_module(name, *path_parts):
    spec = importlib.util.spec_from_file_location(name, os.path.join(REPO_ROOT, *path_parts))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def trigger():
    return load_module("geyser_trigger", "lambda", "trigger", "index.py")


@pytest.fixture
def local_env(tmp_path, monkeypatch):
    """Point tasks.py at a fresh local storage root, with no AWS variables visible."""
    import tasks

    for var in (
        "BUCKET_NAME",
        "AWS_REGION",
        "S3_ENDPOINT_URL",
        "S3_ADDRESSING_STYLE",
        "METRICS_BACKEND",
        "METRICS_FILE",
        "ALIGNER",
        "READ_TYPE",
        "DEEPVARIANT_MODEL_TYPE",
        "GIRAFFE_GRAPH",
        "GIRAFFE_SAMPLING",
        "KMC_MEMORY_GB",
        "RUN_NAME",
        "APP_VERSION",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path / "bucket"))
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setattr(tasks, "_storage", None)
    monkeypatch.setattr(tasks, "_metrics", None)
    return tmp_path / "bucket"


# --- faked bioinformatics tools, shared by the stage tests (test_stage_io, test_scatter_gather) --------

# Real samtools 1.21 / mosdepth 0.3.15 / bcftools 1.21 output for the synthetic sample (single-end and
# paired-end, both with --leading-gap 1000), so the parsers are tested on the tools' actual formats.
STATS_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "stats")


def stats_fixture(layout, name):
    with open(os.path.join(STATS_FIXTURES, f"{layout}.{name}"), "rb") as fh:
        return fh.read()


@pytest.fixture
def tool_calls(monkeypatch):
    """Fake subprocess.run for index builders and fastp; each fake writes the files the real tool would."""
    calls = []

    def fake_run(cmd, check=False, timeout=None, **kwargs):
        calls.append(list(cmd))
        tool = cmd[0]
        if tool in tasks.ALIGNER_INDEX_EXTS and cmd[1] == "index":
            for ext in tasks.ALIGNER_INDEX_EXTS[tool]:
                open(cmd[2] + ext, "wb").close()
        elif tool == "samtools" and cmd[1] == "faidx":
            open(cmd[2] + ".fai", "wb").close()
        elif tool == "fastp":
            # One report per run, at the paths given by --html and --json.
            for flag, text in (("--html", "html"), ("--json", '{"summary": {}}')):
                with open(cmd[cmd.index(flag) + 1], "w") as fh:
                    fh.write(text)
        else:
            raise AssertionError(f"unexpected command {cmd}")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(tasks.subprocess, "run", fake_run)
    return calls


@pytest.fixture
def shell_commands(monkeypatch):
    """Fake run_shell: records each pipeline and creates the output file named after -o / the last argument.

    Stats tools get real output (the fixtures): mosdepth writes <prefix>.mosdepth.*.txt (its last
    argument is the input), flagstat and bcftools stats write the file after '>'.
    """
    commands = []

    def fake_run_shell(command):
        commands.append(command)
        words = shlex.split(command)
        if words[0] == "mosdepth":
            prefix = words[-2]
            for name in ("mosdepth.summary.txt", "mosdepth.global.dist.txt"):
                with open(f"{prefix}.{name}", "wb") as fh:
                    fh.write(stats_fixture("single", name))
            return
        out = words[words.index("-o") + 1] if "-o" in words else words[-1]
        with open(out, "wb") as fh:
            name = os.path.basename(out)
            if name in ("flagstat.txt", "bcftools.stats.txt"):
                fh.write(stats_fixture("single", name))
            else:
                fh.write(b"output of: " + command.encode())

    monkeypatch.setattr(tasks, "run_shell", fake_run_shell)
    return commands


@pytest.fixture
def uploads(local_env, monkeypatch):
    """Record the keys uploaded, in order (the real upload still runs)."""
    keys = []
    store = tasks.storage()
    real = store.upload
    monkeypatch.setattr(store, "upload", lambda path, key, **kw: keys.append(key) or real(path, key, **kw))
    return keys
