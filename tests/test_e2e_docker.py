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


def test_planted_duplicates_are_marked_without_changing_calls(tmp_path, capfd):
    # Exact read copies (PCR duplicates) must be flagged by the align stage's samtools markdup,
    # and the SNP calls must be exactly as without them (bcftools skips flagged reads).
    import json
    import re

    root = tmp_path / "bucket"
    make_test_data.main(["--root", str(root), "--duplicate-rate", "0.1"])
    planted = json.loads((root / "truth" / "SYNTH001.json").read_text())["planted_duplicates"]
    assert planted > 500

    exit_code = run_local.main([
        "--root", str(root), "--sample", "SYNTH001", "--reference", "synthetic_ref.fa", "--check",
        "--image", os.environ.get("GEYSER_IMAGE", "geyser-app"),
    ])
    out, err = capfd.readouterr()

    assert exit_code == 0  # --check: every planted SNP called, nothing unexpected
    marked = int(re.search(r"^DUPLICATE TOTAL: (\d+)", out + err, re.M).group(1))
    # Each planted copy adds one flagged read; single-end reads that merely share a start add a few more.
    assert planted <= marked < planted + 0.1 * 10_000
