"""Stage plumbing around the bioinformatics tools: what each stage downloads, uploads and cleans up.

The tools themselves (bwa, samtools, bcftools, fastqc) are faked, so these run anywhere.
The Docker end-to-end test covers the real tools.
"""
import os
import shlex
import subprocess

import pytest

import tasks

REF = "ref.fa"


def put(root, key, data=b"x"):
    path = root.joinpath(*key.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


@pytest.fixture
def tool_calls(monkeypatch):
    """Fake subprocess.run for index builders and fastqc; each fake writes the files the real tool would."""
    calls = []

    def fake_run(cmd, check=False, timeout=None, **kwargs):
        calls.append(list(cmd))
        tool = cmd[0]
        if tool == "bwa" and cmd[1] == "index":
            for ext in tasks.BWA_INDEX_EXTS:
                open(cmd[2] + ext, "wb").close()
        elif tool == "samtools" and cmd[1] == "faidx":
            open(cmd[2] + ".fai", "wb").close()
        elif tool == "fastqc":
            # Inputs are the .fastq arguments, wherever they sit among the flags (-t, -o ...).
            out_dir = cmd[cmd.index("-o") + 1]
            for fastq in (arg for arg in cmd if arg.endswith(".fastq")):
                stem = os.path.basename(fastq).rsplit(".", 1)[0]
                for ext in ("html", "zip"):
                    with open(os.path.join(out_dir, f"{stem}_fastqc.{ext}"), "w") as fh:
                        fh.write(ext)
        else:
            raise AssertionError(f"unexpected command {cmd}")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(tasks.subprocess, "run", fake_run)
    return calls


@pytest.fixture
def shell_commands(monkeypatch):
    """Fake run_shell: records each pipeline and creates the output file named after -o / the last argument."""
    commands = []

    def fake_run_shell(command):
        commands.append(command)
        words = shlex.split(command)
        out = words[words.index("-o") + 1] if "-o" in words else words[-1]
        with open(out, "wb") as fh:
            fh.write(b"output of: " + command.encode())

    monkeypatch.setattr(tasks, "run_shell", fake_run_shell)
    return commands


def work_dir_is_empty(local_env):
    work = local_env.parent / "work"
    return not work.exists() or not any(work.iterdir())


# --- ensure_reference_local ---------------------------------------------------

def test_stored_indexes_are_downloaded_not_rebuilt(local_env, tool_calls, tmp_path):
    put(local_env, f"reference/{REF}", b">c\nACGT\n")
    for ext in [".fai"] + tasks.BWA_INDEX_EXTS:
        put(local_env, f"reference/{REF}{ext}")
    dest = tmp_path / "dest"
    dest.mkdir()

    path = tasks.ensure_reference_local(REF, str(dest))

    assert path == str(dest / REF)
    assert tool_calls == []
    assert all((dest / f"{REF}{ext}").exists() for ext in [".fai"] + tasks.BWA_INDEX_EXTS)


def test_missing_indexes_are_built(local_env, tool_calls, tmp_path):
    put(local_env, f"reference/{REF}", b">c\nACGT\n")
    dest = tmp_path / "dest"
    dest.mkdir()

    tasks.ensure_reference_local(REF, str(dest))

    assert [c[:2] for c in tool_calls] == [["bwa", "index"], ["samtools", "faidx"]]


def test_one_missing_bwa_index_file_triggers_a_rebuild(local_env, tool_calls, tmp_path):
    put(local_env, f"reference/{REF}", b">c\nACGT\n")
    for ext in [".fai"] + tasks.BWA_INDEX_EXTS[:-1]:  # .sa missing
        put(local_env, f"reference/{REF}{ext}")
    dest = tmp_path / "dest"
    dest.mkdir()

    tasks.ensure_reference_local(REF, str(dest))

    assert [c[:2] for c in tool_calls] == [["bwa", "index"]]


def test_variant_calling_skips_the_bwa_index(local_env, tool_calls, tmp_path):
    put(local_env, f"reference/{REF}", b">c\nACGT\n")
    dest = tmp_path / "dest"
    dest.mkdir()

    tasks.ensure_reference_local(REF, str(dest), need_bwa_index=False)

    assert [c[:2] for c in tool_calls] == [["samtools", "faidx"]]
    assert not (dest / f"{REF}.bwt").exists()


def test_missing_reference_fasta_is_a_clear_error(local_env, tool_calls, tmp_path):
    with pytest.raises(FileNotFoundError, match="Reference FASTA missing"):
        tasks.ensure_reference_local("absent.fa", str(tmp_path))


# --- stages -------------------------------------------------------------------

def test_qc_uploads_both_reports_and_cleans_up(local_env, tool_calls):
    put(local_env, "decompressed/S1.fastq", b"@r\nA\n+\nI\n")

    tasks.qc_task("S1")

    assert (local_env / "qc_reports" / "S1_fastqc.html").read_text() == "html"
    assert (local_env / "qc_reports" / "S1_fastqc.zip").read_text() == "zip"
    assert [c[0] for c in tool_calls] == ["fastqc"]
    assert work_dir_is_empty(local_env)


def test_qc_timeout_comes_from_environment(local_env, monkeypatch):
    put(local_env, "decompressed/S1.fastq")
    seen = {}

    def fake_run(cmd, check=False, timeout=None, **kwargs):
        seen["timeout"] = timeout
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(tasks.subprocess, "run", fake_run)
    monkeypatch.setenv("QC_TIMEOUT_SECONDS", "7")

    with pytest.raises(subprocess.TimeoutExpired):
        tasks.qc_task("S1")
    assert seen["timeout"] == 7
    assert work_dir_is_empty(local_env)


def test_align_uploads_bam_under_the_stage_key(local_env, tool_calls, shell_commands):
    put(local_env, "decompressed/S1.fastq")
    put(local_env, f"reference/{REF}", b">c\nACGT\n")

    tasks.align_task("S1", REF)

    assert len(shell_commands) == 1 and shell_commands[0].startswith("bwa mem")
    assert (local_env / "alignments" / "S1.bam").read_bytes().startswith(b"output of: bwa mem")
    assert work_dir_is_empty(local_env)


def test_variants_calls_bcftools_on_the_aligned_bam(local_env, tool_calls, shell_commands):
    put(local_env, "alignments/S1.bam", b"BAM")
    put(local_env, f"reference/{REF}", b">c\nACGT\n")

    tasks.variants_task("S1", REF)

    (command,) = shell_commands
    assert command.startswith("bcftools mpileup") and "bcftools call -mv" in command
    assert (local_env / "variants" / "S1.vcf.gz").exists()
    # variant calling must not build the (slow) BWA index
    assert [c[:2] for c in tool_calls] == [["samtools", "faidx"]]
    assert work_dir_is_empty(local_env)


def test_failed_tool_fails_the_stage_and_still_cleans_up(local_env, tool_calls, monkeypatch):
    put(local_env, "decompressed/S1.fastq")
    put(local_env, f"reference/{REF}", b">c\nACGT\n")

    def failing(command):
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(tasks, "run_shell", failing)

    with pytest.raises(subprocess.CalledProcessError):
        tasks.align_task("S1", REF)
    assert not (local_env / "alignments" / "S1.bam").exists()
    assert work_dir_is_empty(local_env)


# --- CLI ----------------------------------------------------------------------

def test_main_dispatches_stage_with_reference(local_env, monkeypatch):
    called = []
    monkeypatch.setitem(tasks.TASKS, "variants", (lambda *a: called.append(a), True))
    assert tasks.main(["variants", "S1", "chr20.fa"]) == 0
    assert called == [("S1", "chr20.fa")]


def test_main_ignores_reference_for_stages_that_do_not_need_one(local_env, monkeypatch):
    called = []
    monkeypatch.setitem(tasks.TASKS, "qc", (lambda *a: called.append(a), False))
    assert tasks.main(["qc", "S1", "chr20.fa"]) == 0
    assert called == [("S1",)]


def test_main_rejects_unknown_stage():
    with pytest.raises(SystemExit):
        tasks.main(["dedupe", "S1"])


def test_keys_follow_the_documented_layout():
    assert tasks.keys_for("S1") == {
        "raw": "raw_reads/S1.fastq.gz",
        "fastq": "decompressed/S1.fastq",
        "qc_html": "qc_reports/S1_fastqc.html",
        "qc_zip": "qc_reports/S1_fastqc.zip",
        "bam": "alignments/S1.bam",
        "vcf": "variants/S1.vcf.gz",
    }
