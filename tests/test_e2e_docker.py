"""Full pipeline in Docker against synthetic data with planted SNPs.

Opt in with GEYSER_E2E=1 after:  docker build -f Dockerfile -t geyser-app .
"""
import os

import pytest

import make_test_data
import run_local

pytestmark = pytest.mark.skipif(
    os.environ.get("GEYSER_E2E") != "1", reason="set GEYSER_E2E=1 to run the Docker end-to-end test"
)


def test_pipeline_recovers_planted_snps(tmp_path):
    root = tmp_path / "bucket"
    make_test_data.main(["--root", str(root)])
    exit_code = run_local.main([
        "--root", str(root), "--sample", "SYNTH001", "--reference", "synthetic_ref.fa", "--check",
        "--image", os.environ.get("GEYSER_IMAGE", "geyser-app"),
    ])
    assert exit_code == 0
    assert (root / "qc_reports" / "SYNTH001_fastqc.html").exists()
