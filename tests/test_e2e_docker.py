"""Full pipeline in Docker against synthetic data with planted SNPs.

Opt in with GEYSER_E2E=1 after:  docker build -f Dockerfile -t geyser-app .
"""
import json
import os

import pytest

import make_test_data
import run_local

pytestmark = pytest.mark.skipif(
    os.environ.get("GEYSER_E2E") != "1", reason="set GEYSER_E2E=1 to run the Docker end-to-end test"
)


def test_pipeline_recovers_planted_snps(tmp_path, capfd):
    root = tmp_path / "bucket"
    make_test_data.main(["--root", str(root)])

    exit_code, log = _run(root, "SYNTH001", capfd)

    assert exit_code == 0
    assert (root / "qc_reports" / "SYNTH001.fastp.html").exists()
    report = json.loads((root / "qc_reports" / "SYNTH001.fastp.json").read_text())
    # Report only: every read is counted and none is filtered out (the same reads align sees).
    assert report["summary"]["before_filtering"]["total_reads"] == 10_000
    assert report["filtering_result"]["passed_filter_reads"] == 10_000
    # This reference starts with sequence, so the align stage must have fallen back to bwa (bwa-mem2#262).
    assert "bwa-mem2#262" in log and "Aligning with bwa\n" in log.replace("\r", "")


def test_bwa_mem2_aligns_a_reference_that_starts_with_a_gap(tmp_path, capfd):
    # The default synthetic reference starts with sequence, so the align stage falls back to bwa
    # there (bwa-mem2#262, see BWA_MEM2_SAFE_N_PREFIX). With a leading N gap, like a human
    # chromosome, bwa-mem2 itself must align and every planted SNP must still be called exactly.
    import tasks

    root = tmp_path / "bucket"
    make_test_data.main(["--root", str(root), "--leading-gap", str(tasks.BWA_MEM2_SAFE_N_PREFIX)])

    exit_code, log = _run(root, "SYNTH001", capfd)

    assert exit_code == 0  # --check: 10/10 planted SNPs, nothing unexpected
    assert "Aligning with bwa-mem2" in log and "bwa-mem2#262" not in log



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


def _run(root, sample, capfd):
    exit_code = run_local.main([
        "--root", str(root), "--sample", sample, "--reference", "synthetic_ref.fa", "--check",
        "--image", os.environ.get("GEYSER_IMAGE", "geyser-app"),
    ])
    out, err = capfd.readouterr()
    return exit_code, out + err


def _markdup_stat(log, name):
    import re

    return int(re.search(rf"^{name}: (\d+)", log, re.M).group(1))


def test_paired_end_sample_runs_through_every_stage(tmp_path, capfd):
    import tasks

    root = tmp_path / "bucket"
    # A leading gap so paired-end reads go through bwa-mem2 too (see the single-end gap test above).
    make_test_data.main(["--root", str(root), "--sample", "PAIRED001", "--paired",
                         "--leading-gap", str(tasks.BWA_MEM2_SAFE_N_PREFIX)])

    exit_code, log = _run(root, "PAIRED001", capfd)

    assert exit_code == 0  # --check: 10/10 planted SNPs, nothing unexpected
    assert "Aligning with bwa-mem2" in log
    assert "samtools fixmate -m" in log
    report = json.loads((root / "qc_reports" / "PAIRED001.fastp.json").read_text())  # one report, both mates
    assert report["summary"]["before_filtering"]["total_reads"] == 10_000 and report["insert_size"]["peak"] > 0
    assert not list((root / "qc_reports").glob("PAIRED001_R*"))
    assert _markdup_stat(log, "PAIRED") == 10_000 and _markdup_stat(log, "SINGLE") == 0
    # With both ends required to match, chance duplicates nearly vanish (single-end: ~485 of 10,000).
    assert _markdup_stat(log, "DUPLICATE TOTAL") < 50


def test_paired_end_planted_duplicates_are_marked_as_pairs(tmp_path, capfd):
    import json

    root = tmp_path / "bucket"
    make_test_data.main(["--root", str(root), "--sample", "PAIRED001", "--paired", "--duplicate-rate", "0.1"])
    planted_pairs = json.loads((root / "truth" / "PAIRED001.json").read_text())["planted_duplicates"]

    exit_code, log = _run(root, "PAIRED001", capfd)

    assert exit_code == 0
    # markdup counts reads, so each duplicate pair is 2; the rest are the few chance pairs seen above.
    marked_reads = _markdup_stat(log, "DUPLICATE PAIR")
    assert 2 * planted_pairs <= marked_reads < 2 * planted_pairs + 50


@pytest.mark.parametrize("leading_gap", [0, 1000], ids=["bwa-fallback", "bwa-mem2"])
def test_truncated_upload_fails_both_qc_and_align(tmp_path, leading_gap):
    # There is no decompress stage to trip over a cut-off upload any more, so both stages that read
    # the .gz must fail on it themselves (fastp: "igzip: unexpected eof"; bwa and bwa-mem2:
    # gzclose error), rather than align quietly writing a BAM of the reads before the cut. The
    # leading gap decides which aligner runs (bwa-mem2#262 guard), so both are covered.
    import subprocess

    root = tmp_path / "bucket"
    make_test_data.main(["--root", str(root), "--leading-gap", str(leading_gap)])
    raw = root / "raw_reads" / "SYNTH001.fastq.gz"
    data = raw.read_bytes()
    raw.write_bytes(data[:len(data) // 2])

    image = os.environ.get("GEYSER_IMAGE", "geyser-app")
    for stage, needs_ref in run_local.STAGES[:2]:
        assert stage in ("qc", "align")
        cmd = run_local.stage_command(stage, "SYNTH001", "synthetic_ref.fa", needs_ref)
        result = subprocess.run(run_local.docker_command(cmd, str(root), image), capture_output=True, text=True)
        assert result.returncode != 0, f"{stage} accepted a truncated upload:\n{result.stdout}{result.stderr}"
    assert not (root / "alignments" / "SYNTH001.bam").exists()
    assert not list((root / "qc_reports").glob("SYNTH001*")) if (root / "qc_reports").exists() else True
