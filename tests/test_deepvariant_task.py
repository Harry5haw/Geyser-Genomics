"""DeepVariant stage logic, with run_deepvariant and samtools faked: no Docker, GPU or AWS needed."""
import gzip
import json

import pytest

import deepvariant_task as dv

# Shape of run_deepvariant's output: a banner, the command, the step's own logs, bash `time` lines.
SAMPLE_OUTPUT = [
    (100.0, "***** Running the command:*****"),
    (100.0, "time seq 0 15 | parallel -q --halt 2 --line-buffer /opt/deepvariant/bin/make_examples "
            "--mode calling --ref /w/ref.fa --reads /w/S1.bam --examples /w/i/make_examples.tfrecord@16.gz"),
    (130.0, "real\t0m30.000s"),
    (130.5, "***** Running the command:*****"),
    (130.5, "time /opt/deepvariant/bin/call_variants --outfile /w/i/call_variants_output.tfrecord.gz "
            "--examples /w/i/make_examples.tfrecord@16.gz --checkpoint /opt/models/wgs"),
    (150.5, "real\t0m20.000s"),
    (151.0, "***** Running the command:*****"),
    (151.0, "time /opt/deepvariant/bin/postprocess_variants --ref /w/ref.fa "
            "--infile /w/i/call_variants_output.tfrecord.gz --outfile /w/out.vcf.gz"),
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
    events = [(0.0, "I1101 ... Task 0/16: Summary stats:"), (0.0, "\t3 candidate variants found"),
              (0.0, "\t2 examples written"), (0.0, "\t1 small model examples called"),
              (0.0, "\t0 examples written"), (0.0, "\t4 small model examples called")]
    assert dv.parse_example_counts(events) == {"examples": 2, "small_model_calls": 5}


def test_build_command_has_sample_name_shards_and_extra_args():
    cmd = dv.build_run_deepvariant_cmd("/w/ref.fa", "/w/S1.bam", "/w/out.vcf.gz", "/w/i", "S1", 8,
                                       extra_args="--disable_small_model=true")
    assert cmd[0] == dv.RUN_DEEPVARIANT
    for flag in ("--model_type=WGS", "--ref=/w/ref.fa", "--reads=/w/S1.bam", "--output_vcf=/w/out.vcf.gz",
                 "--intermediate_results_dir=/w/i", "--sample_name=S1", "--num_shards=8",
                 "--disable_small_model=true"):
        assert flag in cmd


def test_run_streaming_captures_timestamped_lines_and_exit_code():
    import sys
    code, events, end = dv.run_streaming(
        [sys.executable, "-c", "import sys; print('a', flush=True); sys.stdout.buffer.write(b'\\xff\\n'); sys.exit(3)"])
    assert code == 3
    assert [line for _, line in events] == ["a", "�"]  # undecodable byte replaced, not fatal
    assert all(ts <= end for ts, _ in events)


def test_keys_follow_storage_contract():
    assert dv.deepvariant_keys("S1") == {"bam": "alignments/S1.bam", "vcf": "variants_deepvariant/S1.vcf.gz"}


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
    assert set(tasks_recorded) == {"DeepVariant.MakeExamples", "DeepVariant.CallVariants",
                                   "DeepVariant.PostprocessVariants", "DeepVariant"}
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


def test_stage_fails_without_bam(local_env, dv_inputs, monkeypatch):
    (local_env / "alignments" / "S1.bam").unlink()
    monkeypatch.setattr(dv, "run_streaming", fake_run(b"x"))
    with pytest.raises(Exception):
        dv.deepvariant_task("S1", "ref.fa")


def test_cli_requires_reference():
    with pytest.raises(SystemExit):
        dv.main(["S1"])
