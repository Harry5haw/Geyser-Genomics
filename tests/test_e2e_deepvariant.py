"""DeepVariant stage in Docker, opt in. Skipped by default (multi-GB images; the GPU one needs a GPU).

    GEYSER_GPU_E2E=1  GPU image with --gpus all   (docker build -f app-deepvariant/Dockerfile -t geyser-deepvariant .)
    GEYSER_DV_E2E=1   CPU image, no GPU needed    (... --build-arg DV_TAG=1.10.0 -t geyser-deepvariant-cpu .)

Both need geyser-app too (docker build -f Dockerfile -t geyser-app .) for the stages before calling.
GEYSER_DV_IMAGE overrides the DeepVariant image name.
"""
import os

import pytest

import make_test_data
import run_local

GPU = os.environ.get("GEYSER_GPU_E2E") == "1"
CPU = os.environ.get("GEYSER_DV_E2E") == "1"

pytestmark = pytest.mark.skipif(
    not (GPU or CPU), reason="set GEYSER_GPU_E2E=1 (GPU image) or GEYSER_DV_E2E=1 (CPU image) to run DeepVariant in Docker"
)


def dv_args():
    image = os.environ.get("GEYSER_DV_IMAGE", "geyser-deepvariant" if GPU else "geyser-deepvariant-cpu")
    return ["--dv-image", image] + ([] if GPU else ["--no-gpu"])


def test_deepvariant_on_synthetic_data(tmp_path):
    root = tmp_path / "bucket"
    make_test_data.main(["--root", str(root)])
    exit_code = run_local.main([
        "--root", str(root), "--sample", "SYNTH001", "--reference", "synthetic_ref.fa",
        "--caller", "both", "--image", os.environ.get("GEYSER_IMAGE", "geyser-app"),
    ] + dv_args())
    assert exit_code == 0
    assert (root / "variants_deepvariant" / "SYNTH001.vcf.gz").stat().st_size > 0
    counts = run_local.truth_counts(str(root), "SYNTH001", "deepvariant")
    assert counts["tp"] == counts["truth"], f"missed planted SNPs: {counts['missed']}"
    assert not counts["extra"], f"unexpected calls: {counts['extra']}"


DV_TESTDATA = os.environ.get("GEYSER_DV_TESTDATA")  # a --root that scripts/fetch_dv_testdata.py filled


@pytest.mark.skipif(not DV_TESTDATA, reason="set GEYSER_DV_TESTDATA=<root filled by fetch_dv_testdata.py>")
def test_deepvariant_on_real_slice():
    exit_code = run_local.main([
        "--root", DV_TESTDATA, "--sample", "NA12878_S1", "--reference", "ucsc.hg19.chr20.unittest.fasta",
        "--caller", "deepvariant", "--skip-to-variants",
    ] + dv_args())
    assert exit_code == 0
    counts = run_local.truth_counts(DV_TESTDATA, "NA12878_S1", "deepvariant")
    assert counts["truth"] > 0
    assert counts["tp"] / counts["truth"] >= 0.9, counts
