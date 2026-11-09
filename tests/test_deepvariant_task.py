"""DeepVariant stage logic, with run_deepvariant and samtools faked: no Docker, GPU or AWS needed."""

import gzip
import json
import os
import sys
import time

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
    monkeypatch.setenv(dv.GPU_SAMPLE_VAR, "0")  # never sample a real GPU on the test machine
    return indexed


def fake_run(vcf_bytes=None, code=0, tbi_bytes=None):
    def run(cmd, **_):
        out = next(c.split("=", 1)[1] for c in cmd if c.startswith("--output_vcf="))
        if vcf_bytes is not None:
            with open(out, "wb") as fh:
                fh.write(vcf_bytes)
        if tbi_bytes is not None:  # postprocess_variants writes the .tbi beside a .vcf.gz
            with open(out + ".tbi", "wb") as fh:
                fh.write(tbi_bytes)
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

    def run(cmd, **_):
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
    # The single-job make_examples summary reaches the record (the split run's is tested in test_provenance).
    assert record["make_examples"]["split"] is False
    assert record["caller_outcome"] == caller


def test_another_runs_caller_does_not_claim_the_samples_stats(local_env, dv_inputs, monkeypatch, fake_stats):
    monkeypatch.setenv("PIPELINE_CALLER", "bcftools")
    monkeypatch.setattr(dv, "run_streaming", fake_run(gzip.compress(b"##fileformat=VCFv4.2\n")))
    dv.deepvariant_task("S1", "ref.fa")
    assert not (local_env / "stats" / "S1" / "bcftools.stats.txt").exists()


@pytest.mark.parametrize("primary", [False, True])
def test_failed_stats_never_fail_the_stage(local_env, dv_inputs, monkeypatch, primary):
    if primary:
        monkeypatch.setenv("PIPELINE_CALLER", "deepvariant")
    else:
        monkeypatch.delenv("PIPELINE_CALLER", raising=False)
    monkeypatch.setattr(dv, "_run_stats_tool", lambda label, command, skipped="": False)
    vcf = gzip.compress(b"##fileformat=VCFv4.2\n")
    monkeypatch.setattr(dv, "run_streaming", fake_run(vcf))
    dv.deepvariant_task("S1", "ref.fa")
    assert (local_env / "variants_deepvariant" / "S1.vcf.gz").read_bytes() == vcf
    assert not (local_env / "stats" / "S1" / "bcftools.stats.deepvariant.txt").exists()
    sample_stats = local_env / "stats" / "S1" / "bcftools.stats.txt"
    assert sample_stats.exists() == primary


def test_failed_stats_of_the_runs_caller_replace_an_earlier_runs_file(local_env, dv_inputs, monkeypatch):
    # An earlier bcftools run left its stats; this DeepVariant run's own failed. Without a replacement the
    # gate would take the old numbers for this run's (the record inherits that run's CallVariants part).
    import provenance
    import tasks

    stats = local_env / "stats" / "S1"
    stats.mkdir(parents=True)
    (stats / "bcftools.stats.txt").write_text(STATS_TEXT)
    monkeypatch.setenv("PIPELINE_CALLER", "deepvariant")
    monkeypatch.setattr(dv, "_run_stats_tool", lambda label, command, skipped="": False)
    monkeypatch.setattr(dv, "run_streaming", fake_run(gzip.compress(b"##fileformat=VCFv4.2\n")))
    dv.deepvariant_task("S1", "ref.fa")
    text = (stats / "bcftools.stats.txt").read_text()
    assert text.startswith("# No bcftools stats: deepvariant") and "SN\t" not in text
    assert tasks.parse_bcftools_stats(text) == {}
    outputs = {o["key"] for o in provenance.read_record(tasks.storage(), "S1")["outputs"]}
    assert "stats/S1/bcftools.stats.txt" in outputs


def test_the_images_bcftools_is_recorded_under_its_own_name(local_env, dv_inputs, monkeypatch, fake_stats):
    import provenance
    import tasks

    monkeypatch.setattr(provenance, "tool_version", lambda name: {"bcftools": "1.13"}.get(name))
    monkeypatch.setattr(dv, "run_streaming", fake_run(gzip.compress(b"##fileformat=VCFv4.2\n")))
    dv.deepvariant_task("S1", "ref.fa")
    tools = provenance.read_record(tasks.storage(), "S1")["tools"]
    assert tools["bcftools (DeepVariant image)"] == "1.13" and "bcftools" not in tools


# ---- stopping a hung or failed wrapper (run_streaming limits) ----


def py(code):
    import sys

    return [sys.executable, "-c", code]


def test_limits_from_env_defaults_and_overrides():
    d = dv.WrapperLimits.from_env({})
    assert (d.timeout, d.idle, d.fatal_grace) == (None, None, 120)  # whole-genome steps can be long and silent
    o = dv.WrapperLimits.from_env(
        {
            "DEEPVARIANT_TIMEOUT_SECONDS": "3300",
            "DEEPVARIANT_IDLE_TIMEOUT_SECONDS": "900",
            "DEEPVARIANT_FATAL_GRACE_SECONDS": "0",
        }
    )
    assert (o.timeout, o.idle, o.fatal_grace) == (3300, 900, None)  # 0 = off, for every limit


@pytest.mark.parametrize("value", ["2h", "-5"])
def test_limits_reject_bad_values_by_name(value):
    with pytest.raises(ValueError, match="DEEPVARIANT_TIMEOUT_SECONDS"):
        dv.WrapperLimits.from_env({"DEEPVARIANT_TIMEOUT_SECONDS": value})


def test_stop_reason_checks_each_limit():
    limits = dv.WrapperLimits(timeout=100, idle=10, fatal_grace=5)
    assert dv.stop_reason(limits, now=50, started=0, last_output=45, fatal_at=None) is None
    assert "within 100 s" in dv.stop_reason(limits, now=101, started=0, last_output=100, fatal_at=None)
    assert "printed nothing for 10 s" in dv.stop_reason(limits, now=60, started=0, last_output=49, fatal_at=None)
    # after a fatal line: stopped only once it has also gone quiet for the grace period
    assert dv.stop_reason(limits, now=60, started=0, last_output=59, fatal_at=40) is None  # still printing
    assert "fatal error" in dv.stop_reason(limits, now=66, started=0, last_output=60, fatal_at=40)
    off = dv.WrapperLimits(fatal_grace=0)
    assert dv.stop_reason(off, now=10_000, started=0, last_output=0, fatal_at=1) is None


def test_fatal_pattern_ignores_tensorflow_allocator_warnings():
    warning = (
        "W external/local_tsl/tsl/framework/bfc_allocator.cc:296] Allocator (GPU_0_bfc) ran out of memory trying "
        "to allocate 2.68GiB with freed_by_count=0. The caller indicates that this is not a failure"
    )
    assert not dv.FATAL_RE.search(warning)  # seen in successful runs
    assert dv.FATAL_RE.search("tensorflow.python.framework.errors_impl.ResourceExhaustedError: OOM when allocating")
    assert dv.FATAL_RE.search("RESOURCE_EXHAUSTED: Out of memory while trying to allocate")


def test_run_streaming_stops_a_silent_wrapper():
    import time as _time

    started = _time.monotonic()
    with pytest.raises(dv.WrapperStopped, match="printed nothing"):
        dv.run_streaming(
            py("import time; print('start', flush=True); time.sleep(60)"),
            limits=dv.WrapperLimits(idle=1),
            poll=0.2,
        )
    assert _time.monotonic() - started < 30  # killed, not waited out


def test_run_streaming_stops_a_wrapper_that_goes_quiet_after_a_fatal_error():
    with pytest.raises(dv.WrapperStopped, match="fatal error"):
        dv.run_streaming(
            py("import time; print('ResourceExhaustedError: OOM', flush=True); time.sleep(60)"),
            limits=dv.WrapperLimits(fatal_grace=1),
            poll=0.2,
        )


def test_run_streaming_keeps_a_wrapper_that_still_prints_after_a_fatal_line():
    code, events, _ = dv.run_streaming(
        py(
            "import time\nprint('RESOURCE_EXHAUSTED (autotuning, recovered)', flush=True)\n"
            "for _ in range(12):\n    time.sleep(0.25); print('progress', flush=True)"
        ),
        limits=dv.WrapperLimits(fatal_grace=1),
        poll=0.1,
    )
    assert code == 0 and len(events) == 13  # 3 s of progress, well past the 1 s grace: still alive


def test_run_streaming_lets_a_wrapper_exit_by_itself_after_a_fatal_error():
    code, events, _ = dv.run_streaming(
        py("import sys; print('RESOURCE_EXHAUSTED', flush=True); sys.exit(1)"),
        limits=dv.WrapperLimits(fatal_grace=30),
        poll=0.2,
    )
    assert code == 1 and events[0][1] == "RESOURCE_EXHAUSTED"  # its own exit code, no WrapperStopped


def test_run_streaming_overall_timeout_even_while_printing():
    with pytest.raises(dv.WrapperStopped, match="did not finish within 1 s"):
        dv.run_streaming(
            py("import time\nwhile True:\n    print('tick', flush=True); time.sleep(0.1)"),
            limits=dv.WrapperLimits(timeout=1),
            poll=0.2,
        )


def test_run_streaming_returns_when_a_leftover_process_holds_the_pipe():
    import time as _time

    started = _time.monotonic()
    code, events, _ = dv.run_streaming(
        py(
            "import subprocess, sys\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])\n"
            "print('done', flush=True)"
        ),
        limits=dv.WrapperLimits(),
        poll=0.2,
        drain=1.0,
    )
    assert code == 0 and [line for _, line in events] == ["done"]
    assert _time.monotonic() - started < 15  # not stuck waiting for the leftover's 20 s


class FakeProc:
    def __init__(self, exited):
        self.pid, self.exited, self.waits = 4242, exited, 0

    def poll(self):
        return 0 if self.exited else None

    def wait(self, timeout=None):
        self.waits += 1
        return 0


def test_kill_process_group_signals_the_group_even_after_the_wrapper_exited(monkeypatch):
    sent = []
    monkeypatch.setattr(dv.os, "killpg", lambda pid, sig: sent.append((pid, sig)), raising=False)
    dv.kill_process_group(FakeProc(exited=True))
    assert [pid for pid, _ in sent] == [4242, 4242]  # TERM and KILL to the group: a stuck step may outlive it


def test_kill_process_group_stops_when_the_group_is_gone(monkeypatch):
    def gone(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(dv.os, "killpg", gone, raising=False)
    proc = FakeProc(exited=True)
    dv.kill_process_group(proc)
    assert proc.waits == 0


def test_stage_checks_limits_before_downloading(dv_inputs, monkeypatch):
    monkeypatch.setenv("DEEPVARIANT_IDLE_TIMEOUT_SECONDS", "soon")
    fetched = []
    monkeypatch.setattr(dv, "fetch_alignment", lambda *a, **k: fetched.append(a))
    with pytest.raises(ValueError, match="DEEPVARIANT_IDLE_TIMEOUT_SECONDS"):
        dv.deepvariant_task("S1", "ref.fa")
    assert fetched == []


def test_stage_fails_with_the_reason_when_the_wrapper_is_stopped(dv_inputs, monkeypatch):
    def stopped(cmd, **_):
        raise dv.WrapperStopped("run_deepvariant printed nothing for 900 s")

    monkeypatch.setattr(dv, "run_streaming", stopped)
    with pytest.raises(dv.WrapperStopped, match="printed nothing"):
        dv.deepvariant_task("S1", "ref.fa")


def test_run_local_passes_the_limits_through():
    import run_local

    for name in (*dv.LIMIT_VARS.values(), dv.GPU_SAMPLE_VAR):
        assert name in run_local.DEEPVARIANT_ENV_VARS


def test_run_streaming_cleans_up_the_group_after_a_normal_exit(monkeypatch):
    sent = []
    monkeypatch.setattr(dv.os, "killpg", lambda pid, sig: sent.append(pid), raising=False)
    code, _, _ = dv.run_streaming(py("print('ok', flush=True)"), limits=dv.WrapperLimits(), poll=0.2)
    assert code == 0 and len(sent) == 1  # stray steps of a finished run do not keep the GPU


# ---- resuming after make_examples (checkpoints) ----

PANGENOME_STEPS_OUTPUT = [
    (0.0, dv.BANNER),
    (0.0, 'time /opt/deepvariant/bin/load_gbz_into_shared_memory --pangenome_gbz "/w/g.gbz" --num_shards "6"'),
    (1.0, dv.BANNER),
    (1.0, "time seq 0 5 | parallel -q /opt/deepvariant/bin/make_examples_pangenome_aware_dv --examples /w/i/x@6.gz"),
    (9.0, dv.BANNER),
    (9.0, 'time /opt/deepvariant/bin/call_variants --outfile "/w/i/cvo.gz" --examples "/w/i/x@6.gz"'),
]


def test_step_commands_reads_each_step_after_its_banner():
    steps = dv.step_commands(SAMPLE_OUTPUT)
    assert [n for n, _ in steps] == ["make_examples", "call_variants", "postprocess_variants"]
    assert steps[1][1].startswith("/opt/deepvariant/bin/call_variants --outfile")  # 'time ' removed
    pg = dv.step_commands(PANGENOME_STEPS_OUTPUT)
    assert [n for n, _ in pg] == ["load_gbz_into_shared_memory", "make_examples", "call_variants"]


def test_make_examples_counts_as_finished_only_once_the_next_step_started():
    assert dv.make_examples_finished(dv.step_commands(SAMPLE_OUTPUT))
    assert dv.make_examples_finished(dv.step_commands(PANGENOME_STEPS_OUTPUT))
    assert not dv.make_examples_finished(dv.step_commands(SAMPLE_OUTPUT[:3]))  # still in make_examples


def test_checkpoints_can_be_turned_off(monkeypatch):
    assert dv.checkpoints_enabled()
    monkeypatch.setenv("DEEPVARIANT_CHECKPOINT", "0")
    assert not dv.checkpoints_enabled()


def option(cmd, name):
    """Value of --name=value in a wrapper command list."""
    return next(c.split("=", 1)[1] for c in cmd if c.startswith(f"--{name}="))


def wrapper_events(cmd):
    """Everything a wrapper prints for its three steps, with this run's paths (what --dry_run=true lists)."""
    inter, vcf = option(cmd, "intermediate_results_dir"), option(cmd, "output_vcf")
    return [
        (0.0, dv.BANNER),
        (0.0, f"time seq 0 1 | parallel /opt/deepvariant/bin/make_examples --examples {inter}/make_examples@2.gz"),
        (9.0, dv.BANNER),
        (9.0, f"time /opt/deepvariant/bin/call_variants --outfile {inter}/cvo.gz --examples {inter}/x@2.gz"),
        (10.0, dv.BANNER),
        (10.0, f"time /opt/deepvariant/bin/postprocess_variants --infile {inter}/cvo.gz --outfile {vcf}"),
    ]


def failing_wrapper(stopped=False, reach_call_variants=True):
    """A fake wrapper that writes make_examples' outputs, then fails or is stopped inside call_variants, as a real
    one does: so it never prints the postprocess_variants command (that comes from the --dry_run plan)."""

    def run(cmd, **_):
        inter = option(cmd, "intermediate_results_dir")
        for name in ("make_examples.tfrecord-00000-of-00002.gz", "make_examples.tfrecord-00001-of-00002.gz"):
            with open(f"{inter}/{name}", "wb") as fh:
                fh.write(b"examples")
        events = wrapper_events(cmd)[: 4 if reach_call_variants else 2]
        if reach_call_variants:
            events.append((9.5, "ResourceExhaustedError: OOM"))
        if stopped:
            raise dv.WrapperStopped("run_deepvariant reported a fatal error", events)
        return 1, events, 11.0

    return run


def replaying_steps(seen):
    """A fake run_streaming for the replayed steps: records each command; postprocess writes the VCF."""

    def run(cmd, **_):
        seen.append(cmd)
        assert cmd[:4] == ["bash", "-o", "pipefail", "-c"]
        if "postprocess_variants" in cmd[-1]:
            out = cmd[-1].split("--outfile ", 1)[1].split()[0]
            with open(out, "wb") as fh:
                fh.write(gzip.compress(b"##fileformat=VCFv4.2\n"))
        return 0, [(0.0, "step log")], 1.0

    return run


@pytest.fixture
def versioned(monkeypatch):
    """A known DeepVariant version, and the wrapper's --dry_run plan for the fake wrapper's steps."""
    monkeypatch.setattr(dv, "wrapper_version", lambda wrapper: "1.10.0")
    monkeypatch.setattr(dv, "planned_steps", lambda cmd: dv.step_commands(wrapper_events(cmd)))


def manifest(root, caller="deepvariant"):
    path = root / "intermediate" / "deepvariant-checkpoints" / "S1" / caller / "manifest.json"
    return json.loads(path.read_text()) if path.exists() else None


def test_failure_after_make_examples_saves_a_checkpoint(dv_inputs, local_env, versioned, monkeypatch):
    monkeypatch.setattr(dv, "run_streaming", failing_wrapper())
    with pytest.raises(RuntimeError, match="exited with code 1"):
        dv.deepvariant_task("S1", "ref.fa")
    m = manifest(local_env)
    assert m and not m["consumed"] and m["fingerprint"]["version"] == "1.10.0"
    assert [n for n, _ in m["steps"]] == ["call_variants", "postprocess_variants"]
    assert m["files"] == ["make_examples.tfrecord-00000-of-00002.gz", "make_examples.tfrecord-00001-of-00002.gz"]
    ckpt = local_env / "intermediate" / "deepvariant-checkpoints" / "S1" / "deepvariant"
    assert (ckpt / m["files"][0]).read_bytes() == b"examples"


def test_next_run_replays_only_the_remaining_steps_in_its_own_work_dir(dv_inputs, local_env, versioned, monkeypatch):
    monkeypatch.setattr(dv, "run_streaming", failing_wrapper())
    with pytest.raises(RuntimeError):
        dv.deepvariant_task("S1", "ref.fa")
    old_wd = manifest(local_env)["work_dir"]

    seen = []
    monkeypatch.setattr(dv, "run_streaming", replaying_steps(seen))
    dv.deepvariant_task("S1", "ref.fa")

    assert len(seen) == 2 and "call_variants" in seen[0][-1] and "postprocess_variants" in seen[1][-1]
    assert all(old_wd not in cmd[-1] for cmd in seen)  # paths moved to this run's work dir
    assert (local_env / "variants_deepvariant" / "S1.vcf.gz").exists()
    assert manifest(local_env)["consumed"]  # used once only

    # a third run starts from scratch: the consumed checkpoint is ignored
    full = []
    monkeypatch.setattr(dv, "run_streaming", lambda cmd, **_: full.append(cmd) or fake_run(b"x")(cmd))
    dv.deepvariant_task("S1", "ref.fa")
    assert full and full[0][0] == dv.RUN_DEEPVARIANT


def test_a_wrapper_stopped_after_make_examples_also_saves_a_checkpoint(dv_inputs, local_env, versioned, monkeypatch):
    monkeypatch.setattr(dv, "run_streaming", failing_wrapper(stopped=True))
    with pytest.raises(dv.WrapperStopped):
        dv.deepvariant_task("S1", "ref.fa")
    assert manifest(local_env) is not None


def test_no_checkpoint_when_make_examples_had_not_finished(dv_inputs, local_env, versioned, monkeypatch):
    monkeypatch.setattr(dv, "run_streaming", failing_wrapper(reach_call_variants=False))
    with pytest.raises(RuntimeError):
        dv.deepvariant_task("S1", "ref.fa")
    assert manifest(local_env) is None


def test_no_checkpoint_when_the_version_is_unknown(dv_inputs, local_env, monkeypatch):
    monkeypatch.setattr(dv, "wrapper_version", lambda wrapper: None)
    monkeypatch.setattr(dv, "run_streaming", failing_wrapper())
    with pytest.raises(RuntimeError):
        dv.deepvariant_task("S1", "ref.fa")
    assert manifest(local_env) is None


def test_no_checkpoint_when_turned_off(dv_inputs, local_env, versioned, monkeypatch):
    monkeypatch.setenv("DEEPVARIANT_CHECKPOINT", "0")
    monkeypatch.setattr(dv, "run_streaming", failing_wrapper())
    with pytest.raises(RuntimeError):
        dv.deepvariant_task("S1", "ref.fa")
    assert manifest(local_env) is None


def test_a_checkpoint_for_other_settings_is_not_used(dv_inputs, local_env, versioned, monkeypatch):
    monkeypatch.setattr(dv, "run_streaming", failing_wrapper())
    with pytest.raises(RuntimeError):
        dv.deepvariant_task("S1", "ref.fa")
    monkeypatch.setenv("DEEPVARIANT_EXTRA_ARGS", "--call_variants_extra_args=batch_size=256")
    full = []
    monkeypatch.setattr(dv, "run_streaming", lambda cmd, **_: full.append(cmd) or fake_run(b"x")(cmd))
    dv.deepvariant_task("S1", "ref.fa")
    assert full[0][0] == dv.RUN_DEEPVARIANT  # full run: the examples were made with other settings
    assert not manifest(local_env)["consumed"]


def test_a_failed_replay_keeps_the_checkpoint_for_another_try(dv_inputs, local_env, versioned, monkeypatch):
    monkeypatch.setattr(dv, "run_streaming", failing_wrapper())
    with pytest.raises(RuntimeError):
        dv.deepvariant_task("S1", "ref.fa")
    monkeypatch.setattr(dv, "run_streaming", lambda cmd, **_: (1, [], 0.0))
    with pytest.raises(RuntimeError, match="resumed steps exited with code 1"):
        dv.deepvariant_task("S1", "ref.fa")
    assert not manifest(local_env)["consumed"]


def test_step_commands_skip_a_stray_line_after_the_banner():
    events = [
        (0.0, dv.BANNER),
        (0.0, ""),
        (0.0, "I1102 late log"),
        (0.0, "time /opt/deepvariant/bin/call_variants --x"),
    ]
    assert dv.step_commands(events) == [("call_variants", "/opt/deepvariant/bin/call_variants --x")]


def test_file_identity_tells_same_size_files_apart(tmp_path):
    a, b = tmp_path / "a.cram", tmp_path / "b.cram"
    a.write_bytes(b"x" * 100)
    b.write_bytes(b"y" * 100)
    ia, ib = dv.file_identity(str(a)), dv.file_identity(str(b))
    assert ia[1] == ib[1] == 100 and ia[2] != ib[2]
    assert dv.file_identity(str(tmp_path / "missing")) is None


def test_checkpoint_keeps_every_make_examples_file_but_not_call_variants_output(
    dv_inputs, local_env, versioned, monkeypatch
):
    wrapper = failing_wrapper()

    def run(cmd, **kw):
        inter = option(cmd, "intermediate_results_dir")
        for name in ("gvcf.tfrecord-00000-of-00002.gz", "call_variants_output.tfrecord.gz"):
            with open(f"{inter}/{name}", "wb") as fh:
                fh.write(b"x")
        return wrapper(cmd, **kw)

    monkeypatch.setattr(dv, "run_streaming", run)
    with pytest.raises(RuntimeError):
        dv.deepvariant_task("S1", "ref.fa")
    files = manifest(local_env)["files"]
    assert "gvcf.tfrecord-00000-of-00002.gz" in files  # postprocess reads it with --output_gvcf
    assert not any(f.startswith("call_variants_output") for f in files)


def test_a_broken_checkpoint_falls_back_to_a_full_run_and_is_retired(dv_inputs, local_env, versioned, monkeypatch):
    monkeypatch.setattr(dv, "run_streaming", failing_wrapper())
    with pytest.raises(RuntimeError):
        dv.deepvariant_task("S1", "ref.fa")
    ckpt = local_env / "intermediate" / "deepvariant-checkpoints" / "S1" / "deepvariant"
    (ckpt / manifest(local_env)["files"][0]).unlink()  # e.g. expired by the lifecycle rule
    full = []
    monkeypatch.setattr(dv, "run_streaming", lambda cmd, **_: full.append(cmd) or fake_run(b"x")(cmd))
    dv.deepvariant_task("S1", "ref.fa")
    assert full[0][0] == dv.RUN_DEEPVARIANT
    retired = manifest(local_env)
    assert retired["consumed"] and retired["retired"].startswith("unusable")


def test_a_new_checkpoint_retires_the_old_manifest_before_writing_files(dv_inputs, local_env, versioned, monkeypatch):
    monkeypatch.setattr(dv, "run_streaming", failing_wrapper())
    with pytest.raises(RuntimeError):
        dv.deepvariant_task("S1", "ref.fa")
    order = []
    real_write, real_upload = dv.write_manifest, dv.storage().upload
    monkeypatch.setattr(
        dv, "write_manifest", lambda prefix, m: order.append(m.get("retired", "new")) or real_write(prefix, m)
    )
    monkeypatch.setattr(dv.storage(), "upload", lambda local, key: order.append("file") or real_upload(local, key))
    monkeypatch.setenv("DEEPVARIANT_EXTRA_ARGS", "--other")  # other settings: a full run that fails again
    with pytest.raises(RuntimeError):
        dv.deepvariant_task("S1", "ref.fa")
    assert order[0] == "being replaced" and order[-1] == "new" and "file" in order[1:-1]
    assert manifest(local_env)["fingerprint"]["extra_args"] == "--other"


def test_replayed_steps_share_one_overall_time_budget(monkeypatch):
    import time as _time

    seen = []

    def run(cmd, limits=None, **_):
        seen.append(limits.timeout)
        _time.sleep(1.2)
        return 0, [], 0.0

    monkeypatch.setattr(dv, "run_streaming", run)
    steps = [("call_variants", "cv"), ("postprocess_variants", "pp")]
    with pytest.raises(dv.WrapperStopped, match="resumed steps did not finish within 1 s"):
        dv.run_steps(steps, dv.WrapperLimits(timeout=1))
    assert len(seen) == 1 and seen[0] <= 1  # the second step got no fresh budget
    seen.clear()
    code, events, _ = dv.run_steps(steps, dv.WrapperLimits(timeout=10))
    assert code == 0 and seen[0] <= 10 and seen[1] < seen[0] - 1  # each step gets what is left
    assert [n for n, _ in dv.step_commands(events)] == []  # 'cv'/'pp' are not step binaries: nothing parsed


def test_remaining_steps_come_from_the_plan_when_the_run_matches_it():
    plan = [("make_examples", "me"), ("call_variants", "cv"), ("postprocess_variants", "pp")]
    started = plan[:2]  # stopped inside call_variants: postprocess never printed
    assert dv.remaining_steps(started, plan) == plan[1:]
    assert dv.remaining_steps(started, []) is None  # no plan: commands unknown
    assert dv.remaining_steps([("make_examples", "other"), ("call_variants", "cv")], plan) is None  # plan differs
    assert dv.remaining_steps(plan, []) == plan[1:]  # the run itself got to postprocess


def test_no_checkpoint_without_a_plan_when_stopped_in_call_variants(dv_inputs, local_env, versioned, monkeypatch):
    monkeypatch.setattr(dv, "planned_steps", lambda cmd: [])
    monkeypatch.setattr(dv, "run_streaming", failing_wrapper(stopped=True))
    with pytest.raises(dv.WrapperStopped):
        dv.deepvariant_task("S1", "ref.fa")
    assert manifest(local_env) is None  # a checkpoint without postprocess_variants could never finish


def test_planned_steps_parse_a_dry_run(monkeypatch):
    class Done:
        returncode = 0
        stdout = (
            "I1102 ... intermediate\n\n***** Running the command:*****\ntime /opt/deepvariant/bin/call_variants --x\n"
        )
        stderr = ""

    seen = []
    monkeypatch.setattr(dv.subprocess, "run", lambda cmd, **kw: seen.append(cmd) or Done())
    assert dv.planned_steps(["/opt/deepvariant/bin/run_deepvariant", "--ref=r"]) == [
        ("call_variants", "/opt/deepvariant/bin/call_variants --x")
    ]
    assert seen[0][-1] == "--dry_run=true"


def test_a_checkpoint_without_postprocess_is_retired_not_replayed(dv_inputs, local_env, versioned, monkeypatch):
    monkeypatch.setattr(dv, "run_streaming", failing_wrapper())
    with pytest.raises(RuntimeError):
        dv.deepvariant_task("S1", "ref.fa")
    path = local_env / "intermediate" / "deepvariant-checkpoints" / "S1" / "deepvariant" / "manifest.json"
    m = json.loads(path.read_text())
    m["steps"] = m["steps"][:1]  # only call_variants: what the first version saved for a run stopped in it
    path.write_text(json.dumps(m))
    full = []
    monkeypatch.setattr(dv, "run_streaming", lambda cmd, **_: full.append(cmd) or fake_run(b"x")(cmd))
    dv.deepvariant_task("S1", "ref.fa")
    assert full[0][0] == dv.RUN_DEEPVARIANT and manifest(local_env)["retired"].startswith("unusable")


# ---- GPU utilisation and memory (#139) ----
# 39 s of `nvidia-smi ... -lms 1000` on the RTX 3080 around the start of call_variants (HG003 chr20, 3 Nov 2026):
# the end of make_examples (1.8 GB, idle), TensorFlow reserving the free memory, then the model running.
GPU_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "deepvariant", "nvidia-smi-loop.txt")


def recorded_gpu_lines():
    with open(GPU_FIXTURE, encoding="utf-8") as fh:
        return fh.read().splitlines()


def test_parse_gpu_line_reads_recorded_nvidia_smi_output():
    parsed = [s for s in map(dv.parse_gpu_line, recorded_gpu_lines()) if s]
    assert len(parsed) >= 10, "fixture has too few samples"
    for index, util, used, total in parsed:
        assert index == 0 and total == 10240.0
        assert 0 <= util <= 100 and 0 < used <= total
    assert max(u for _, u, _, _ in parsed) >= 50  # the fixture covers call_variants busy on the GPU
    used = [m for _, _, m, _ in parsed]
    assert min(used) < 2000 and max(used) > 9000  # and TensorFlow reserving nearly all of the 10 GB


@pytest.mark.parametrize(
    "line, expected",
    [
        ("0, 97, 9581, 10240", (0, 97.0, 9581.0, 10240.0)),
        ("1, [N/A], 512, 24576", (1, None, 512.0, 24576.0)),  # a GPU that doesn't report utilisation
        ("0, [Not Supported], [Not Supported], 15360", None),  # nothing to learn from
        ("", None),
        ("Failed to initialize NVML: Driver/library version mismatch", None),
        ("index, utilization.gpu [%], memory.used [MiB], memory.total [MiB]", None),  # a header, if ever printed
    ],
)
def test_parse_gpu_line_skips_what_is_not_a_sample(line, expected):
    assert dv.parse_gpu_line(line) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, dv.GPU_SAMPLE_DEFAULT),
        ("", dv.GPU_SAMPLE_DEFAULT),
        ("0", 0.0),
        ("0.5", 0.5),
        ("x", 0.0),
        ("-1", 0.0),
        ("nan", 0.0),
        ("inf", 0.0),
    ],
)
def test_gpu_sample_interval(value, expected):
    env = {} if value is None else {dv.GPU_SAMPLE_VAR: value}
    assert dv.gpu_sample_interval(env) == expected


# Samples at the times of SAMPLE_OUTPUT's steps: make_examples 100-130.5, call_variants 130.5-151, postprocess 151-156.
FAKE_SAMPLES = [
    (110.0, 0, 0.0, 300.0, 10240.0),
    (120.0, 0, 2.0, 300.0, 10240.0),
    (135.0, 0, 80.0, 9500.0, 10240.0),
    (145.0, 0, 100.0, 9600.0, 10240.0),
    (153.0, 0, 0.0, 400.0, 10240.0),
]


def test_summarise_gpu_overall_and_per_step():
    summary = dv.summarise_gpu(FAKE_SAMPLES, dv.parse_step_windows(SAMPLE_OUTPUT, 156.0))
    assert summary["gpus"] == 1 and summary["memory_total_mib"] == 10240.0 and summary["samples"] == 5
    assert summary["utilization_mean_pct"] == 36.4 and summary["utilization_peak_pct"] == 100.0
    assert summary["memory_peak_mib"] == 9600.0
    assert summary["steps"] == {
        "make_examples": {
            "samples": 2,
            "utilization_mean_pct": 1.0,
            "utilization_peak_pct": 2.0,
            "memory_peak_mib": 300.0,
        },
        "call_variants": {
            "samples": 2,
            "utilization_mean_pct": 90.0,
            "utilization_peak_pct": 100.0,
            "memory_peak_mib": 9600.0,
        },
        "postprocess_variants": {
            "samples": 1,
            "utilization_mean_pct": 0.0,
            "utilization_peak_pct": 0.0,
            "memory_peak_mib": 400.0,
        },
    }


def test_summarise_gpu_without_samples_is_none():
    assert dv.summarise_gpu([], dv.parse_step_windows(SAMPLE_OUTPUT, 156.0)) is None


def test_step_windows_and_timings_agree():
    windows = dv.parse_step_windows(SAMPLE_OUTPUT, 156.0)
    assert [w[0] for w in windows] == ["make_examples", "call_variants", "postprocess_variants"]
    assert windows[1] == ("call_variants", 130.5, 151.0)


def test_sampler_does_nothing_without_nvidia_smi(monkeypatch):
    monkeypatch.setattr(dv.shutil, "which", lambda name: None)
    sampler = dv.GpuSampler(1.0).start()
    sampler.stop()
    sampler.stop()  # safe twice
    assert sampler.samples == []


def test_sampler_is_off_at_interval_zero(monkeypatch):
    started = []
    monkeypatch.setattr(dv.subprocess, "Popen", lambda *a, **k: started.append(a))
    sampler = dv.GpuSampler(0).start()
    sampler.stop()
    assert started == [] and sampler.samples == []


def test_sampler_reads_a_looping_nvidia_smi(monkeypatch):
    # A stand-in for `nvidia-smi -lms`: prints the recorded lines, then keeps running until stopped.
    script = (
        "import sys, time\n"
        f"for line in open({GPU_FIXTURE!r}, encoding='utf-8'):\n"
        "    print(line, end='', flush=True)\n"
        "time.sleep(60)\n"
    )
    monkeypatch.setattr(dv.GpuSampler, "command", lambda self: [sys.executable, "-c", script])
    sampler = dv.GpuSampler(0.1).start()
    expected = len([line for line in recorded_gpu_lines() if dv.parse_gpu_line(line)])
    deadline = time.monotonic() + 20
    while len(sampler.samples) < expected and time.monotonic() < deadline:
        time.sleep(0.05)
    started = time.monotonic()
    sampler.stop()
    assert time.monotonic() - started < 15  # stopped, not waited out
    assert len(sampler.samples) == expected
    assert all(ts > 0 and index == 0 for ts, index, *_ in sampler.samples)


def test_sample_on_a_step_boundary_counts_once():
    windows = [("make_examples", 0.0, 10.0), ("call_variants", 10.0, 20.0)]
    samples = [(10.0, 0, 50.0, 100.0, 1000.0), (20.0, 0, 70.0, 100.0, 1000.0)]
    steps = dv.summarise_gpu(samples, windows)["steps"]
    assert "make_examples" not in steps and steps["call_variants"]["samples"] == 2  # the last step keeps its end


def test_sampler_never_raises_on_a_strange_interval(monkeypatch):
    for interval in (float("nan"), float("inf")):
        sampler = dv.GpuSampler(interval).start()
        sampler.stop()
        assert sampler.samples == []


def test_sampler_stopped_at_once_does_not_trip_its_reader(monkeypatch, capsys):
    script = "import time\nprint('0, 5, 100, 1000', flush=True)\ntime.sleep(60)\n"
    monkeypatch.setattr(dv.GpuSampler, "command", lambda self: [sys.executable, "-c", script])
    sampler = dv.GpuSampler(1.0).start()
    sampler.stop()
    assert "GPU sampling stopped" not in capsys.readouterr().out


def test_recorded_interval_is_the_one_nvidia_smi_uses():
    assert dv.GpuSampler(0.01).effective_interval == 0.1
    assert dv.GpuSampler(2.0).effective_interval == 2.0
    assert dv.GpuSampler(0).effective_interval is None


def test_sampler_command_loops_in_milliseconds():
    cmd = dv.GpuSampler(2.0).command()
    assert cmd[0] == "nvidia-smi" and "--format=csv,noheader,nounits" in cmd
    assert cmd[-2:] == ["-lms", "2000"] and f"--query-gpu={dv.GPU_QUERY}" in cmd


class FakeSampler:
    def __init__(self, interval):
        self.interval = interval
        self.samples = list(FAKE_SAMPLES) if interval else []
        self.effective_interval = interval or None
        self.stopped = 0

    def start(self):
        return self

    def stop(self):
        self.stopped += 1


def dv_part(root):
    return json.loads((root / "runs" / "S1.provenance" / "DeepVariant.json").read_text())


def test_stage_records_gpu_use_in_the_run_record_and_step_metrics(local_env, dv_inputs, monkeypatch):
    monkeypatch.setenv(dv.GPU_SAMPLE_VAR, "2")
    monkeypatch.setattr(dv, "GpuSampler", FakeSampler)
    monkeypatch.setattr(dv, "run_streaming", fake_run(gzip.compress(b"##fileformat=VCFv4.2\n")))

    dv.deepvariant_task("S1", "ref.fa")

    gpu = dv_part(local_env)["gpu"]
    assert gpu["model"] == "Fake GPU" and gpu["sample_seconds"] == 2.0 and gpu["memory_peak_mib"] == 9600.0
    assert gpu["steps"]["call_variants"]["utilization_mean_pct"] == 90.0
    by_task = {m["task"]: m for m in metrics_lines(local_env)}
    call = by_task["DeepVariant.CallVariants"]
    assert (call["gpu_utilization_mean_pct"], call["gpu_utilization_peak_pct"], call["gpu_memory_peak_mib"]) == (
        90.0,
        100.0,
        9600.0,
    )
    assert by_task["DeepVariant.MakeExamples"]["gpu_utilization_mean_pct"] == 1.0  # the GPU idles here
    assert "gpu_utilization_mean_pct" not in by_task["DeepVariant"]  # the stage metric keeps its CPU usage only


def test_stage_without_a_gpu_records_no_gpu_figures(local_env, dv_inputs, monkeypatch):
    monkeypatch.setattr(dv, "gpu_name", lambda: None)  # CPU image / CI: no nvidia-smi
    made = []
    monkeypatch.setattr(dv, "GpuSampler", lambda interval: made.append(interval) or FakeSampler(interval))
    monkeypatch.setattr(dv, "run_streaming", fake_run(gzip.compress(b"##fileformat=VCFv4.2\n")))

    dv.deepvariant_task("S1", "ref.fa")

    assert made == [0]  # sampling off
    assert "gpu" not in dv_part(local_env)
    assert not any(k.startswith("gpu_") for m in metrics_lines(local_env) for k in m)


def test_gpu_model_is_recorded_even_with_sampling_off(local_env, dv_inputs, monkeypatch):
    monkeypatch.setattr(dv, "run_streaming", fake_run(gzip.compress(b"##fileformat=VCFv4.2\n")))
    dv.deepvariant_task("S1", "ref.fa")  # dv_inputs sets the interval to 0
    assert dv_part(local_env)["gpu"] == {"model": "Fake GPU", "sample_seconds": None, "samples": 0}


def test_a_stopped_wrapper_still_records_gpu_use(local_env, dv_inputs, monkeypatch):
    # e.g. call_variants out of GPU memory and hung: the peak memory is the evidence.
    monkeypatch.setenv(dv.GPU_SAMPLE_VAR, "2")
    monkeypatch.setenv("DEEPVARIANT_CHECKPOINT", "0")
    samplers = []
    monkeypatch.setattr(dv, "GpuSampler", lambda interval: samplers.append(FakeSampler(interval)) or samplers[-1])

    def stopped(cmd, **_):
        raise dv.WrapperStopped("call_variants went quiet", SAMPLE_OUTPUT[:5])

    monkeypatch.setattr(dv, "run_streaming", stopped)
    with pytest.raises(dv.WrapperStopped):
        dv.deepvariant_task("S1", "ref.fa")
    part = dv_part(local_env)
    assert part["status"] == "Failure" and part["gpu"]["memory_peak_mib"] == 9600.0
    assert samplers[0].stopped >= 1


def test_a_broken_gpu_summary_never_fails_the_stage(local_env, dv_inputs, monkeypatch, capsys):
    monkeypatch.setenv(dv.GPU_SAMPLE_VAR, "2")
    monkeypatch.setattr(dv, "GpuSampler", FakeSampler)
    monkeypatch.setattr(dv, "summarise_gpu", lambda *a: 1 / 0)
    monkeypatch.setattr(dv, "run_streaming", fake_run(gzip.compress(b"##fileformat=VCFv4.2\n")))
    dv.deepvariant_task("S1", "ref.fa")
    assert "could not summarise GPU use" in capsys.readouterr().out
    assert (local_env / "variants_deepvariant" / "S1.vcf.gz").exists()


def test_gpu_metric_usage_drops_missing_values():
    assert dv.gpu_metric_usage(None) is None
    assert dv.gpu_metric_usage({"utilization_mean_pct": None, "memory_peak_mib": 512.0}) == {
        "gpu_memory_peak_mib": 512.0
    }


def test_stage_uploads_the_index_deepvariant_wrote_beside_the_vcf(local_env, dv_inputs, monkeypatch):
    vcf = gzip.compress(b"##fileformat=VCFv4.2\n")
    monkeypatch.setattr(dv, "run_streaming", fake_run(vcf, tbi_bytes=b"TBI"))
    dv.deepvariant_task("S1", "ref.fa")
    assert (local_env / "variants_deepvariant" / "S1.vcf.gz.tbi").read_bytes() == b"TBI"
    assert (local_env / "variants_deepvariant" / "S1.vcf.gz").read_bytes() == vcf


def test_a_missing_index_is_built_and_a_failed_build_only_warns(local_env, dv_inputs, monkeypatch, capsys):
    vcf = gzip.compress(b"##fileformat=VCFv4.2\n")
    monkeypatch.setattr(dv, "run_streaming", fake_run(vcf))
    built = []

    def fake_index(vcf_path, tbi):
        built.append((vcf_path, tbi))
        with open(tbi, "wb") as fh:
            fh.write(b"BUILT")

    monkeypatch.setattr(dv, "build_vcf_index", fake_index)
    dv.deepvariant_task("S1", "ref.fa")
    assert built and built[0][1] == built[0][0] + ".tbi"
    assert (local_env / "variants_deepvariant" / "S1.vcf.gz.tbi").read_bytes() == b"BUILT"

    def no_bcftools(vcf_path, tbi):
        raise FileNotFoundError("bcftools")

    monkeypatch.setattr(dv, "build_vcf_index", no_bcftools)
    (local_env / "variants_deepvariant" / "S1.vcf.gz.tbi").unlink()
    dv.deepvariant_task("S1", "ref.fa")
    assert (local_env / "variants_deepvariant" / "S1.vcf.gz").exists()  # the VCF still lands
    assert not (local_env / "variants_deepvariant" / "S1.vcf.gz.tbi").exists()
    assert "uploading the VCF without a .tbi" in capsys.readouterr().out
