"""Long reads (READ_TYPE=hifi|ont, ALIGNER=minimap2): aligner choice, presets, layout, duplicate marking,
mpileup's error model, DeepVariant's model type, the run record, run_local and make_test_data --long-reads.
The real tools run in tests/test_e2e_docker.py (test_long_reads_*)."""

import gzip
import json
import os
import shlex
import statistics

import pytest

import tasks
from conftest import load_module

REF = "ref.fa"


def put(root, key, data=b"x"):
    path = root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


# --- READ_TYPE and the aligner ----------------------------------------------------------------------------


def test_read_type_defaults_to_short_and_keeps_bwa_mem2(local_env):
    assert tasks.read_type_from_env() == "short"
    assert tasks.aligner_from_env() == "bwa-mem2"


@pytest.mark.parametrize("read_type", ["hifi", "ont", "HiFi "])
def test_long_reads_default_to_minimap2(local_env, monkeypatch, read_type):
    monkeypatch.setenv("READ_TYPE", read_type)
    assert tasks.aligner_from_env() == "minimap2"


def test_minimap2_named_explicitly_with_long_reads(local_env, monkeypatch):
    # The benchmark terminal's interface: ALIGNER=minimap2 READ_TYPE=hifi.
    monkeypatch.setenv("ALIGNER", "minimap2")
    monkeypatch.setenv("READ_TYPE", "hifi")
    assert tasks.aligner_from_env() == "minimap2"


@pytest.mark.parametrize("aligner", ["bwa-mem2", "bwa", "giraffe"])
def test_a_short_read_aligner_refuses_long_reads(local_env, monkeypatch, aligner):
    monkeypatch.setenv("ALIGNER", aligner)
    monkeypatch.setenv("READ_TYPE", "hifi")
    with pytest.raises(ValueError, match="READ_TYPE=hifi needs ALIGNER=minimap2"):
        tasks.aligner_from_env()


def test_minimap2_may_align_short_reads(local_env, monkeypatch):
    monkeypatch.setenv("ALIGNER", "minimap2")
    assert tasks.aligner_from_env() == "minimap2"


def test_unknown_read_type_fails(local_env, monkeypatch):
    monkeypatch.setenv("READ_TYPE", "sanger")
    with pytest.raises(ValueError, match="Unknown read type 'sanger'"):
        tasks.read_type_from_env()


def test_read_types_are_consistent():
    assert tasks.DEFAULT_READ_TYPE == "short" and set(tasks.LONG_READ_TYPES) == {"hifi", "ont"}
    for read_type, settings in tasks.READ_TYPES.items():
        assert settings["mark_duplicates"] == (read_type not in tasks.LONG_READ_TYPES)
    # minimap2 builds its own index per preset: nothing to store or fetch for it.
    assert "minimap2" in tasks.ALIGNERS and "minimap2" not in tasks.ALIGNER_INDEX_EXTS


# --- the align command ------------------------------------------------------------------------------------


@pytest.mark.parametrize("read_type, preset", [("hifi", "map-hifi"), ("ont", "lr:hq")])
def test_long_read_align_command_uses_the_preset_and_skips_markdup(read_type, preset):
    command = tasks.align_command("ref.fa", ["S1.fastq.gz"], "out.cram", 8, "/tmp/w", "S1", "minimap2", read_type)
    stages = command.split(" | ")
    assert len(stages) == 2  # minimap2 | sort: no fixmate (single-end), no markdup
    words = shlex.split(stages[0])
    assert words[:4] == ["minimap2", "-ax", preset, "-t"] and words[4] == "8"
    assert words[words.index("-R") + 1] == r"@RG\tID:S1\tSM:S1"
    assert words[-2:] == ["ref.fa", "S1.fastq.gz"]
    assert stages[1].startswith("samtools sort ") and "-u" not in shlex.split(stages[1])
    assert "--reference ref.fa -O cram -o out.cram -" in stages[1]
    assert "markdup" not in command


def test_minimap2_on_short_reads_uses_sr_and_marks_duplicates():
    command = tasks.align_command(
        "ref.fa", ["S1_R1.fastq.gz", "S1_R2.fastq.gz"], "out.cram", 4, "/tmp/w", "S1", "minimap2"
    )
    first, *rest = command.split(" | ")
    assert first.startswith("minimap2 -ax sr ") and first.endswith("S1_R1.fastq.gz S1_R2.fastq.gz")
    assert rest[0] == "samtools fixmate -m -u - -" and rest[-1].startswith("samtools markdup ")


def test_bwa_align_command_is_unchanged_by_the_read_type_parameter():
    # Short reads through bwa-mem2: the same pipeline as before long reads existed.
    default = tasks.align_command("ref.fa", ["S1.fastq.gz"], "out.cram", 4, "/tmp/w", "S1")
    explicit = tasks.align_command("ref.fa", ["S1.fastq.gz"], "out.cram", 4, "/tmp/w", "S1", "bwa-mem2", "short")
    assert default == explicit
    stages = default.split(" | ")
    assert stages[0].startswith(f"bwa-mem2 mem -t 4 -K {tasks.BWA_BATCH_BASES} -R ")
    assert stages[1].startswith("samtools sort -@ 4 ") and stages[1].endswith(" -u -")
    assert stages[2].startswith("samtools markdup -s ")


def test_paired_long_reads_are_a_layout_error():
    with pytest.raises(tasks.LayoutError, match="long reads are single-end"):
        tasks.align_command("ref.fa", ["a_R1.fq.gz", "a_R2.fq.gz"], "o.cram", 1, "/tmp", "a", "minimap2", "hifi")


def test_align_command_refuses_a_short_read_aligner_on_long_reads():
    with pytest.raises(ValueError, match="needs ALIGNER=minimap2"):
        tasks.align_command("ref.fa", ["a.fq.gz"], "o.cram", 1, "/tmp", "a", "bwa-mem2", "ont")


def test_the_chunked_route_refuses_minimap2():
    with pytest.raises(ValueError, match="minimap2 isn't supported there yet"):
        tasks.chunk_align_command("ref.fa", ["c.fq.gz"], "c.bam", 1, "/tmp", "S1", "minimap2")


def test_prepare_aligner_builds_no_index_for_minimap2(local_env, monkeypatch):
    monkeypatch.setattr(tasks, "_ensure_aligner_index", lambda *a: pytest.fail("minimap2 has no stored index"))
    assert tasks.prepare_aligner(REF, "unused.fa", "minimap2") == "minimap2"


# --- the align stage --------------------------------------------------------------------------------------


def test_align_stage_runs_minimap2_and_records_the_read_type(local_env, tool_calls, shell_commands, monkeypatch):
    monkeypatch.setenv("READ_TYPE", "hifi")
    put(local_env, "raw_reads/S1.fastq.gz", b"reads")
    put(local_env, f"reference/{REF}", b">c\nACGT\n")  # no N gap: irrelevant to minimap2 (no bwa-mem2#262 guard)

    tasks.align_task("S1", REF)

    assert [c[:2] for c in tool_calls] == [["samtools", "faidx"]]  # no aligner index built or fetched
    align = shell_commands[0]
    assert align.startswith("minimap2 -ax map-hifi ") and "markdup" not in align
    part = json.loads((local_env / "runs" / "S1.provenance" / "Align.json").read_text())
    assert part["aligner"] == "minimap2" and part["layout"] == "single"
    assert part["read_type"] == "hifi" and part["minimap2_preset"] == "map-hifi"
    assert part["duplicates_marked"] is False
    assert (local_env / "alignments" / "S1.cram").exists()
    record = json.loads((local_env / "runs" / "S1.provenance.json").read_text())  # the assembled run record
    assert record["read_type"] == "hifi" and record["minimap2_preset"] == "map-hifi"
    assert record["duplicates_marked"] is False and record["aligner"] == "minimap2"


def test_align_stage_records_short_reads_too(local_env, tool_calls, shell_commands, monkeypatch):
    monkeypatch.setenv("ALIGNER", "bwa")
    put(local_env, "raw_reads/S1.fastq.gz", b"reads")
    put(local_env, f"reference/{REF}", b">c\nACGT\n")

    tasks.align_task("S1", REF)

    part = json.loads((local_env / "runs" / "S1.provenance" / "Align.json").read_text())
    assert part["read_type"] == "short" and part["duplicates_marked"] is True and "minimap2_preset" not in part


def test_paired_long_read_upload_fails_before_any_download(local_env, monkeypatch):
    monkeypatch.setenv("READ_TYPE", "ont")
    put(local_env, "raw_reads/S1_R1.fastq.gz")
    put(local_env, "raw_reads/S1_R2.fastq.gz")
    monkeypatch.setattr(tasks, "download_raw_reads", lambda *a: pytest.fail("downloaded before the layout check"))
    with pytest.raises(tasks.LayoutError, match="READ_TYPE=ont reads are single-end"):
        tasks.align_task("S1", REF)


def test_a_mismatched_aligner_fails_before_any_download(local_env, monkeypatch):
    monkeypatch.setenv("READ_TYPE", "hifi")
    monkeypatch.setenv("ALIGNER", "bwa-mem2")
    put(local_env, "raw_reads/S1.fastq.gz")
    monkeypatch.setattr(tasks, "download_raw_reads", lambda *a: pytest.fail("downloaded before the aligner check"))
    with pytest.raises(ValueError, match="needs ALIGNER=minimap2"):
        tasks.align_task("S1", REF)


# --- variant calling: mpileup's error model --------------------------------------------------------------


def test_mpileup_options_per_read_type():
    assert tasks.mpileup_options("short") == ""
    assert tasks.mpileup_options("hifi") == "-X pacbio-ccs "
    assert tasks.mpileup_options("ont") == "-X ont "


def test_variants_stage_uses_the_long_read_error_model(local_env, tool_calls, shell_commands, monkeypatch):
    monkeypatch.setenv("READ_TYPE", "hifi")
    monkeypatch.setenv("ANNOTATE", "off")
    put(local_env, f"reference/{REF}", b">c\nACGT\n")
    put(local_env, "alignments/S1.cram")
    put(local_env, "alignments/S1.cram.crai")
    monkeypatch.setattr(tasks, "fetch_alignment", lambda sample_id, wd, **kw: os.path.join(wd, "S1.cram"))

    tasks.variants_task("S1", REF)

    mpileup = next(c for c in shell_commands if c.startswith("bcftools mpileup"))
    assert mpileup.startswith("bcftools mpileup -X pacbio-ccs -f ")
    part = json.loads((local_env / "runs" / "S1.provenance" / "CallVariants.json").read_text())
    assert part["read_type"] == "hifi"


# --- DeepVariant's model type -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env, model",
    [
        ({}, "WGS"),
        ({"READ_TYPE": "short"}, "WGS"),
        ({"READ_TYPE": "hifi"}, "PACBIO"),
        ({"READ_TYPE": "ont"}, "ONT_R104"),
        ({"READ_TYPE": "hifi", "DEEPVARIANT_MODEL_TYPE": "PACBIO"}, "PACBIO"),  # agreeing is fine
        ({"DEEPVARIANT_MODEL_TYPE": "WES"}, "WES"),  # without READ_TYPE, any model passes through (as before)
        ({"DEEPVARIANT_MODEL_TYPE": "HYBRID_PACBIO_ILLUMINA"}, "HYBRID_PACBIO_ILLUMINA"),
    ],
)
def test_deepvariant_model_type(env, model):
    assert tasks.deepvariant_model_type(env) == model


def test_deepvariant_model_type_refuses_a_mismatch():
    with pytest.raises(ValueError, match="DEEPVARIANT_MODEL_TYPE=WGS disagrees with READ_TYPE=hifi"):
        tasks.deepvariant_model_type({"READ_TYPE": "hifi", "DEEPVARIANT_MODEL_TYPE": "WGS"})
    with pytest.raises(ValueError, match="Unknown read type"):
        tasks.deepvariant_model_type({"READ_TYPE": "pacbio"})


# --- run_local and make_test_data -------------------------------------------------------------------------


def test_run_local_knows_the_read_types_and_passes_read_type_on():
    run_local = load_module("run_local_lr", "scripts", "run_local.py")
    assert set(run_local.READ_TYPES) == set(tasks.READ_TYPES)
    assert "READ_TYPE" in run_local.PIPELINE_ENV_VARS
    assert "minimap2" in run_local.ALIGNERS


def test_run_local_read_type_reaches_every_job_without_touching_the_environment(tmp_path, monkeypatch):
    # main() runs in-process in the E2E tests: setting os.environ there would turn every later run into HiFi.
    run_local = load_module("run_local_lr2", "scripts", "run_local.py")
    monkeypatch.delenv("READ_TYPE", raising=False)
    (tmp_path / "raw_reads").mkdir()
    (tmp_path / "raw_reads" / "S1.fastq.gz").write_bytes(b"")
    (tmp_path / "reference").mkdir()
    (tmp_path / "reference" / "r.fa").write_text(">r\nACGT\n")
    seen = []

    def fake_run_job(job, runner, root, dv_memory=None, env=None, *args, **kwargs):
        seen.append((job["name"], env or {}))
        return 0

    monkeypatch.setattr(run_local, "run_job", fake_run_job)

    argv = ["--root", str(tmp_path), "--sample", "S1", "--reference", "r.fa"]
    assert run_local.main([*argv, "--read-type", "hifi"]) == 0

    assert [name for name, _ in seen] == ["qc", "align", "variants", "gate"]
    assert all(env["READ_TYPE"] == "hifi" for _, env in seen)
    assert "READ_TYPE" not in os.environ
    seen.clear()
    assert run_local.main(argv) == 0  # a later run without --read-type is a short-read run again
    assert all("READ_TYPE" not in env for _, env in seen)


def test_make_test_data_long_reads(tmp_path):
    mtd = load_module("make_test_data_lr", "scripts", "make_test_data.py")
    mtd.main(["--root", str(tmp_path), "--sample", "HIFI001", "--long-reads"])

    assert not list((tmp_path / "raw_reads").glob("HIFI001_R*"))  # single-end
    with gzip.open(tmp_path / "raw_reads" / "HIFI001.fastq.gz", "rt") as fh:
        lines = fh.read().splitlines()
    seqs, quals = lines[1::4], lines[3::4]
    lengths = [len(s) for s in seqs]
    assert all(len(s) == len(q) for s, q in zip(seqs, quals, strict=True))
    assert 4000 < statistics.mean(lengths) < 6000 and min(lengths) >= mtd.MIN_LONG_READ - 50  # clipped ends
    assert sum(lengths) >= 50_000 * 30  # the default 30x
    truth = json.loads((tmp_path / "truth" / "HIFI001.json").read_text())
    assert truth["read_type"] == "hifi" and len(truth["snps"]) == 10 and truth["planted_duplicates"] == 0
    # Every SNP at least one mean read length from the contig ends, so long reads cover it fully.
    assert all(mtd.LONG_READ_MEAN <= s["pos"] <= 50_000 - mtd.LONG_READ_MEAN for s in truth["snps"])


@pytest.mark.parametrize("extra", [["--paired"], ["--duplicate-rate", "0.1"], ["--long-read-length", "50000"]])
def test_make_test_data_long_reads_rejects_conflicting_options(tmp_path, extra):
    mtd = load_module("make_test_data_lr2", "scripts", "make_test_data.py")
    with pytest.raises(SystemExit):
        mtd.main(["--root", str(tmp_path), "--long-reads", *extra])


def test_long_read_errors_are_substitutions_and_small_indels():
    import random

    mtd = load_module("make_test_data_lr3", "scripts", "make_test_data.py")
    seq = "ACGT" * 2500
    noisy = mtd.add_long_read_errors(random.Random(1), seq, 0.01, 0.3)
    assert noisy != seq and abs(len(noisy) - len(seq)) < 60  # indels change the length a little, not a lot
    assert mtd.add_long_read_errors(random.Random(1), seq, 0, 0.3) == seq
