"""scripts/build_reference_index.py: the submit-job request, and its container script run for real under bash
with fake samtools / bwa-mem2, a file:// download and local storage (no Docker, no AWS)."""

import gzip
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys

import boto3
import pytest
from botocore.stub import ANY, Stubber

import build_reference_index as bri
import tasks

NOW = 1793750400.0  # 2026-11-04T00:00:00Z


def request(argv=()):
    return bri.submit_request(bri.parse_args(list(argv)), now=NOW)


def env_of(req):
    return {e["name"]: e["value"] for e in req["containerOverrides"]["environment"]}


# --- the request ---------------------------------------------------------------------------------------------


def test_default_request_builds_grch38_no_alt_on_the_large_job_definition():
    req = request()
    assert req["jobName"] == "IndexBuild-GRCh38_no_alt-20261104T000000Z"
    assert req["jobQueue"] == "geyser-job-queue"
    assert req["jobDefinition"] == "geyser-app-large-job"
    assert req["timeout"] == {"attemptDurationSeconds": 14400}
    over = req["containerOverrides"]
    assert over["command"] == ["bash", "-c", bri.CONTAINER_SCRIPT]
    assert over["resourceRequirements"] == [{"type": "VCPU", "value": "16"}, {"type": "MEMORY", "value": "122880"}]
    assert env_of(req) == {
        "REF_URL": bri.DEFAULT_URL,
        "REF_MD5": "a08035b6a6e31780e96a34008ff21bd6",
        "REF_NAME": "GRCh38_no_alt.fa",
        "REF_EXPECT_CONTIGS": "195",
        "REF_UPLOAD_EXTS": ".fai .0123 .amb .ann .bwt.2bit.64 .pac",
        "REF_FORCE": "0",
    }
    assert "GCA_000001405.15_GRCh38_no_alt_analysis_set.fna.gz" in bri.DEFAULT_URL
    assert set(req) == {"jobName", "jobQueue", "jobDefinition", "containerOverrides", "timeout"}


def test_the_uploaded_index_is_exactly_what_the_align_stage_fetches():
    exts = env_of(request())["REF_UPLOAD_EXTS"].split()
    assert exts[0] == ".fai"
    assert exts[1:] == tasks.ALIGNER_INDEX_EXTS["bwa-mem2"]


def test_options_reach_the_request():
    req = request(
        [
            "--url", "https://example.org/x.fa", "--md5", "0" * 32, "--name", "x.fa", "--expect-contigs", "3",
            "--force", "--vcpus", "8", "--memory-mib", "61440", "--timeout-seconds", "600", "--job-name", "J",
            "--queue", "q", "--job-definition", "d",
        ]
    )  # fmt: skip
    env = env_of(req)
    assert (env["REF_URL"], env["REF_MD5"], env["REF_NAME"]) == ("https://example.org/x.fa", "0" * 32, "x.fa")
    assert (env["REF_EXPECT_CONTIGS"], env["REF_FORCE"]) == ("3", "1")
    assert (req["jobName"], req["jobQueue"], req["jobDefinition"]) == ("J", "q", "d")
    assert req["containerOverrides"]["resourceRequirements"][1] == {"type": "MEMORY", "value": "61440"}
    assert req["timeout"] == {"attemptDurationSeconds": 600}


def test_job_name_is_batch_safe():
    assert bri.job_name("hg38.v2+alt.fa", NOW) == "IndexBuild-hg38-v2-alt-20261104T000000Z"


@pytest.mark.parametrize(
    "argv",
    [
        ["--vcpus", "3"],
        ["--memory-mib", "130000"],  # above Fargate's 120 GiB
        ["--vcpus", "8", "--memory-mib", "62000"],  # not a 4 GiB step
        ["--name", "sub/ref.fa"],
        ["--name", "ref.fa.gz"],
        ["--md5", "ABC"],
        ["--expect-contigs", "0"],
    ],
)
def test_bad_arguments_are_refused_before_any_aws_call(argv, capsys):
    with Stubber(boto3.client("batch", region_name="eu-west-2")) as stub:
        assert bri.main([*argv], batch_client=stub.client) == 2
    assert "error:" in capsys.readouterr().err


def test_dry_run_prints_the_request_and_calls_nothing(capsys):
    batch = boto3.client("batch", region_name="eu-west-2")
    with Stubber(batch):  # any call would fail: the stub was given none
        assert bri.main(["--dry-run"], batch_client=batch) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["jobDefinition"] == "geyser-app-large-job"
    assert printed["containerOverrides"]["command"][2] == bri.CONTAINER_SCRIPT


def test_submit_sends_the_request(capsys):
    batch = boto3.client("batch", region_name="eu-west-2")
    with Stubber(batch) as stub:
        expected = request() | {"jobName": ANY}  # main() stamps the current time
        stub.add_response("submit_job", {"jobName": "IndexBuild-GRCh38_no_alt-x", "jobId": "j-1"}, expected)
        assert bri.main([], batch_client=batch) == 0
        stub.assert_no_pending_responses()
    assert "job j-1" in capsys.readouterr().out


# --- the container script's text -----------------------------------------------------------------------------


def test_script_uploads_the_fasta_last_and_checks_before_the_expensive_steps():
    s = bri.CONTAINER_SCRIPT
    order = [
        "already exists",  # 1 refuse an existing reference
        "md5 mismatch",  # 2 download + md5
        "gunzip -c download",  # 3
        "samtools faidx",  # 4
        "REF_EXPECT_CONTIGS",
        "effective_aligner",  # 5 bwa-mem2#262 guard
        "bwa-mem2 index",  # 6
        'REF_UPLOAD_EXTS"].split() + [""]',  # 7 indexes, then "" = the FASTA itself
    ]
    positions = [s.index(marker) for marker in order]
    assert positions == sorted(positions)
    assert s.startswith("set -euo pipefail\n")
    assert 'export APP="$PWD"' in s  # the Python steps import tasks/storage from the image's /app


def test_script_takes_every_value_from_the_environment():
    s = bri.CONTAINER_SCRIPT
    for literal in (bri.DEFAULT_URL, bri.DEFAULT_MD5, bri.DEFAULT_NAME, "195", "GRCh38"):
        assert literal not in s


# --- the container script, run -------------------------------------------------------------------------------

BASH = shutil.which("bash")
# Git Bash works on Windows; the WSL launcher in System32 would see none of these Windows paths.
needs_bash = pytest.mark.skipif(BASH is None or "system32" in BASH.lower(), reason="needs bash (CI runs it)")


def _posix(path):
    return pathlib.Path(path).as_posix()


@pytest.fixture
def job(tmp_path):
    """Run CONTAINER_SCRIPT like the job would: cwd = an "/app" holding tasks.py and storage.py, fake tools."""
    app = tmp_path / "app"
    shutil.copytree(os.path.join(bri.REPO_ROOT, "app"), app, ignore=shutil.ignore_patterns("__pycache__"))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tools = {
        "python": f'exec "{_posix(sys.executable)}" "$@"\n',
        # faidx: one .fai line per header (name, a length of 100)
        "samtools": 'awk \'/^>/ {print substr($1, 2) "\\t100\\t0\\t60\\t61"}\' "$2" > "$2.fai"\n',
        "bwa-mem2": 'for e in .0123 .amb .ann .bwt.2bit.64 .pac; do echo "$e" > "$2$e"; done\n',
    }
    for tool, body in tools.items():
        path = bin_dir / tool
        path.write_text("#!/usr/bin/env bash\n" + body)
        path.chmod(0o755)
    bucket = tmp_path / "bucket"

    def run(fasta_text, expect_contigs=2, md5=None, force="0", gz=True):
        raw = fasta_text.encode()
        data = gzip.compress(raw) if gz else raw
        src = tmp_path / ("src.fa.gz" if gz else "src.fa")
        src.write_bytes(data)
        env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "STORAGE_BACKEND": "local",
            "LOCAL_STORAGE_DIR": str(bucket),
            "REF_URL": src.as_uri(),
            "REF_MD5": md5 or hashlib.md5(data).hexdigest(),
            "REF_NAME": "test_ref.fa",
            "REF_EXPECT_CONTIGS": str(expect_contigs),
            "REF_UPLOAD_EXTS": " ".join(bri.upload_exts()),
            "REF_FORCE": force,
        }
        script = bri.CONTAINER_SCRIPT.replace("W=/tmp/refbuild", f"W={_posix(tmp_path / 'work')}")
        return subprocess.run([BASH, "-c", script], cwd=app, env=env, capture_output=True, text=True, timeout=120)

    run.bucket = bucket
    return run


GAPPED = ">chr1 first\n" + "N" * 1000 + "ACGTACGT\n>chr2\nACGTACGTAC\n"


def stored(bucket):
    ref = bucket / "reference"
    return sorted(p.name for p in ref.iterdir()) if ref.exists() else []


@needs_bash
def test_builds_and_uploads_indexes_then_the_fasta(job):
    result = job(GAPPED)
    assert result.returncode == 0, result.stderr
    uploaded = [line.split()[1] for line in result.stdout.splitlines() if line.startswith("uploaded ")]
    assert uploaded == ["test_ref.fa" + e for e in bri.upload_exts()] + ["test_ref.fa"]
    assert stored(job.bucket) == sorted(uploaded)
    assert (job.bucket / "reference" / "test_ref.fa").read_text() == GAPPED  # gunzipped


@needs_bash
def test_an_uncompressed_url_is_stored_as_is(job):
    assert job(GAPPED, gz=False).returncode == 0
    assert (job.bucket / "reference" / "test_ref.fa").read_text() == GAPPED


@needs_bash
def test_md5_mismatch_stops_before_anything_is_stored(job):
    result = job(GAPPED, md5="0" * 32)
    assert result.returncode != 0
    assert "md5 mismatch" in result.stderr
    assert stored(job.bucket) == []


@needs_bash
def test_wrong_contig_count_stops_before_indexing(job):
    result = job(GAPPED, expect_contigs=195)
    assert result.returncode != 0
    assert "contig count 2, expected 195" in result.stderr
    assert "bwa-mem2 index" not in result.stdout.split("== ")[-1]
    assert stored(job.bucket) == []


@needs_bash
def test_a_reference_bwa_mem2_would_not_be_used_on_is_refused(job):
    result = job(">chr1\nACGTACGT\n>chr2\nACGT\n")  # no leading N gap: align would use bwa (bwa-mem2#262)
    assert result.returncode != 0
    assert "a bwa-mem2 index is useless" in result.stderr
    assert stored(job.bucket) == []


@needs_bash
def test_an_existing_reference_is_kept_unless_forced(job):
    assert job(GAPPED).returncode == 0
    again = job(GAPPED)
    assert again.returncode != 0
    assert "already exists" in again.stderr
    assert job(GAPPED, force="1").returncode == 0
