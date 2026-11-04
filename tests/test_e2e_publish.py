"""The publish image (app-publish/, W2) end to end, opt in: GEYSER_PUBLISH_E2E=1.

Needs geyser-app and geyser-publish:
    docker build -f Dockerfile -t geyser-app .
    docker build -f app-publish/Dockerfile -t geyser-publish .
GEYSER_PUBLISH_IMAGE overrides the image name. The image renders a small hand-made bucket (mounted read-only) to a
folder, as the cloud Publish state does to the site bucket: this checks the image's wiring (the repo layout the UI
expects, the settings, the entrypoint's secret key), not the pages, which web/tests/test_web_publish.py covers.
"""

import gzip
import io
import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("GEYSER_PUBLISH_E2E") != "1",
    reason="set GEYSER_PUBLISH_E2E=1 to run the publish image (geyser-publish) in Docker",
)
IMAGE = os.environ.get("GEYSER_PUBLISH_IMAGE", "geyser-publish")
VCF = (
    "##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tsample\n"
    "synth1\t1000\t.\tA\tG\t99.5\t.\tDP=30\tGT\t0/1\n"
)


def gz(data: bytes) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as fh:
        fh.write(data)
    return buf.getvalue()


def bucket(root: Path) -> Path:
    files = {
        "raw_reads/E2E1.fastq.gz": gz(b"@r1\nACGT\n+\nIIII\n"),
        "qc_reports/E2E1.fastp.html": b"<html></html>",
        "qc_reports/E2E1.fastp.json": json.dumps({"summary": {}}).encode(),
        "alignments/E2E1.cram": b"CRAM-not-public",
        "variants/E2E1.vcf.gz": gz(VCF.encode()),
    }
    for key, data in files.items():
        path = root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def test_the_image_publishes_a_bucket_to_a_folder(tmp_path):
    data, site = bucket(tmp_path / "bucket"), tmp_path / "site"
    site.mkdir()
    run = subprocess.run(
        [
            "docker", "run", "--rm", "--memory", "1g",
            "-v", f"{data}:/data:ro", "-v", f"{site}:/site",
            "-e", "STORAGE_BACKEND=local", "-e", "LOCAL_STORAGE_DIR=/data",
            IMAGE, "python", "manage.py", "publish", "--all", "--out", "/site",
        ],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert run.returncode == 0, run.stderr
    assert "1 sample(s)" in run.stdout
    for page in ("index.html", "404.html", "csp-hashes.json", "samples/E2E1/index.html", "figures/index.html"):
        assert (site / page).is_file(), page
    assert sorted(p.name for p in (site / "samples/E2E1/files").iterdir()) == [
        "E2E1.fastp.html",
        "E2E1.fastp.json",
        "E2E1.vcf.gz",
    ]
    assert (site / "figures/coverage.json").is_file()  # the figure CSVs are in the image


@pytest.mark.parametrize(
    "env, message",
    [(["-e", "BUCKET_NAME=lake"], "no output"), (["-e", "SITE_BUCKET=site"], "BUCKET_NAME must be set")],
)
def test_the_image_says_what_is_missing(env, message):
    run = subprocess.run(
        ["docker", "run", "--rm", *env, IMAGE, "python", "manage.py", "publish", "--all"],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert run.returncode == 1 and message in run.stderr and "Traceback" not in run.stderr
