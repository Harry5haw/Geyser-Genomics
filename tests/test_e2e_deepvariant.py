"""DeepVariant stage in Docker, opt in. Skipped by default (multi-GB images; the GPU one needs a GPU).

    GEYSER_GPU_E2E=1  GPU image with --gpus all   (docker build -f app-deepvariant/Dockerfile -t geyser-deepvariant .)
    GEYSER_DV_E2E=1   CPU image, no GPU needed    (... --build-arg DV_TAG=1.10.0 -t geyser-deepvariant-cpu .)

Both need geyser-app too (docker build -f Dockerfile -t geyser-app .) for the stages before calling.
GEYSER_DV_IMAGE overrides the DeepVariant image name.
"""

import json
import os

import pytest

import make_test_data
import run_local
import tasks

GPU = os.environ.get("GEYSER_GPU_E2E") == "1"
CPU = os.environ.get("GEYSER_DV_E2E") == "1"

pytestmark = pytest.mark.skipif(
    not (GPU or CPU),
    reason="set GEYSER_GPU_E2E=1 (GPU image) or GEYSER_DV_E2E=1 (CPU image) to run DeepVariant in Docker",
)


def dv_args():
    image = os.environ.get("GEYSER_DV_IMAGE", "geyser-deepvariant" if GPU else "geyser-deepvariant-cpu")
    return ["--dv-image", image] + ([] if GPU else ["--no-gpu"])


def test_deepvariant_on_synthetic_data(tmp_path):
    root = tmp_path / "bucket"
    make_test_data.main(["--root", str(root)])
    exit_code = run_local.main(
        [
            "--root",
            str(root),
            "--sample",
            "SYNTH001",
            "--reference",
            "synthetic_ref.fa",
            "--caller",
            "both",
            "--image",
            os.environ.get("GEYSER_IMAGE", "geyser-app"),
        ]
        + dv_args()
    )
    assert exit_code == 0
    assert (root / "variants_deepvariant" / "SYNTH001.vcf.gz").stat().st_size > 0
    counts = run_local.truth_counts(str(root), "SYNTH001", "deepvariant")
    assert counts["tp"] == counts["truth"], f"missed planted SNPs: {counts['missed']}"
    assert not counts["extra"], f"unexpected calls: {counts['extra']}"
    # PASS-call stats: exactly the planted SNPs, whatever RefCall candidates the VCF also holds. With
    # bcftools in the run, the sample's bcftools.stats.txt stays the bcftools stage's.
    stats_dir = root / "stats" / "SYNTH001"
    dv_stats = tasks.parse_bcftools_stats((stats_dir / "bcftools.stats.deepvariant.txt").read_text())
    assert dv_stats["snps"] == counts["truth"], dv_stats
    record = json.loads((root / "runs" / "SYNTH001.provenance.json").read_text())
    assert "stats/SYNTH001/bcftools.stats.deepvariant.txt" in {o["key"] for o in record["outputs"]}


def test_a_deepvariant_only_run_is_complete(tmp_path):
    # #158: no bcftools stage, so no CallVariants; DeepVariant is the run's caller (PIPELINE_CALLER) and
    # completes the record. The quality gate then judges DeepVariant's PASS-call stats.
    root = tmp_path / "bucket"
    make_test_data.main(["--root", str(root)])
    exit_code = run_local.main(
        [
            "--root",
            str(root),
            "--sample",
            "SYNTH001",
            "--reference",
            "synthetic_ref.fa",
            "--caller",
            "deepvariant",
            "--image",
            os.environ.get("GEYSER_IMAGE", "geyser-app"),
        ]
        + dv_args()
    )
    assert exit_code == 0
    record = json.loads((root / "runs" / "SYNTH001.provenance.json").read_text())
    assert (record["status"], record["complete"], record["caller"]) == ("succeeded", True, "deepvariant")
    assert "CallVariants" not in record["stages"] and record["stages"]["DeepVariant"]["status"] == "Success"
    verdict = json.loads((root / "qc_reports" / "SYNTH001.gate.json").read_text())
    run_status = next(c for c in verdict["checks"] if c["name"] == "run_status")
    assert run_status["status"] == "pass", run_status


def test_image_bcftools_counts_pass_calls_only(tmp_path):
    """The DeepVariant image's own bcftools (Ubuntu's) on a VCF with RefCall candidates, using the stage's
    exact command: 5 PASS records (4 SNPs, Ti/Tv 3.0), while all 10 records would give Ti/Tv 0.5."""
    import shutil
    import subprocess

    import deepvariant_task as dv

    fixtures = os.path.join(os.path.dirname(__file__), "fixtures", "deepvariant")
    for name in ("ref.fa", "refcall.vcf"):
        shutil.copy(os.path.join(fixtures, name), tmp_path / name)
    image = dv_args()[1]

    def stats(command):
        docker = ["docker", "run", "--rm", "--mount", f"type=bind,source={tmp_path},target=/f", image]
        out = subprocess.run(docker + ["bash", "-c", command], check=True, capture_output=True, text=True)
        return tasks.parse_bcftools_stats(out.stdout)

    passed = stats(dv.pass_stats_command("/f/ref.fa", "/f/refcall.vcf", "/dev/stdout"))
    assert passed == {"records": 5, "snps": 4, "indels": 1, "tstv": 3.0}
    everything = stats("bcftools stats -F /f/ref.fa -s - /f/refcall.vcf")
    assert everything["records"] == 10 and everything["tstv"] == 0.5


DV_TESTDATA = os.environ.get("GEYSER_DV_TESTDATA")  # a --root that scripts/fetch_dv_testdata.py filled


@pytest.mark.skipif(not DV_TESTDATA, reason="set GEYSER_DV_TESTDATA=<root filled by fetch_dv_testdata.py>")
def test_deepvariant_on_real_slice():
    exit_code = run_local.main(
        [
            "--root",
            DV_TESTDATA,
            "--sample",
            "NA12878_S1",
            "--reference",
            "ucsc.hg19.chr20.unittest.fasta",
            "--caller",
            "deepvariant",
            "--skip-to-variants",
        ]
        + dv_args()
    )
    assert exit_code == 0
    counts = run_local.truth_counts(DV_TESTDATA, "NA12878_S1", "deepvariant")
    assert counts["truth"] > 0
    assert counts["tp"] / counts["truth"] >= 0.9, counts
