"""DeepVariant stage logic, with run_deepvariant and samtools faked: no Docker, GPU or AWS needed."""

import gzip
import json

import pytest

import deepvariant_task as dv

# Shape of run_deepvariant's output: a banner, the command, the step's own logs, bash `time` lines.
SAMPLE_OUTPUT = [
    (100.0, "***** Running the command:*****"),
    (
        100.0,
        "time seq 0 15 | parallel -q --halt 2 --line-buffer /opt/deepvariant/bin/make_examples "
        "--mode calling --ref /w/ref.fa --reads /w/S1.bam --examples /w/i/make_examples.tfrecord@16.gz",
    ),
    (130.0, "real\t0m30.000s"),
    (130.5, "***** Running the command:*****"),
    (
        130.5,
        "time /opt/deepvariant/bin/call_variants --outfile /w/i/call_variants_output.tfrecord.gz "
        "--examples /w/i/make_examples.tfrecord@16.gz --checkpoint /opt/models/wgs",
    ),
    (150.5, "real\t0m20.000s"),
    (151.0, "***** Running the command:*****"),
    (
        151.0,
        "time /opt/deepvariant/bin/postprocess_variants --ref /w/ref.fa "
        "--infile /w/i/call_variants_output.tfrecord.gz --outfile /w/out.vcf.gz",
    ),
]


def test_parse_step_timings_splits_on_banners():
    timings = dv.parse_step_timings(SAMPLE_OUTPUT, end_time=156.0)
    assert list(timings) == ["make_examples", "call_variants", "postprocess_variants"]
    assert timings["make_examples"] == pytest.approx(30.5)
    assert timings["call_variants"] == pytest.approx(20.5)
    assert timings["postprocess_variants"] == pytest.approx(5.0)


def test_parse_step_timings_ignores_step_names_in_file_paths():
    # call_variants reads make_examples.tfrecord; that must not be taken for a make_examples step.
    events = [(0.0, dv.BANNER), (0.0, "time /opt/deepvariant/bin/call_variants --examples /x/make_examples.tfrecord")]
    assert dv.parse_step_timings(events, end_time=4.0) == {"call_variants": 4.0}


def test_parse_step_timings_without_banners_is_empty():
    assert dv.parse_step_timings([(0.0, "some log line")], end_time=1.0) == {}


def test_parse_example_counts_sums_shard_summaries():
    # make_examples prints a tab-indented summary per shard (format seen in DeepVariant 1.10.0).
    events = [
        (0.0, "I1101 ... Task 0/16: Summary stats:"),
        (0.0, "\t3 candidate variants found"),
        (0.0, "\t2 examples written"),
        (0.0, "\t1 small model examples called"),
        (0.0, "\t0 examples written"),
        (0.0, "\t4 small model examples called"),
    ]
    assert dv.parse_example_counts(events) == {"examples": 2, "small_model_calls": 5}


def test_build_command_has_sample_name_shards_and_extra_args():
    cmd = dv.build_run_deepvariant_cmd(
        "/w/ref.fa", "/w/S1.bam", "/w/out.vcf.gz", "/w/i", "S1", 8, extra_args="--disable_small_model=true"
    )
    assert cmd[0] == dv.RUN_DEEPVARIANT
    for flag in (
        "--model_type=WGS",
        "--ref=/w/ref.fa",
        "--reads=/w/S1.bam",
        "--output_vcf=/w/out.vcf.gz",
        "--intermediate_results_dir=/w/i",
        "--sample_name=S1",
        "--num_shards=8",
        "--disable_small_model=true",
    ):
        assert flag in cmd


def test_run_streaming_captures_timestamped_lines_and_exit_code():
    import sys

    code, events, end = dv.run_streaming(
        [sys.executable, "-c", "import sys; print('a', flush=True); sys.stdout.buffer.write(b'\\xff\\n'); sys.exit(3)"]
    )
    assert code == 3
    assert [line for _, line in events] == ["a", "�"]  # undecodable byte replaced, not fatal
    assert all(ts <= end for ts, _ in events)


def test_keys_follow_storage_contract():
    assert dv.deepvariant_keys("S1") == {
        "bam": "alignments/S1.bam",
        "vcf": "variants_deepvariant/S1.vcf.gz",
        "caller_stats": "stats/S1/bcftools.stats.deepvariant.txt",
        "bcftools_stats": "stats/S1/bcftools.stats.txt",  # tasks.keys_for: what the gate and widgets read
    }


@pytest.fixture
def dv_inputs(local_env, monkeypatch):
    """BAM + reference (with .fai, so samtools is never needed) in local storage; samtools index faked."""
    (local_env / "alignments").mkdir(parents=True)
    (local_env / "alignments" / "S1.bam").write_bytes(b"BAM\x01fake")
    (local_env / "reference").mkdir()
    (local_env / "reference" / "ref.fa").write_text(">c\nACGT\n")
    (local_env / "reference" / "ref.fa.fai").write_text("c\t4\t3\t4\t5\n")
    indexed = []
    monkeypatch.setattr(dv, "index_bam", indexed.append)
    monkeypatch.setattr(dv, "gpu_name", lambda: "Fake GPU")
    return indexed


def fake_run(vcf_bytes=None, code=0):
    def run(cmd):
        out = next(c.split("=", 1)[1] for c in cmd if c.startswith("--output_vcf="))
        if vcf_bytes is not None:
            with open(out, "wb") as fh:
                fh.write(vcf_bytes)
        return code, SAMPLE_OUTPUT, 156.0

    return run


def metrics_lines(root):
    return [json.loads(line) for line in (root / "metrics" / "metrics.jsonl").read_text().splitlines()]


def test_stage_uploads_vcf_and_records_step_metrics(local_env, dv_inputs, monkeypatch):
    vcf = gzip.compress(b"##fileformat=VCFv4.2\n")
    monkeypatch.setattr(dv, "run_streaming", fake_run(vcf))

    dv.deepvariant_task("S1", "ref.fa")

    assert (local_env / "variants_deepvariant" / "S1.vcf.gz").read_bytes() == vcf
    assert len(dv_inputs) == 1 and dv_inputs[0].endswith("S1.bam")
    tasks_recorded = {m["task"]: m for m in metrics_lines(local_env)}
    assert set(tasks_recorded) == {
        "DeepVariant.MakeExamples",
        "DeepVariant.CallVariants",
        "DeepVariant.PostprocessVariants",
        "DeepVariant",
    }
    assert tasks_recorded["DeepVariant"]["status"] == "Success"
    assert tasks_recorded["DeepVariant.CallVariants"]["duration_seconds"] == pytest.approx(20.5)
    assert not (local_env / "variants" / "S1.vcf.gz").exists()  # bcftools output untouched


def test_stage_fails_when_deepvariant_fails(local_env, dv_inputs, monkeypatch):
    monkeypatch.setattr(dv, "run_streaming", fake_run(gzip.compress(b"x"), code=1))
    with pytest.raises(RuntimeError, match="exited with code 1"):
        dv.deepvariant_task("S1", "ref.fa")
    last = metrics_lines(local_env)[-1]
    assert (last["task"], last["status"]) == ("DeepVariant", "Failure")
    assert not (local_env / "variants_deepvariant").exists()


@pytest.mark.parametrize("vcf_bytes", [None, b""])
def test_stage_fails_on_missing_or_empty_vcf(local_env, dv_inputs, monkeypatch, vcf_bytes):
    monkeypatch.setattr(dv, "run_streaming", fake_run(vcf_bytes))
    with pytest.raises(RuntimeError, match="no VCF"):
        dv.deepvariant_task("S1", "ref.fa")
    assert metrics_lines(local_env)[-1]["status"] == "Failure"


def test_stage_reads_the_cram_and_indexes_it_itself(local_env, dv_inputs, monkeypatch):
    # The align stage writes CRAM + .crai; DeepVariant must read the CRAM, not look for a BAM,
    # and index what it downloaded rather than trust the stored .crai.
    (local_env / "alignments" / "S1.bam").unlink()
    (local_env / "alignments" / "S1.cram").write_bytes(b"CRAM")
    (local_env / "alignments" / "S1.cram.crai").write_bytes(b"STALE")
    seen = []

    def run(cmd):
        seen.extend(c for c in cmd if c.startswith("--reads="))
        return fake_run(b"x")(cmd)

    monkeypatch.setattr(dv, "run_streaming", run)

    dv.deepvariant_task("S1", "ref.fa")

    assert len(seen) == 1 and seen[0].endswith("S1.cram")
    assert len(dv_inputs) == 1 and dv_inputs[0].endswith("S1.cram")  # indexed in the job


def test_stage_fails_without_bam(local_env, dv_inputs, monkeypatch):
    (local_env / "alignments" / "S1.bam").unlink()
    monkeypatch.setattr(dv, "run_streaming", fake_run(b"x"))
    with pytest.raises(FileNotFoundError, match="No alignment for S1"):
        dv.deepvariant_task("S1", "ref.fa")


def test_cli_requires_reference():
    with pytest.raises(SystemExit):
        dv.main(["S1"])


# ---------- PASS-call stats (#58 follow-up) ----------

STATS_TEXT = (
    "SN\t0\tnumber of records:\t5\n"
    "SN\t0\tnumber of SNPs:\t4\n"
    "SN\t0\tnumber of indels:\t1\n"
    "TSTV\t0\t3\t1\t3.00\t3\t1\t3.00\n"
)


@pytest.fixture
def fake_stats(monkeypatch):
    """bcftools stats faked: writes STATS_TEXT to the command's '> out' and records the command."""
    commands = []

    def run(label, command, skipped="its stats are skipped for this run"):
        commands.append(command)
        out = command.rsplit("> ", 1)[1].strip("'")
        with open(out, "w") as fh:
            fh.write(STATS_TEXT)
        return True

    monkeypatch.setattr(dv, "_run_stats_tool", run)
    return commands


def test_stats_count_pass_calls_only():
    cmd = dv.pass_stats_command("/w/ref.fa", "/w/S1.vcf.gz", "/w/out.txt")
    assert cmd.startswith("bcftools stats -f PASS ") and cmd.endswith("/w/S1.vcf.gz > /w/out.txt")


def test_extra_caller_writes_only_its_own_stats(local_env, dv_inputs, monkeypatch, fake_stats, capsys):
    # A benchmark running bcftools and DeepVariant: the sample's bcftools.stats.txt is the bcftools stage's.
    monkeypatch.delenv("PIPELINE_CALLER", raising=False)
    monkeypatch.setattr(dv, "run_streaming", fake_run(gzip.compress(b"##fileformat=VCFv4.2\n")))
    dv.deepvariant_task("S1", "ref.fa")
    assert (local_env / "stats" / "S1" / "bcftools.stats.deepvariant.txt").read_text() == STATS_TEXT
    assert not (local_env / "stats" / "S1" / "bcftools.stats.txt").exists()
    assert len(fake_stats) == 1 and " -f PASS " in fake_stats[0]
    out = capsys.readouterr().out
    assert "bcftools stats deepvariant: records=5, snps=4, indels=1, tstv=3.0" in out
    assert "\nbcftools stats: " not in out  # not the line the quality widgets read


@pytest.mark.parametrize("pangenome", [None, "g.gbz"])
def test_the_runs_caller_also_writes_the_samples_stats(
    local_env, dv_inputs, monkeypatch, fake_stats, capsys, pangenome
):
    caller = "pangenome" if pangenome else "deepvariant"
    monkeypatch.setenv("PIPELINE_CALLER", caller)
    monkeypatch.setattr(dv, "run_streaming", fake_run(gzip.compress(b"##fileformat=VCFv4.2\n")))
    if pangenome:
        (local_env / "pangenome").mkdir()
        (local_env / "pangenome" / pangenome).write_bytes(b"graph")
        dv.pangenome_deepvariant_task("S1", "ref.fa", pangenome)
    else:
        dv.deepvariant_task("S1", "ref.fa")
    stats = local_env / "stats" / "S1"
    assert (stats / "bcftools.stats.txt").read_text() == (stats / f"bcftools.stats.{caller}.txt").read_text()
    assert "bcftools stats: records=5, snps=4, indels=1, tstv=3.0" in capsys.readouterr().out
    # Listed as outputs of the run, so the quality gate doesn't take them for leftovers of an earlier run.
    import provenance
    import tasks

    record = provenance.read_record(tasks.storage(), "S1")
    outputs = {o["key"] for o in record["outputs"]}
    assert {"stats/S1/bcftools.stats.txt", f"stats/S1/bcftools.stats.{caller}.txt"} <= outputs
    assert record["stats"]["bcftools_stats"]["tstv"] == 3.0


def test_another_runs_caller_does_not_claim_the_samples_stats(local_env, dv_inputs, monkeypatch, fake_stats):
    monkeypatch.setenv("PIPELINE_CALLER", "bcftools")
    monkeypatch.setattr(dv, "run_streaming", fake_run(gzip.compress(b"##fileformat=VCFv4.2\n")))
    dv.deepvariant_task("S1", "ref.fa")
    assert not (local_env / "stats" / "S1" / "bcftools.stats.txt").exists()


def test_failed_stats_never_fail_the_stage(local_env, dv_inputs, monkeypatch):
    monkeypatch.setenv("PIPELINE_CALLER", "deepvariant")
    monkeypatch.setattr(dv, "_run_stats_tool", lambda label, command, skipped="": False)
    vcf = gzip.compress(b"##fileformat=VCFv4.2\n")
    monkeypatch.setattr(dv, "run_streaming", fake_run(vcf))
    dv.deepvariant_task("S1", "ref.fa")
    assert (local_env / "variants_deepvariant" / "S1.vcf.gz").read_bytes() == vcf
    assert not (local_env / "stats").exists()
