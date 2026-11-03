"""ALIGNER=giraffe in Docker on the synthetic pangenome graph, opt in (#146).

    GEYSER_GIRAFFE_E2E=1   needs geyser-app and geyser-giraffe:
                               docker build -f Dockerfile -t geyser-app .
                               docker build -f app-giraffe/Dockerfile -t geyser-giraffe .
                           and quay.io/vgteam/vg:v1.55.0 (make_test_data.py --pangenome builds the graph with it)

GEYSER_IMAGE / GEYSER_GIRAFFE_IMAGE override the image names. make_test_data.py --pangenome writes a 50 kb
graph: the reference as the GRCh38 path, six panel haplotypes carrying the planted SNPs, and five decoy SNPs
only the panel carries. The sample is aligned to it with vg giraffe, surjected to the reference and called with
bcftools, so a correct run calls exactly the planted SNPs: none of the decoys, which no read supports.
Each run takes well under a minute.
"""

import json
import os
import subprocess

import pytest

import make_test_data
import run_local

pytestmark = pytest.mark.skipif(
    os.environ.get("GEYSER_GIRAFFE_E2E") != "1",
    reason="set GEYSER_GIRAFFE_E2E=1 to run the giraffe align stage in Docker (geyser-giraffe image)",
)
APP_IMAGE = os.environ.get("GEYSER_IMAGE", "geyser-app")
GIRAFFE_IMAGE = os.environ.get("GEYSER_GIRAFFE_IMAGE", "geyser-giraffe")


def run_giraffe(root, sample, *extra):
    return run_local.main(
        [
            "--root",
            str(root),
            "--sample",
            sample,
            "--reference",
            "synthetic_ref.fa",
            "--aligner",
            "giraffe",
            "--image",
            APP_IMAGE,
            "--giraffe-image",
            GIRAFFE_IMAGE,
            *extra,
        ]
    )


def assert_planted_snps_only(root, sample):
    counts = run_local.truth_counts(str(root), sample)
    assert counts["tp"] == counts["truth"] == 10, f"missed planted SNPs: {counts['missed']}"
    assert not counts["extra"], f"unexpected calls (decoys?): {counts['extra']}"


def cram_contigs(root, sample):
    """@SQ names of the stored CRAM, read with geyser-app's samtools (the header needs no reference)."""
    out = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--mount",
            f"type=bind,source={os.path.abspath(root)},target=/data",
            APP_IMAGE,
            "samtools",
            "view",
            "-H",
            f"/data/alignments/{sample}.cram",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [f[3:] for line in out.splitlines() if line.startswith("@SQ") for f in line.split("\t") if f[:3] == "SN:"]


def align_part(root, sample):
    with open(root / "runs" / f"{sample}.provenance" / "Align.json") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def bucket(tmp_path_factory):
    """One graph, a single-end and a paired-end sample from the same synthetic genome."""
    root = tmp_path_factory.mktemp("giraffe") / "bucket"
    make_test_data.main(["--root", str(root), "--pangenome"])
    make_test_data.main(["--root", str(root), "--pangenome", "--paired", "--sample", "PAIRED001"])
    return root


def test_giraffe_with_haplotype_sampling_calls_the_planted_snps(bucket):
    assert run_giraffe(bucket, "SYNTH001", "--check") == 0
    assert_planted_snps_only(bucket, "SYNTH001")
    # Surjected and renamed: the CRAM is on the FASTA's contig (GRCh38#0#synth1 -> synth1).
    assert cram_contigs(bucket, "SYNTH001") == ["synth1"]
    part = align_part(bucket, "SYNTH001")
    assert part["status"] == "Success" and part["aligner"] == "giraffe" and part["layout"] == "single"
    assert part["graph"] == {"name": "synthetic_ref.gbz", "haplotype_sampling": True}
    assert part["tools"]["vg"] == "1.55.0" and part["tools"]["kmc"] == "3.2.4"
    assert part["stats"]["flagstat"]["mapped_pct"] > 99


def test_giraffe_paired_end_calls_the_planted_snps(bucket):
    assert run_giraffe(bucket, "PAIRED001", "--check") == 0
    assert_planted_snps_only(bucket, "PAIRED001")
    part = align_part(bucket, "PAIRED001")
    assert part["layout"] == "paired"
    assert part["stats"]["flagstat"]["properly_paired_pct"] > 95  # fixmate saw the mates side by side


def test_giraffe_on_the_whole_graph_calls_the_planted_snps(bucket, monkeypatch):
    monkeypatch.setenv("GIRAFFE_SAMPLING", "off")
    assert run_giraffe(bucket, "SYNTH001", "--check") == 0
    assert_planted_snps_only(bucket, "SYNTH001")
    assert align_part(bucket, "SYNTH001")["graph"]["haplotype_sampling"] is False


def test_giraffe_refuses_a_graph_of_another_reference(bucket, tmp_path):
    """A graph whose GRCh38 path is not the FASTA's contig fails the align stage instead of writing a bad CRAM."""
    root = tmp_path / "bucket"
    make_test_data.main(["--root", str(root), "--pangenome", "--contig", "synthX"])
    # Swap in the FASTA of the first bucket: same sequence, contig synth1, so the graph's synthX path is foreign.
    with open(bucket / "reference" / "synthetic_ref.fa", "rb") as src:
        (root / "reference" / "synthetic_ref.fa").write_bytes(src.read())
    assert run_giraffe(root, "SYNTH001", "--no-gate") == 1
    assert not (root / "alignments" / "SYNTH001.cram").exists()
