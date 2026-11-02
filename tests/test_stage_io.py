"""Stage plumbing around the bioinformatics tools: what each stage downloads, uploads and cleans up.

The tools themselves (bwa-mem2, bwa, samtools, bcftools, fastp) are faked, so these run anywhere.
The Docker end-to-end test covers the real tools.
"""

import gzip
import shlex
import subprocess

import pytest

import tasks
from conftest import stats_fixture

REF = "ref.fa"
# The default aligner's index files (bwa-mem2: .0123, .amb, .ann, .bwt.2bit.64, .pac).
INDEX_EXTS = tasks.ALIGNER_INDEX_EXTS[tasks.DEFAULT_ALIGNER]
# A reference whose first contig starts with a gap, like a human chromosome: bwa-mem2 is safe on it.
GAPPED_REF = b">c\n" + b"N" * tasks.BWA_MEM2_SAFE_N_PREFIX + b"\nACGT\n"


def put(root, key, data=b"x"):
    path = root.joinpath(*key.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def work_dir_is_empty(local_env):
    work = local_env.parent / "work"
    return not work.exists() or not any(work.iterdir())


# --- ensure_reference_local ---------------------------------------------------


def test_stored_indexes_are_downloaded_not_rebuilt(local_env, tool_calls, tmp_path):
    put(local_env, f"reference/{REF}", GAPPED_REF)
    for ext in [".fai"] + INDEX_EXTS:
        put(local_env, f"reference/{REF}{ext}")
    dest = tmp_path / "dest"
    dest.mkdir()

    path = tasks.ensure_reference_local(REF, str(dest))

    assert path == str(dest / REF)
    assert tool_calls == []
    assert all((dest / f"{REF}{ext}").exists() for ext in [".fai"] + INDEX_EXTS)


def test_missing_indexes_are_built(local_env, tool_calls, tmp_path):
    put(local_env, f"reference/{REF}", GAPPED_REF)
    dest = tmp_path / "dest"
    dest.mkdir()

    tasks.ensure_reference_local(REF, str(dest))

    assert [c[:2] for c in tool_calls] == [["samtools", "faidx"], ["bwa-mem2", "index"]]


@pytest.mark.parametrize("aligner, missing", [(a, ext) for a, exts in tasks.ALIGNER_INDEX_EXTS.items() for ext in exts])
def test_one_missing_index_file_triggers_a_rebuild(local_env, tool_calls, tmp_path, monkeypatch, aligner, missing):
    monkeypatch.setenv("ALIGNER", aligner)
    put(local_env, f"reference/{REF}", GAPPED_REF)
    for ext in [".fai"] + [e for e in tasks.ALIGNER_INDEX_EXTS[aligner] if e != missing]:
        put(local_env, f"reference/{REF}{ext}")
    dest = tmp_path / "dest"
    dest.mkdir()

    tasks.ensure_reference_local(REF, str(dest))

    assert [c[:2] for c in tool_calls] == [[aligner, "index"]]
    assert all((dest / f"{REF}{ext}").exists() for ext in tasks.ALIGNER_INDEX_EXTS[aligner])


def test_a_partly_stored_index_is_not_downloaded(local_env, tool_calls, tmp_path, monkeypatch):
    # Only bwa's index is stored: bwa-mem2 builds its own without first downloading the shared files.
    put(local_env, f"reference/{REF}", GAPPED_REF)
    for ext in [".fai"] + tasks.ALIGNER_INDEX_EXTS["bwa"]:
        put(local_env, f"reference/{REF}{ext}")
    downloaded = []
    real_download = tasks.storage().download
    monkeypatch.setattr(
        tasks.storage(), "download", lambda key, path: downloaded.append(key) or real_download(key, path)
    )
    dest = tmp_path / "dest"
    dest.mkdir()

    tasks.ensure_reference_local(REF, str(dest))

    assert downloaded == [f"reference/{REF}", f"reference/{REF}.fai"]
    assert [c[:2] for c in tool_calls] == [["bwa-mem2", "index"]]


def test_a_stored_bwa_index_is_not_enough_for_bwa_mem2(local_env, tool_calls, tmp_path):
    # The cloud reference/ prefix held only bwa's index before bwa-mem2: .amb/.ann/.pac are shared,
    # but bwa-mem2 also needs .0123 and .bwt.2bit.64, so it must rebuild rather than fail in `mem`.
    put(local_env, f"reference/{REF}", GAPPED_REF)
    for ext in [".fai"] + tasks.ALIGNER_INDEX_EXTS["bwa"]:
        put(local_env, f"reference/{REF}{ext}")
    dest = tmp_path / "dest"
    dest.mkdir()

    tasks.ensure_reference_local(REF, str(dest))

    assert [c[:2] for c in tool_calls] == [["bwa-mem2", "index"]]
    assert all((dest / f"{REF}{ext}").exists() for ext in INDEX_EXTS)


def test_aligner_env_selects_which_index_is_fetched(local_env, tool_calls, tmp_path, monkeypatch):
    monkeypatch.setenv("ALIGNER", "bwa")
    put(local_env, f"reference/{REF}", b">c\nACGT\n")
    for ext in [".fai"] + tasks.ALIGNER_INDEX_EXTS["bwa"]:
        put(local_env, f"reference/{REF}{ext}")
    dest = tmp_path / "dest"
    dest.mkdir()

    tasks.ensure_reference_local(REF, str(dest))

    assert tool_calls == []  # bwa's stored index is complete: nothing to build
    assert not (dest / f"{REF}.0123").exists()


def test_the_default_index_path_applies_the_bwa_mem2_guard(local_env, tool_calls, tmp_path):
    # Not only the align stage: any caller asking for an aligner index on a reference that starts
    # with sequence gets bwa's index, never a bwa-mem2 one it could misalign with (bwa-mem2#262).
    put(local_env, f"reference/{REF}", b">c\nACGT\n")
    dest = tmp_path / "dest"
    dest.mkdir()

    tasks.ensure_reference_local(REF, str(dest))

    assert [c[:2] for c in tool_calls] == [["samtools", "faidx"], ["bwa", "index"]]


def test_unknown_aligner_argument_fails_before_the_download(local_env, tool_calls, tmp_path):
    # No reference stored: reaching the download would raise FileNotFoundError instead.
    with pytest.raises(ValueError, match="minimap2"):
        tasks.ensure_reference_local(REF, str(tmp_path), aligner="minimap2")


def test_variant_calling_skips_the_bwa_index(local_env, tool_calls, tmp_path):
    put(local_env, f"reference/{REF}", b">c\nACGT\n")
    dest = tmp_path / "dest"
    dest.mkdir()

    tasks.ensure_reference_local(REF, str(dest), need_bwa_index=False)

    assert [c[:2] for c in tool_calls] == [["samtools", "faidx"]]
    assert not any((dest / f"{REF}{ext}").exists() for ext in INDEX_EXTS)


def test_missing_reference_fasta_is_a_clear_error(local_env, tool_calls, tmp_path):
    with pytest.raises(FileNotFoundError, match="Reference FASTA missing"):
        tasks.ensure_reference_local("absent.fa", str(tmp_path))


# --- stages -------------------------------------------------------------------


def test_qc_uploads_both_reports_and_cleans_up(local_env, tool_calls):
    put(local_env, "raw_reads/S1.fastq.gz", gzip.compress(b"@r\nA\n+\nI\n"))

    tasks.qc_task("S1")

    assert (local_env / "qc_reports" / "S1.fastp.html").read_text() == "html"
    assert (local_env / "qc_reports" / "S1.fastp.json").read_text() == '{"summary": {}}'
    assert [c[0] for c in tool_calls] == ["fastp"]
    assert work_dir_is_empty(local_env)


def test_qc_timeout_comes_from_environment(local_env, monkeypatch):
    put(local_env, "raw_reads/S1.fastq.gz")
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


def test_align_uploads_cram_and_its_index(local_env, tool_calls, shell_commands, uploads):
    put(local_env, "raw_reads/S1.fastq.gz")
    put(local_env, f"reference/{REF}", GAPPED_REF)

    tasks.align_task("S1", REF)

    align, index, flagstat, mosdepth = shell_commands
    assert align.startswith("bwa-mem2 mem")
    assert "S1.fastq.gz" in align.split(" | ")[0]  # the aligner reads the upload, still gzipped
    assert "-O cram" in align.split(" | ")[-1]
    assert index.startswith("samtools index ")
    assert (local_env / "alignments" / "S1.cram").read_bytes().startswith(b"output of: bwa-mem2 mem")
    assert (local_env / "alignments" / "S1.cram.crai").exists()
    assert not (local_env / "alignments" / "S1.bam").exists()
    # The CRAM marks the stage done, so its index must already be there when it appears.
    assert flagstat.startswith("samtools flagstat ") and mosdepth.startswith("mosdepth ")
    # Stats and index before the CRAM: once a sample shows as aligned, everything about it is there.
    assert uploads == [
        "stats/S1/flagstat.txt",
        "stats/S1/mosdepth.summary.txt",
        "stats/S1/mosdepth.global.dist.txt",
        "alignments/S1.cram.crai",
        "alignments/S1.cram",
    ]
    assert (local_env / "stats" / "S1" / "flagstat.txt").read_bytes() == stats_fixture("single", "flagstat.txt")
    assert work_dir_is_empty(local_env)


def test_align_with_aligner_bwa_builds_and_runs_bwa(local_env, tool_calls, shell_commands, monkeypatch):
    monkeypatch.setenv("ALIGNER", "bwa")
    put(local_env, "raw_reads/S1.fastq.gz")
    put(local_env, f"reference/{REF}", b">c\nACGT\n")

    tasks.align_task("S1", REF)

    assert [c[:2] for c in tool_calls] == [["samtools", "faidx"], ["bwa", "index"]]
    assert shell_commands[0].startswith("bwa mem")


def test_align_falls_back_to_bwa_when_the_first_contig_starts_with_sequence(
    local_env, tool_calls, shell_commands, capsys
):
    # bwa-mem2#262: reads at the very start of the first contig get misplaced. ACGT... has no gap.
    put(local_env, "raw_reads/S1.fastq.gz")
    put(local_env, f"reference/{REF}", b">c\nACGT\n")

    tasks.align_task("S1", REF)

    assert shell_commands[0].startswith("bwa mem ")
    assert ["bwa", "index"] in [c[:2] for c in tool_calls]
    assert not any(c[0] == "bwa-mem2" for c in tool_calls)  # no wasted bwa-mem2 index build
    assert "bwa-mem2#262" in capsys.readouterr().out


def test_unknown_aligner_fails_before_any_download(local_env, tool_calls, monkeypatch):
    monkeypatch.setenv("ALIGNER", "bowtie2")
    put(local_env, "raw_reads/S1.fastq.gz")

    with pytest.raises(ValueError, match="bowtie2"):
        tasks.align_task("S1", REF)  # no reference stored: a download would fail differently
    assert tool_calls == []
    assert work_dir_is_empty(local_env)


@pytest.mark.parametrize("stored", ["S1.cram", "S1.bam"])
def test_variants_calls_bcftools_on_the_alignment_and_indexes_the_vcf(
    local_env, tool_calls, shell_commands, uploads, stored
):
    put(local_env, f"alignments/{stored}", b"ALN")
    put(local_env, f"reference/{REF}", b">c\nACGT\n")

    tasks.variants_task("S1", REF)

    call, index, stats = shell_commands
    assert call.startswith("bcftools mpileup") and "bcftools call -mv" in call
    assert shlex.split(call.split(" | ")[0])[-1].endswith(stored)  # the stored CRAM, or a BAM if that's all
    assert index.startswith("bcftools index -t ")
    assert (local_env / "variants" / "S1.vcf.gz").exists()
    assert stats.startswith("bcftools stats ")
    assert uploads == ["stats/S1/bcftools.stats.txt", "variants/S1.vcf.gz.tbi", "variants/S1.vcf.gz"]
    # variant calling must not build the (slow) aligner index
    assert [c[:2] for c in tool_calls] == [["samtools", "faidx"]]
    assert work_dir_is_empty(local_env)


def test_failed_tool_fails_the_stage_and_still_cleans_up(local_env, tool_calls, monkeypatch):
    put(local_env, "raw_reads/S1.fastq.gz")
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
        "qc_html": "qc_reports/S1.fastp.html",
        "qc_json": "qc_reports/S1.fastp.json",
        "cram": "alignments/S1.cram",
        "crai": "alignments/S1.cram.crai",
        "bam": "alignments/S1.bam",
        "vcf": "variants/S1.vcf.gz",
        "tbi": "variants/S1.vcf.gz.tbi",
        "flagstat": "stats/S1/flagstat.txt",
        "mosdepth_summary": "stats/S1/mosdepth.summary.txt",
        "mosdepth_dist": "stats/S1/mosdepth.global.dist.txt",
        "bcftools_stats": "stats/S1/bcftools.stats.txt",
    }


# --- bwa-mem2 guard (bwa-mem2#262) ---------------------------------------------


def write_ref(tmp_path, text, name="r.fa"):
    path = tmp_path / name
    if name.endswith(".gz"):
        import gzip

        with gzip.open(path, "wt") as fh:
            fh.write(text)
    else:
        path.write_text(text)
    return str(path)


def test_first_contig_prefix_joins_lines_and_stops_at_the_next_contig(tmp_path):
    ref = write_ref(tmp_path, ">a desc\nnnAC\nGT\n>b\nTTTT\n")
    assert tasks.first_contig_prefix(ref, 5) == "NNACG"
    assert tasks.first_contig_prefix(ref, 100) == "NNACGT"  # never reads into contig b


def test_first_contig_prefix_reads_gzipped_fasta(tmp_path):
    assert tasks.first_contig_prefix(write_ref(tmp_path, ">a\nNNNA\n", "r.fa.gz"), 3) == "NNN"


@pytest.mark.parametrize(
    "first_contig, expected",
    [
        ("N" * 1000 + "ACGT", "bwa-mem2"),  # human-like leading gap
        ("n" * 600 + "\n" + "N" * 400, "bwa-mem2"),  # soft-masked, split over lines
        ("N" * 999 + "A", "bwa"),  # gap one base too short
        ("ACGT" + "N" * 2000, "bwa"),  # sequence at position 1
        ("N" * 500, "bwa"),  # contig shorter than the window: can't tell, be safe
    ],
)
def test_effective_aligner(tmp_path, first_contig, expected):
    ref = write_ref(tmp_path, f">c\n{first_contig}\n>d\n{'N' * 2000}\n")
    assert tasks.effective_aligner("bwa-mem2", ref) == expected


def test_effective_aligner_leaves_bwa_alone(tmp_path):
    assert tasks.effective_aligner("bwa", write_ref(tmp_path, ">c\nACGT\n")) == "bwa"


# --- alignment input (CRAM, else BAM) ------------------------------------------


def test_alignment_input_prefers_the_cram_and_notes_a_bam_beside_it(capsys):
    present = {"alignments/S1.cram", "alignments/S1.bam"}
    assert tasks.alignment_input("S1", present.__contains__) == "alignments/S1.cram"
    out = capsys.readouterr().out
    assert "Note:" in out and "alignments/S1.bam" in out  # an imported BAM next to a CRAM is easy to miss


def test_alignment_input_falls_back_to_a_bam_quietly(capsys):
    # fetch_dv_testdata.py writes alignments/<id>.bam, and runs from before CRAM left BAMs.
    assert tasks.alignment_input("S1", {"alignments/S1.bam"}.__contains__) == "alignments/S1.bam"
    assert "Note:" not in capsys.readouterr().out


def test_alignment_input_without_either_names_both(local_env):
    with pytest.raises(FileNotFoundError, match=r"S1\.cram.*S1\.bam"):
        tasks.alignment_input("S1")


def test_fetch_alignment_indexes_what_it_downloaded_not_the_stored_index(
    local_env, tool_calls, shell_commands, tmp_path
):
    # A stored .crai can belong to the previous CRAM if a re-run failed between its two uploads.
    put(local_env, "alignments/S1.cram", b"CRAM")
    put(local_env, "alignments/S1.cram.crai", b"STALE")

    path = tasks.fetch_alignment("S1", str(tmp_path), need_index=True)

    assert path == str(tmp_path / "S1.cram")
    assert not (tmp_path / "S1.cram.crai").exists()  # not downloaded
    (command,) = [shlex.split(c) for c in shell_commands]
    assert command[:2] == ["samtools", "index"] and command[-1] == path


def test_fetch_alignment_uses_the_given_indexer(local_env, tool_calls, shell_commands, tmp_path):
    put(local_env, "alignments/S1.cram", b"CRAM")
    indexed = []

    tasks.fetch_alignment("S1", str(tmp_path), need_index=True, indexer=indexed.append)

    assert indexed == [str(tmp_path / "S1.cram")] and shell_commands == []


def test_one_alignment_rule_for_stages_web_and_reconcile():
    # status.py and reconcile.py import ALIGNMENT_FORMS; this pins the order readers rely on.
    assert tasks.ALIGNMENT_FORMS == ("cram", "bam")
    assert all(form in tasks.keys_for("S1") for form in tasks.ALIGNMENT_FORMS)


@pytest.mark.parametrize("stored_bai", [True, False])
def test_fetch_alignment_reuses_a_bams_stored_index(local_env, tool_calls, shell_commands, tmp_path, stored_bai):
    # Imported BAMs are never half-written by a stage, so their .bai can be trusted; a CRAM's .crai can't.
    put(local_env, "alignments/S1.bam", b"BAM")
    if stored_bai:
        put(local_env, "alignments/S1.bam.bai", b"BAI")

    path = tasks.fetch_alignment("S1", str(tmp_path), need_index=True)

    if stored_bai:
        assert (tmp_path / "S1.bam.bai").read_bytes() == b"BAI" and shell_commands == []
    else:
        (command,) = [shlex.split(c) for c in shell_commands]
        assert command[:2] == ["samtools", "index"] and command[-1] == path


# --- QC statistics (flagstat, mosdepth, bcftools stats) ------------------------


def text_fixture(layout, name):
    return stats_fixture(layout, name).decode()


def test_parse_flagstat_single_and_paired_end():
    single = tasks.parse_flagstat(text_fixture("single", "flagstat.txt"))
    assert single["in_total"] == 10_000 and single["mapped_pct"] == 100.0
    assert single["duplicates"] == 485  # chance duplicates of single-end reads (see align_command)
    assert single["paired_in_sequencing"] == 0 and "properly_paired_pct" not in single  # N/A
    paired = tasks.parse_flagstat(text_fixture("paired", "flagstat.txt"))
    assert paired["properly_paired"] == 10_000 and paired["properly_paired_pct"] == 100.0
    assert paired["duplicates"] == 4


def test_parse_mosdepth_summary_uses_the_total_row():
    # 10,000 x 150 bp over 51,000 bp (1,000 of them the leading N gap), duplicates excluded.
    assert tasks.parse_mosdepth_summary(text_fixture("single", "mosdepth.summary.txt")) == {
        "length": 51_000,
        "bases": 1_427_250,
        "mean_depth": 27.99,
    }


def test_parse_bcftools_stats():
    assert tasks.parse_bcftools_stats(text_fixture("single", "bcftools.stats.txt")) == {
        "records": 10,
        "snps": 10,
        "indels": 0,
        "tstv": 1.5,
    }


def test_failed_stats_tools_warn_but_the_alignment_is_still_uploaded(
    local_env, tool_calls, monkeypatch, capsys, uploads
):
    put(local_env, "raw_reads/S1.fastq.gz")
    put(local_env, f"reference/{REF}", GAPPED_REF)

    def run_shell(command):
        words = shlex.split(command)
        if words[0] in ("mosdepth",) or words[:2] == ["samtools", "flagstat"]:
            raise subprocess.CalledProcessError(1, command)
        out = words[words.index("-o") + 1] if "-o" in words else words[-1]
        with open(out, "wb") as fh:
            fh.write(b"data")

    monkeypatch.setattr(tasks, "run_shell", run_shell)

    tasks.align_task("S1", REF)

    assert uploads == ["alignments/S1.cram.crai", "alignments/S1.cram"]  # no stats, but the alignment
    out = capsys.readouterr().out
    assert "WARNING: samtools flagstat failed" in out and "WARNING: mosdepth failed" in out


def test_parse_bcftools_stats_all_transitions_has_no_tstv():
    # 3 transitions, 0 transversions: bcftools prints ts/tv 0.00, but the ratio is undefined.
    text = "SN\t0\tnumber of SNPs:\t3\nTSTV\t0\t3\t0\t0.00\t3\t0\t0.00\n"
    assert tasks.parse_bcftools_stats(text)["tstv"] is None


def test_parse_bcftools_stats_without_snps_has_no_tstv():
    text = "SN\t0\tnumber of records:\t0\nSN\t0\tnumber of SNPs:\t0\nTSTV\t0\t0\t0\t0.00\t0\t0\t0.00\n"
    assert tasks.parse_bcftools_stats(text)["tstv"] is None


def test_a_stats_file_that_does_not_parse_only_warns(tmp_path, capsys):
    bad = tmp_path / "flagstat.txt"
    bad.write_text("not flagstat")
    tasks._log_summary("mosdepth", tasks.parse_mosdepth_summary, str(bad))  # must not raise
    assert "WARNING: could not summarise flagstat.txt" in capsys.readouterr().out


def test_alignment_stats_commands(shell_commands, tmp_path):
    files = tasks.alignment_stats("/w/ref.fa", "/w/S1.cram", str(tmp_path), 16, tasks.keys_for("S1"))
    flagstat, mosdepth = (shlex.split(c) for c in shell_commands)
    assert flagstat[:2] == ["samtools", "flagstat"] and "reference=/w/ref.fa" in flagstat  # CRAM needs it
    assert mosdepth[:2] == ["mosdepth", "-n"]  # summary only: no per-base bedGraph
    assert "--fast-mode" not in mosdepth  # it would count overlapping mates twice (paired-end depth too high)
    assert mosdepth[mosdepth.index("-t") + 1] == str(tasks.MOSDEPTH_MAX_THREADS)
    assert mosdepth[mosdepth.index("-f") + 1] == "/w/ref.fa" and mosdepth[-1] == "/w/S1.cram"
    assert [key for _, key in files] == [
        "stats/S1/flagstat.txt",
        "stats/S1/mosdepth.summary.txt",
        "stats/S1/mosdepth.global.dist.txt",
    ]


def test_stats_are_per_sample_not_per_mate():
    keys = tasks.keys_for("S1", "paired")
    assert keys["flagstat"] == "stats/S1/flagstat.txt" and "flagstat_R1" not in keys


# --- run record (provenance.py) --------------------------------------------------


def read_json(root, key):
    import json

    return json.loads((root / key).read_text())


def test_align_writes_its_run_record_part(local_env, tool_calls, shell_commands, monkeypatch):
    import hashlib

    monkeypatch.setenv("RUN_NAME", "S1-20261102T100000Z-abc")
    monkeypatch.setenv("APP_VERSION", "dev-local")
    put(local_env, "raw_reads/S1.fastq.gz", b"reads")
    put(local_env, f"reference/{REF}", GAPPED_REF)

    tasks.align_task("S1", REF)

    part = read_json(local_env, "runs/S1.provenance/Align.json")
    assert part["status"] == "Success" and part["run_name"] == "S1-20261102T100000Z-abc"
    assert part["aligner"] == "bwa-mem2" and part["layout"] == "single" and part["app_version"] == "dev-local"
    assert part["inputs"] == [{"key": "raw_reads/S1.fastq.gz", "bytes": 5}]
    assert part["reference"] == {"name": REF, "md5": hashlib.md5(GAPPED_REF).hexdigest(), "bytes": len(GAPPED_REF)}
    assert [o["key"] for o in part["outputs"]] == [
        "stats/S1/flagstat.txt",
        "stats/S1/mosdepth.summary.txt",
        "stats/S1/mosdepth.global.dist.txt",
        "alignments/S1.cram.crai",
        "alignments/S1.cram",
    ]
    assert part["stats"]["flagstat"]["mapped_pct"] == 100.0  # the fixture's real flagstat output
    assert part["stats"]["mosdepth"]["mean_depth"] == 27.99
    assert set(part["tools"]) >= {"bwa-mem2", "samtools", "mosdepth"}
    record = read_json(local_env, "runs/S1.provenance.json")
    assert record["stages"]["Align"]["status"] == "Success" and record["status"] == "incomplete"


def test_a_failed_stage_still_records_itself_with_the_error(local_env, tool_calls, monkeypatch):
    put(local_env, "raw_reads/S1.fastq.gz")
    put(local_env, f"reference/{REF}", GAPPED_REF)

    def failing(command):
        raise subprocess.CalledProcessError(1, "bwa-mem2 mem ...")

    monkeypatch.setattr(tasks, "run_shell", failing)
    with pytest.raises(subprocess.CalledProcessError):
        tasks.align_task("S1", REF)

    part = read_json(local_env, "runs/S1.provenance/Align.json")
    assert part["status"] == "Failure" and part["error"].startswith("CalledProcessError")
    assert read_json(local_env, "runs/S1.provenance.json")["status"] == "failed"


def test_a_full_run_assembles_a_complete_record(local_env, tool_calls, shell_commands):
    put(local_env, "raw_reads/S1.fastq.gz")
    put(local_env, f"reference/{REF}", GAPPED_REF)

    tasks.qc_task("S1")
    tasks.align_task("S1", REF)
    tasks.variants_task("S1", REF)

    record = read_json(local_env, "runs/S1.provenance.json")
    assert record["status"] == "succeeded" and record["complete"]
    assert set(record["stages"]) == {"QualityControl", "Align", "CallVariants"}
    assert "variants/S1.vcf.gz" in [o["key"] for o in record["outputs"]]
    assert record["stats"]["bcftools_stats"] == {"records": 10, "snps": 10, "indels": 0, "tstv": 1.5}


def test_a_broken_run_record_never_fails_the_stage(local_env, tool_calls, monkeypatch):
    import provenance

    def broken(*args, **kwargs):
        raise RuntimeError("storage is down for run records")

    monkeypatch.setattr(provenance, "assemble", broken)
    put(local_env, "raw_reads/S1.fastq.gz", b"@r\nA\n+\nI\n")

    tasks.qc_task("S1")  # must not raise

    assert (local_env / tasks.keys_for("S1")["qc_html"]).exists()
