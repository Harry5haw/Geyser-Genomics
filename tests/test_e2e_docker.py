"""Full pipeline in Docker against synthetic data with planted SNPs.

Opt in with GEYSER_E2E=1 after:  docker build -f Dockerfile -t geyser-app .
"""

import json
import os
import subprocess

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
    _check_cram_and_indexes(root, "SYNTH001", reads=10_000)
    _check_stats(root, "SYNTH001", paired=False)
    _check_run_record(root, "SYNTH001", aligner="bwa")
    _check_gate(root, "SYNTH001", layout="single")


def _check_run_record(root, sample, aligner):
    """runs/<id>.provenance.json: one complete run, with real tool versions and the reference's MD5."""
    import hashlib
    import json

    record = json.loads((root / "runs" / f"{sample}.provenance.json").read_text())
    assert record["status"] == "succeeded" and record["complete"], record["stages"]
    assert set(record["stages"]) == {"QualityControl", "Align", "CallVariants", "QualityGate"}
    assert record["run_name"].startswith(f"{sample}-") and record["run_name"].endswith("-local")
    assert record["app_version"].startswith("local:")
    assert record["aligner"] == aligner
    ref = root / "reference" / "synthetic_ref.fa"
    assert record["reference"]["md5"] == hashlib.md5(ref.read_bytes()).hexdigest()
    for tool in ("fastp", "samtools", "bcftools", "mosdepth", "geyser-gate", aligner):
        assert record["tools"].get(tool), (tool, record["tools"])  # every tool reported a version
    assert record["tools"]["mosdepth release"] == "0.3.15"
    outputs = {o["key"] for o in record["outputs"]}
    assert {f"alignments/{sample}.cram", f"variants/{sample}.vcf.gz", f"stats/{sample}/flagstat.txt"} <= outputs
    assert record["stats"]["flagstat"]["in_total"] == 10_000
    assert record["gate"]["verdict"] == "pass" and record["gate"]["mode"] == "warn", record["gate"]
    assert f"qc_reports/{sample}.gate.json" in outputs

    for task, stage in record["stages"].items():  # #59: each job measured itself
        usage = stage["resources"]
        # The quality gate parses a few small files in milliseconds: on a fast runner its CPU time rounds
        # to 0.00 s (measured in CI). It must still have measured itself.
        assert usage["cpu_seconds"] > 0 or (task == "QualityGate" and usage["cpu_seconds"] == 0), (task, usage)
        assert usage["peak_memory_mib"] > 10, (task, usage)
        print(f"{task} resources: {usage}")


def _check_gate(root, sample, layout):
    """qc_reports/{id}.gate.json: the real binary's verdict, with run_local's synthetic thresholds."""
    verdict = json.loads((root / "qc_reports" / f"{sample}.gate.json").read_text())
    assert verdict["verdict"] == "pass", verdict["reasons"]
    assert verdict["config"] == "/opt/geyser-gate/thresholds/synthetic.toml"
    assert verdict["layout"] == layout
    assert all(v["status"] == "ok" for v in verdict["inputs"].values()), verdict["inputs"]  # nothing stale
    assert verdict["metrics"]["read_count_difference"] == 0  # no read lost between upload and alignment
    assert verdict["metrics"]["reads"] == 10_000


def _check_stats(root, sample, paired):
    """stats/{id}/ holds all four files and they describe the synthetic data: 30x reads, 10 SNPs."""
    import tasks

    stats = {
        name: (root / key).read_text() for name, key in tasks.keys_for(sample).items() if name in tasks.STATS_FILES
    }
    assert set(stats) == set(tasks.STATS_FILES)
    flagstat = tasks.parse_flagstat(stats["flagstat"])
    assert flagstat["in_total"] == 10_000 and flagstat["mapped_pct"] >= 99.5
    if paired:
        assert flagstat["properly_paired_pct"] >= 99.0
    depth = tasks.parse_mosdepth_summary(stats["mosdepth_summary"])["mean_depth"]
    assert 25 <= depth <= 31, depth  # simulated 30x, duplicates and the leading gap (if any) not counted
    at_1x = next(
        float(line.split("\t")[2]) for line in stats["mosdepth_dist"].splitlines() if line.startswith("total\t1\t")
    )
    assert at_1x >= 0.95, at_1x  # nearly every base covered (only a leading N gap, if any, is not)
    variants = tasks.parse_bcftools_stats(stats["bcftools_stats"])
    assert variants["snps"] == 10 and variants["indels"] == 0 and variants["tstv"] is not None


def _check_cram_and_indexes(root, sample, reads):
    """CRAM + .crai and VCF + .tbi, no BAM; the CRAM decodes against the reference, both indexes answer a region."""
    import subprocess

    for key in (
        f"alignments/{sample}.cram",
        f"alignments/{sample}.cram.crai",
        f"variants/{sample}.vcf.gz",
        f"variants/{sample}.vcf.gz.tbi",
    ):
        assert (root / key).stat().st_size > 0, key
    assert not (root / "alignments" / f"{sample}.bam").exists()
    script = (
        f"set -e; cd /data; samtools quickcheck -v alignments/{sample}.cram; "
        f"samtools view -c -T reference/synthetic_ref.fa alignments/{sample}.cram; "
        f"samtools view -c -T reference/synthetic_ref.fa alignments/{sample}.cram synth1:20000-30000; "
        f"bcftools view -H -r synth1:1-60000 variants/{sample}.vcf.gz | wc -l"
    )
    out = subprocess.run(
        run_local.docker_command(["bash", "-c", script], str(root), os.environ.get("GEYSER_IMAGE", "geyser-app")),
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    total, region, calls = (int(x) for x in out.stdout.split())
    assert total == reads  # every read decodes against the reference
    assert 0 < region < total  # the .crai serves a region query
    assert calls >= 10  # the .tbi serves a region query (every planted SNP is in it)


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

    exit_code = run_local.main(
        [
            "--root",
            str(root),
            "--sample",
            "SYNTH001",
            "--reference",
            "synthetic_ref.fa",
            "--check",
            "--image",
            os.environ.get("GEYSER_IMAGE", "geyser-app"),
        ]
    )
    out, err = capfd.readouterr()

    assert exit_code == 0  # --check: every planted SNP called, nothing unexpected
    marked = int(re.search(r"^DUPLICATE TOTAL: (\d+)", out + err, re.M).group(1))
    # Each planted copy adds one flagged read; single-end reads that merely share a start add a few more.
    assert planted <= marked < planted + 0.1 * 10_000

    # The quality gate catches the duplicated library (synthetic thresholds: at most 6% duplicates).
    # GATE_MODE=warn (default): the verdict is stored and recorded, and the run still succeeds.
    verdict = json.loads((root / "qc_reports" / "SYNTH001.gate.json").read_text())
    assert verdict["verdict"] == "fail"
    assert any(r.startswith("duplicate_pct ") and "above the maximum 6" in r for r in verdict["reasons"])
    assert "SYNTH001 failed the quality gate: " in out + err and "GATE_MODE=warn" in out + err
    record = json.loads((root / "runs" / "SYNTH001.provenance.json").read_text())
    assert record["gate"]["verdict"] == "fail" and record["status"] == "succeeded"
    # GATE_MODE=fail: the same verdict fails the stage (the container exits non-zero).
    image = os.environ.get("GEYSER_IMAGE", "geyser-app")
    env = {"GATE_MODE": "fail", "GATE_CONFIG": "/opt/geyser-gate/thresholds/synthetic.toml"}
    cmd = run_local.docker_command(["python", "tasks.py", "gate", "SYNTH001"], str(root), image, extra_env=env)
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode != 0 and "GateFailed: SYNTH001 failed the quality gate" in result.stdout + result.stderr


def _run(root, sample, capfd, *extra):
    exit_code = run_local.main(
        [
            "--root",
            str(root),
            "--sample",
            sample,
            "--reference",
            "synthetic_ref.fa",
            "--check",
            "--image",
            os.environ.get("GEYSER_IMAGE", "geyser-app"),
            *extra,
        ]
    )
    out, err = capfd.readouterr()
    return exit_code, out + err


def _markdup_stat(log, name):
    import re

    return int(re.search(rf"^{name}: (\d+)", log, re.M).group(1))


def test_paired_end_sample_runs_through_every_stage(tmp_path, capfd):
    import tasks

    root = tmp_path / "bucket"
    # A leading gap so paired-end reads go through bwa-mem2 too (see the single-end gap test above).
    make_test_data.main(
        ["--root", str(root), "--sample", "PAIRED001", "--paired", "--leading-gap", str(tasks.BWA_MEM2_SAFE_N_PREFIX)]
    )

    exit_code, log = _run(root, "PAIRED001", capfd)

    assert exit_code == 0  # --check: 10/10 planted SNPs, nothing unexpected
    assert "Aligning with bwa-mem2" in log
    assert "samtools fixmate -m" in log
    _check_cram_and_indexes(root, "PAIRED001", reads=10_000)  # 5,000 pairs
    _check_stats(root, "PAIRED001", paired=True)
    _check_run_record(root, "PAIRED001", aligner="bwa-mem2")
    _check_gate(root, "PAIRED001", layout="paired")
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

    import tasks

    root = tmp_path / "bucket"
    make_test_data.main(["--root", str(root), "--leading-gap", str(leading_gap)])
    raw = root / "raw_reads" / "SYNTH001.fastq.gz"
    data = raw.read_bytes()
    raw.write_bytes(data[: len(data) // 2])

    image = os.environ.get("GEYSER_IMAGE", "geyser-app")
    for stage, needs_ref in run_local.STAGES[:2]:
        assert stage in ("qc", "align")
        cmd = run_local.stage_command(stage, "SYNTH001", "synthetic_ref.fa", needs_ref)
        result = subprocess.run(run_local.docker_command(cmd, str(root), image), capture_output=True, text=True)
        assert result.returncode != 0, f"{stage} accepted a truncated upload:\n{result.stdout}{result.stderr}"
    for name in ("SYNTH001.cram", "SYNTH001.cram.crai", "SYNTH001.bam"):
        assert not (root / "alignments" / name).exists()
    keys = tasks.keys_for("SYNTH001")
    assert not (root / keys["qc_html"]).exists() and not (root / keys["qc_json"]).exists()


def _vcf_records(path):
    import gzip

    with gzip.open(path, "rt") as fh:
        return [line for line in fh if not line.startswith("#")]


@pytest.mark.parametrize("paired", [False, True], ids=["single-end", "paired-end"])
def test_sharded_calling_gives_the_same_calls_as_one_job(tmp_path, capfd, paired):
    # Scatter-gather (#108): 8 regions of 7,000 bp, one bcftools job each, then the merge. Positions are
    # called in exactly one region, so the merged VCF must have the very same records as the single job.
    import tasks

    root = tmp_path / "bucket"
    sample = "PAIRED001" if paired else "SYNTH001"
    make_test_data.main(
        ["--root", str(root), "--sample", sample, "--leading-gap", "1000"] + (["--paired"] if paired else [])
    )
    exit_code, _ = _run(root, sample, capfd)
    assert exit_code == 0
    unsharded = _vcf_records(root / "variants" / f"{sample}.vcf.gz")

    exit_code, log = _run(root, sample, capfd, "--skip-to-variants", "--shard-window", "7000")

    assert exit_code == 0  # --check: 10/10 planted SNPs from the merged VCF, nothing unexpected
    assert "Merging 8 region parts" in log
    assert _vcf_records(root / "variants" / f"{sample}.vcf.gz") == unsharded and len(unsharded) == 10
    assert len(list((root / "intermediate" / "variant-parts" / sample).glob("*/*.vcf.gz"))) == 8  # <RUN_NAME>/
    assert (root / "variants" / f"{sample}.vcf.gz.tbi").exists()
    record = json.loads((root / "runs" / f"{sample}.provenance.json").read_text())
    assert record["status"] == "succeeded"  # the variants-only run inherits QC and align (provenance.UPSTREAM)
    assert record["stages"]["CallVariants"]["run_name"] != record["stages"]["Align"]["run_name"]
    metrics = [json.loads(line) for line in (root / "metrics" / "metrics.jsonl").read_text().splitlines()]
    assert sum(m["task"] == tasks.SHARD_METRIC for m in metrics) == 8


def test_gene_models_give_consequences_for_genic_snps_only(tmp_path, capfd):
    # make_test_data --gff puts two genes (+ and - strand) around planted SNPs 2-5; the variants
    # stage then runs bcftools csq. Same calls as without annotation (--check), plus BCSQ on exactly
    # the SNPs inside a gene.
    import gzip

    root = tmp_path / "bucket"
    make_test_data.main(["--root", str(root), "--gff"])
    exit_code, log = _run(root, "SYNTH001", capfd)
    assert exit_code == 0  # --check on the plain VCF: 10/10 planted SNPs
    assert "bcftools csq" in log

    truth = json.loads((root / "truth" / "SYNTH001.json").read_text())
    genic = {s["pos"] for s in truth["snps"][1:5]}
    consequences = {}
    with gzip.open(root / "variants" / "SYNTH001.csq.vcf.gz", "rt") as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            cols = line.split("\t")
            bcsq = [item[5:] for item in cols[7].split(";") if item.startswith("BCSQ=")]
            if bcsq:
                consequences[int(cols[1])] = bcsq[0]
    assert set(consequences) == genic
    genes = {c.split("|")[1] for c in consequences.values()}
    assert genes == {"SYNTHG1", "SYNTHG2"}
    assert (root / "variants" / "SYNTH001.csq.vcf.gz.tbi").exists()


# --- v1.4 "Big inputs": the large route and region-streamed calling (docs/design/genome-scale-app.md) -----------


def _flagstat(root, sample):
    import tasks

    return tasks.parse_flagstat((root / "stats" / sample / "flagstat.txt").read_text())


@pytest.mark.parametrize("paired", [False, True])
def test_the_large_route_gives_the_single_jobs_calls(tmp_path, capfd, paired):
    # preflight into 4 chunks, align each (no markdup), merge + markdup once: the same CRAM content as one align
    # job, so the same calls. Duplicate counts may differ by a hair: markdup's choice among equally good
    # duplicates can depend on input order, which chunking changes (documented tolerance: 1% of duplicates).
    import tasks

    sample = "PAIRED001" if paired else "SYNTH001"
    extra = ["--sample", sample, "--paired", "--leading-gap", str(tasks.BWA_MEM2_SAFE_N_PREFIX)] if paired else []
    single, chunked = tmp_path / "single", tmp_path / "chunked"
    for root in (single, chunked):
        make_test_data.main(["--root", str(root), *extra])
    code, log = _run(single, sample, capfd)
    assert code == 0, log
    code, log = _run(chunked, sample, capfd, "--chunks", "4")
    assert code == 0, log
    assert "Pre-flight OK" in log and "Merging 4 chunk BAMs" in log
    assert _vcf_records(chunked / "variants" / f"{sample}.vcf.gz") == _vcf_records(
        single / "variants" / f"{sample}.vcf.gz"
    )
    a, b = _flagstat(single, sample), _flagstat(chunked, sample)
    for key in ("in_total", "mapped", "primary_mapped") + (("properly_paired",) if paired else ()):
        assert a[key] == b[key], key
    assert abs(a["duplicates"] - b["duplicates"]) <= max(1, a["duplicates"] // 100)
    report = json.loads((chunked / "qc_reports" / f"{sample}.preflight.json").read_text())
    assert report["ok"] and report["chunk_count"] == 4
    record = json.loads((chunked / "runs" / f"{sample}.provenance.json").read_text())
    assert record["status"] == "succeeded" and record["stages"]["Align"]["status"] == "Success"


def test_calling_shards_read_their_region_in_place(tmp_path, capfd, monkeypatch):
    # align writes the CRAM's ETag on its .crai, so every shard reads only its region; the merged calls equal
    # those of shards that download the CRAM (GEYSER_STREAM_REGIONS=0), which equal the unsharded call.
    streamed, downloaded = tmp_path / "streamed", tmp_path / "downloaded"
    for root in (streamed, downloaded):
        make_test_data.main(["--root", str(root)])
    code, log = _run(streamed, "SYNTH001", capfd, "--shard-window", "8000")
    assert code == 0, log
    assert log.count("Read the region in place") >= 2
    monkeypatch.setenv("GEYSER_STREAM_REGIONS", "0")
    code, log = _run(downloaded, "SYNTH001", capfd, "--shard-window", "8000")
    assert code == 0, log
    assert "Read the region in place" not in log
    assert _vcf_records(streamed / "variants" / "SYNTH001.vcf.gz") == _vcf_records(
        downloaded / "variants" / "SYNTH001.vcf.gz"
    )
