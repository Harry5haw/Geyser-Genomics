"""The provenance run record (app/provenance.py): parts per stage, the assembled record, best effort."""

import hashlib
import json

import pytest

import provenance
from storage import LocalStorage


@pytest.fixture
def store(tmp_path, monkeypatch):
    for var in ("RUN_NAME", "APP_VERSION", "GEYSER_BWA_MEM2_RELEASE", "GEYSER_MOSDEPTH_RELEASE", "PIPELINE_CALLER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(provenance, "_current", None)
    return LocalStorage(str(tmp_path / "bucket"))


def write_part(store, task, status="Success", run_name=None, finished="2026-11-02T10:00:00Z", **fields):
    part = {
        "schema": 1,
        "task": task,
        "sample_id": "S1",
        "run_name": run_name,
        "app_version": "dev-local",
        "started": finished,
        "finished": finished,
        "duration_seconds": 1.0,
        "status": status,
        "error": None,
        "inputs": [],
        "outputs": [],
        "tools": {},
        "stats": {},
        **fields,
    }
    provenance._put_json(store, f"runs/S1.provenance/{task}.json", part)


def read(store, key):
    with store.open_read(key) as fh:
        return json.loads(fh.read())


def test_a_stage_writes_its_part_and_the_record(store, tmp_path, monkeypatch):
    monkeypatch.setenv("RUN_NAME", "S1-20261102T100000Z-abc")
    monkeypatch.setenv("APP_VERSION", "dev-local")
    reads = tmp_path / "S1.fastq.gz"
    reads.write_bytes(b"x" * 10)

    provenance.begin("QualityControl", "S1")
    provenance.note(layout="single")
    provenance.note_input("raw_reads/S1.fastq.gz", str(reads))
    provenance.note_output("qc_reports/S1.fastp.html", str(reads))
    provenance.note_stats("flagstat", {"mapped_pct": 100.0})
    provenance.finish(store, "Success", 1.234)

    part = read(store, "runs/S1.provenance/QualityControl.json")
    assert part["status"] == "Success" and part["duration_seconds"] == 1.23 and part["layout"] == "single"
    assert part["inputs"] == [{"key": "raw_reads/S1.fastq.gz", "bytes": 10}]
    record = read(store, "runs/S1.provenance.json")
    assert record["run_name"] == "S1-20261102T100000Z-abc" and record["app_version"] == "dev-local"
    assert record["status"] == "incomplete" and not record["complete"]  # only one of the three stages so far
    assert record["stats"] == {"flagstat": {"mapped_pct": 100.0}}
    assert provenance.read_record(store, "S1") == record


def test_all_core_stages_succeeding_is_a_complete_record(store):
    for i, task in enumerate(provenance.CORE_STAGES):
        write_part(store, task, finished=f"2026-11-02T10:0{i}:00Z", outputs=[{"key": f"out/{task}", "bytes": 1}])
    record = provenance.assemble(store, "S1")
    assert record["status"] == "succeeded" and record["complete"]
    assert [o["key"] for o in record["outputs"]] == sorted(f"out/{t}" for t in provenance.CORE_STAGES)


def test_any_failed_stage_makes_the_record_failed(store):
    write_part(store, "QualityControl")
    write_part(store, "Align", status="Failure", error="CalledProcessError: ...")
    record = provenance.assemble(store, "S1")
    assert record["status"] == "failed" and record["stages"]["Align"]["error"].startswith("CalledProcessError")


def test_only_the_latest_runs_parts_are_assembled(store):
    # An old run's variants part must not make a new, still-running run look complete.
    write_part(store, "CallVariants", run_name="old", finished="2026-11-01T09:00:00Z")
    write_part(store, "QualityControl", run_name="new", finished="2026-11-02T10:00:00Z")
    record = provenance.assemble(store, "S1")
    assert record["run_name"] == "new" and set(record["stages"]) == {"QualityControl"}


def test_later_stages_win_field_clashes(store):
    write_part(store, "QualityControl", finished="2026-11-02T10:00:00Z", layout="single", tools={"samtools": "1.20"})
    write_part(
        store,
        "Align",
        finished="2026-11-02T10:05:00Z",
        aligner="bwa-mem2",
        tools={"samtools": "1.21"},
        reference={"name": "r.fa", "md5": "abc", "bytes": 1},
    )
    record = provenance.assemble(store, "S1")
    assert record["tools"] == {"samtools": "1.21"} and record["aligner"] == "bwa-mem2"
    assert record["layout"] == "single" and record["reference"]["md5"] == "abc"


def test_finish_never_raises(store, monkeypatch, capsys):
    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store, "upload_stream", broken)
    provenance.begin("Align", "S1")
    provenance.finish(store, "Success", 1.0)  # must not raise
    assert "WARNING: provenance: could not write the run record: disk full" in capsys.readouterr().out


def test_notes_without_a_stage_are_ignored(store):
    provenance.note(layout="single")
    provenance.note_input("raw_reads/S1.fastq.gz")
    provenance.finish(store, "Success", 1.0)
    assert list(store.list_keys("runs/")) == []


def test_reference_md5_is_the_files(store, tmp_path):
    ref = tmp_path / "ref.fa"
    ref.write_bytes(b">c\nACGT\n")
    provenance.begin("Align", "S1")
    provenance.note_reference("ref.fa", str(ref))
    assert provenance._current["reference"] == {
        "name": "ref.fa",
        "md5": hashlib.md5(b">c\nACGT\n").hexdigest(),
        "bytes": 8,
    }


class FakePopen:
    outputs = {}

    def __init__(self, command, stdout=None, stderr=None):
        if command[0] not in self.outputs:
            raise FileNotFoundError(command[0])
        self.out = self.outputs[command[0]]

    def communicate(self, timeout=None):
        return self.out, None


@pytest.mark.parametrize(
    "tool, output, expected",
    [
        ("samtools", b"samtools 1.21\nUsing htslib 1.21\n", "1.21"),
        ("bcftools", b"bcftools 1.21\nUsing htslib 1.21\n", "1.21"),
        # The launcher prints its choice first; the version is the bare line.
        ("bwa-mem2", b'Looking to launch executable "/opt/bwa-mem2/bwa-mem2.avx2", simd = .avx2\n2.2.1\n', "2.2.1"),
        (
            "bwa",
            b"\nProgram: bwa (alignment via Burrows-Wheeler transformation)\nVersion: 0.7.18-r1243\n",
            "0.7.18-r1243",
        ),
        ("fastp", b"fastp 0.24.0\n", "0.24.0"),  # what the geyser-app image's fastp prints (to stderr)
        ("mosdepth", b"mosdepth 0.3.15\n", "0.3.15"),
        ("samtools", b"something else\n", None),
    ],
)
def test_tool_versions_are_parsed_from_real_outputs(monkeypatch, tool, output, expected):
    monkeypatch.setattr(provenance.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(FakePopen, "outputs", {tool: output})
    provenance.tool_version.cache_clear()
    assert provenance.tool_version(tool) == expected
    provenance.tool_version.cache_clear()


def test_missing_tool_is_none_and_the_pinned_release_is_recorded(store, monkeypatch):
    monkeypatch.setattr(provenance.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(FakePopen, "outputs", {})
    monkeypatch.setenv("GEYSER_BWA_MEM2_RELEASE", "2.3")
    provenance.tool_version.cache_clear()
    provenance.begin("Align", "S1")
    provenance.note_tools("bwa-mem2")
    assert provenance._current["tools"] == {"bwa-mem2": None, "bwa-mem2 release": "2.3"}
    provenance.tool_version.cache_clear()


def test_summary_is_the_short_form():
    record = {
        "status": "failed",
        "complete": False,
        "run_name": "r",
        "app_version": "v",
        "stages": {"Align": {"status": "Failure", "duration_seconds": 3}},
    }
    assert provenance.summary(record) == {
        "status": "failed",
        "complete": False,
        "run_name": "r",
        "app_version": "v",
        "stages": {"Align": "Failure"},
        "stage_runs": {"Align": None},
        "failed_stage": None,  # set when the state machine closed the run (test_finalize.py)
    }


def test_a_deepvariant_only_run_inherits_the_core_run(store):
    # run_local --caller deepvariant --skip-to-variants after a full bcftools run: an add-on, not a new run.
    for task in provenance.CORE_STAGES:
        write_part(store, task, run_name="core", finished="2026-11-02T10:00:00Z")
    write_part(store, "DeepVariant", run_name="dv", finished="2026-11-02T11:00:00Z")
    record = provenance.build(store, "S1")
    assert record["complete"] and record["status"] == "succeeded" and record["run_name"] == "dv"
    assert set(record["stages"]) == {*provenance.CORE_STAGES, "DeepVariant"}


def test_a_failed_extra_caller_does_not_fail_the_core_run(store):
    for task in provenance.CORE_STAGES:
        write_part(store, task, run_name="r")
    write_part(store, "DeepVariant", status="Failure", run_name="r")
    record = provenance.build(store, "S1")
    assert record["status"] == "succeeded" and record["stages"]["DeepVariant"]["status"] == "Failure"


def test_build_uses_given_part_keys_without_listing(store, monkeypatch):
    write_part(store, "QualityControl")

    def no_listing(prefix=""):
        raise AssertionError("listed again")

    monkeypatch.setattr(store, "list_keys", no_listing)
    assert provenance.build(store, "S1", ["runs/S1.provenance/QualityControl.json"])["stages"]


def test_a_variants_only_run_inherits_its_upstream_stages(store):
    # run_local --skip-to-variants: a new run name with only CallVariants, on an earlier run's alignment.
    write_part(store, "QualityControl", run_name="first", finished="2026-11-02T10:00:00Z")
    write_part(store, "Align", run_name="first", finished="2026-11-02T10:01:00Z")
    write_part(store, "CallVariants", run_name="second", finished="2026-11-02T11:00:00Z")
    record = provenance.build(store, "S1")
    assert record["run_name"] == "second" and record["status"] == "succeeded" and record["complete"]
    assert record["stages"]["Align"]["run_name"] == "first" and record["stages"]["CallVariants"]["run_name"] == "second"


def test_readers_assemble_from_the_parts_not_the_stored_copy(store):
    # Two stages finishing at once can leave the stored copy one part behind; the parts can't race.
    write_part(store, "QualityControl")
    provenance._put_json(store, "runs/S1.provenance.json", {"status": "stale copy"})
    assert provenance.read_record(store, "S1")["stages"] == {
        "QualityControl": {
            "status": "Success",
            "started": "2026-11-02T10:00:00Z",
            "finished": "2026-11-02T10:00:00Z",
            "duration_seconds": 1.0,
            "error": None,
            "run_name": None,
            "resources": None,
        }
    }


def test_the_stored_copy_is_used_only_without_parts(store):
    provenance._put_json(store, "runs/S1.provenance.json", {"status": "succeeded"})
    assert provenance.read_record(store, "S1") == {"status": "succeeded"}
    assert provenance.read_record(store, "NOPE") is None


def test_unknown_tool_is_none_not_an_error(store):
    assert provenance.tool_version("minimap2") is None


def test_a_hung_version_command_is_killed(monkeypatch):
    killed = []

    class Hung:
        def __init__(self, *args, **kwargs):
            self.calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise provenance.subprocess.TimeoutExpired("samtools", timeout)
            return b"", None

        def kill(self):
            killed.append(True)

    monkeypatch.setattr(provenance.subprocess, "Popen", Hung)
    provenance.tool_version.cache_clear()
    assert provenance.tool_version("samtools") is None and killed == [True]
    provenance.tool_version.cache_clear()


# --- the run's caller completes the core pipeline (#158) -------------------------------------------


def dv_only_run(store, dv_status="Success", run="R1", caller="deepvariant", task="DeepVariant"):
    """QC and align, then the caller's own stage (PIPELINE_CALLER set, as the state machine does)."""
    write_part(store, "QualityControl", run_name=run, finished="2026-11-02T10:00:00Z")
    write_part(store, "Align", run_name=run, finished="2026-11-02T10:01:00Z")
    write_part(store, task, status=dv_status, run_name=run, finished="2026-11-02T10:05:00Z", caller=caller)


def test_a_bcftools_run_is_complete_with_its_three_core_stages(store):
    for i, task in enumerate(provenance.CORE_STAGES):
        write_part(store, task, run_name="R1", finished=f"2026-11-02T10:0{i}:00Z")
    record = provenance.build(store, "S1")
    assert (record["status"], record["complete"]) == ("succeeded", True)
    assert "caller" not in record


def test_a_deepvariant_only_run_is_complete_without_call_variants(store):
    dv_only_run(store)
    record = provenance.build(store, "S1")
    assert (record["status"], record["complete"]) == ("succeeded", True)
    assert record["caller"] == "deepvariant"
    assert "CallVariants" not in record["stages"]


def test_a_pangenome_only_run_is_complete_too(store):
    dv_only_run(store, caller="pangenome", task="PangenomeDeepVariant")
    assert provenance.build(store, "S1")["status"] == "succeeded"


def test_a_failed_deepvariant_only_run_is_failed(store):
    dv_only_run(store, dv_status="Failure")
    record = provenance.build(store, "S1")
    assert (record["status"], record["complete"]) == ("failed", False)


def test_the_runs_own_bcftools_fallback_completes_it(store):
    # The cloud: DeepVariant failed, the state machine called the sample with bcftools instead.
    dv_only_run(store, dv_status="Failure")
    write_part(store, "CallVariants", run_name="R1", finished="2026-11-02T10:20:00Z")
    record = provenance.build(store, "S1")
    assert (record["status"], record["complete"]) == ("succeeded", True)


def test_an_earlier_runs_call_variants_does_not_complete_a_failed_deepvariant_run(store):
    write_part(store, "CallVariants", run_name="R0", finished="2026-11-01T09:00:00Z")
    dv_only_run(store, dv_status="Failure", run="R1")
    record = provenance.build(store, "S1")
    assert "CallVariants" not in record["stages"], "a run with its own caller inherits no CallVariants"
    assert (record["status"], record["complete"]) == ("failed", False)


def test_a_deepvariant_only_run_still_calling_is_incomplete(store):
    write_part(store, "QualityControl", run_name="R1", finished="2026-11-02T10:00:00Z")
    write_part(store, "Align", run_name="R1", finished="2026-11-02T10:01:00Z")
    assert provenance.build(store, "S1")["status"] == "incomplete"


def test_both_callers_keep_the_bcftools_rule(store):
    # run_local --caller both: no PIPELINE_CALLER; DeepVariant is an extra caller, as before.
    for i, task in enumerate(provenance.CORE_STAGES):
        write_part(store, task, run_name="R1", finished=f"2026-11-02T10:0{i}:00Z")
    write_part(store, "DeepVariant", status="Failure", run_name="R1", finished="2026-11-02T10:09:00Z")
    assert provenance.build(store, "S1")["status"] == "succeeded"
    write_part(store, "CallVariants", status="Failure", run_name="R1", finished="2026-11-02T10:10:00Z")
    assert provenance.build(store, "S1")["status"] == "failed"


def test_a_caller_added_to_an_earlier_run_keeps_the_bcftools_rule(store):
    # --skip-to-variants --caller deepvariant: no PIPELINE_CALLER, the core stages come from run R0.
    for i, task in enumerate(provenance.CORE_STAGES):
        write_part(store, task, run_name="R0", finished=f"2026-11-01T10:0{i}:00Z")
    write_part(store, "DeepVariant", status="Failure", run_name="R1", finished="2026-11-02T10:00:00Z")
    record = provenance.build(store, "S1")
    assert record["status"] == "succeeded" and "caller" not in record


def test_begin_records_the_runs_caller(store, monkeypatch):
    monkeypatch.setenv("PIPELINE_CALLER", "DeepVariant")
    provenance.begin("DeepVariant", "S1")
    assert provenance._current["caller"] == "deepvariant"
    monkeypatch.delenv("PIPELINE_CALLER")
    provenance.begin("CallVariants", "S1")
    assert provenance._current["caller"] is None


def test_without_run_names_the_latest_variant_attempt_decides(store):
    # Manual runs set no RUN_NAME: every part counts as one run. An old successful bcftools call must not
    # rescue a later failed DeepVariant-only run...
    write_part(store, "QualityControl", finished="2026-11-02T09:00:00Z")
    write_part(store, "Align", finished="2026-11-02T09:01:00Z")
    write_part(store, "CallVariants", finished="2026-11-02T09:02:00Z")
    write_part(store, "DeepVariant", status="Failure", finished="2026-11-02T10:00:00Z", caller="deepvariant")
    assert provenance.build(store, "S1")["status"] == "failed"
    # ...and an old successful DeepVariant-only run must not rescue a later failed bcftools call.
    write_part(store, "DeepVariant", finished="2026-11-02T10:00:00Z", caller="deepvariant")
    write_part(store, "CallVariants", status="Failure", finished="2026-11-02T11:00:00Z")
    assert provenance.build(store, "S1")["status"] == "failed"


def test_a_gate_rerun_keeps_a_deepvariant_only_runs_caller(store):
    dv_only_run(store, run="R1")
    write_part(store, "QualityGate", run_name="R2", finished="2026-11-02T12:00:00Z")
    record = provenance.build(store, "S1")
    assert (record["status"], record["caller"]) == ("succeeded", "deepvariant")
    assert record["stages"]["DeepVariant"]["run_name"] == "R1"


def test_the_record_names_only_a_known_caller(store):
    dv_only_run(store, caller="bcftools")  # not a caller with a stage of its own: the bcftools rule
    record = provenance.build(store, "S1")
    assert "caller" not in record and record["status"] == "incomplete"
