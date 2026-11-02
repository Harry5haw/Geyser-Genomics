"""Pangenome-aware DeepVariant in Docker on synthetic data with a synthetic graph, opt in (#51).

    GEYSER_PG_E2E=1   the pangenome GPU image with --gpus all
                      (docker build -f app-deepvariant/Dockerfile
                       --build-arg DV_TAG=pangenome_aware_deepvariant-1.10.0-gpu -t geyser-deepvariant-pangenome .)

Also needs geyser-app (the stages before calling) and quay.io/vgteam/vg:v1.55.0 (make_test_data.py --pangenome
builds the graph with it). GEYSER_PG_IMAGE overrides the pangenome image name. The image is 38 GB, so this does not
run in CI; run it locally before changing app-deepvariant or the pangenome image tag.
"""

import os

import pytest

import make_test_data
import run_local

pytestmark = pytest.mark.skipif(
    os.environ.get("GEYSER_PG_E2E") != "1",
    reason="set GEYSER_PG_E2E=1 to run pangenome-aware DeepVariant in Docker (38 GB GPU image)",
)


def test_pangenome_deepvariant_calls_the_planted_snps_and_not_the_panel_only_ones(tmp_path, monkeypatch):
    root = tmp_path / "bucket"
    make_test_data.main(["--root", str(root), "--pangenome"])
    # 10 GB GPUs need a smaller call_variants batch than the default 1,024.
    monkeypatch.setenv("DEEPVARIANT_EXTRA_ARGS", "--call_variants_extra_args=batch_size=256")
    exit_code = run_local.main(
        [
            "--root",
            str(root),
            "--sample",
            "SYNTH001",
            "--reference",
            "synthetic_ref.fa",
            "--caller",
            "pangenome",
            "--pangenome",
            "synthetic_ref.gbz",
            "--pg-image",
            os.environ.get("GEYSER_PG_IMAGE", "geyser-deepvariant-pangenome"),
            "--image",
            os.environ.get("GEYSER_IMAGE", "geyser-app"),
            "--dv-memory",
            "8g",
        ]
    )
    assert exit_code == 0
    assert (root / "variants_pangenome" / "SYNTH001.vcf.gz").stat().st_size > 0
    counts = run_local.truth_counts(str(root), "SYNTH001", "pangenome")
    assert counts["tp"] == counts["truth"] == 10, f"missed planted SNPs: {counts['missed']}"
    # The graph's panel carries 5 decoy SNPs the sample does not have: no read supports them, so none is called.
    assert not counts["extra"], f"unexpected calls: {counts['extra']}"
