"""scripts/benchmark_aligners.py helpers (no Docker needed)."""
import csv
import gzip

import pytest

import benchmark_aligners as ba
import tasks

MEASURED_OUTPUT = """Aligning with bwa-mem2
[main] Real time: 2.000 sec; CPU: 1.500 sec
READ: 33475012
DUPLICATE TOTAL: 2911218
[main] Real time: 812.345 sec; CPU: 11893.210 sec
GEYSER_PEAK_ANON_BYTES=7516192768
GEYSER_CGROUP_PEAK_BYTES=12884901888
"""


def test_sample_ids_do_not_touch_the_source_sample():
    assert ba.sample_id("HG003_chr20", "bwa") == "HG003_chr20_bwa"
    assert ba.sample_id("HG003_chr20", "bwa-mem2") == "HG003_chr20_bwamem2"
    assert ba.raw_keys("S_bwa") == ["raw_reads/S_bwa_R1.fastq.gz", "raw_reads/S_bwa_R2.fastq.gz"]


def test_sample_ids_parse_as_paired_samples_in_the_trigger(trigger):
    for aligner in ba.ALIGNERS:
        sid = ba.sample_id("HG003_chr20", aligner)
        for key in ba.raw_keys(sid):
            assert trigger.parse_sample_id(key) == sid


def test_index_extensions_match_the_align_stage():
    assert ba.INDEX_EXTS == tasks.ALIGNER_INDEX_EXTS
    assert set(ba.ALIGNERS) == set(tasks.ALIGNER_INDEX_EXTS)


def test_measured_wraps_the_command_and_reports_memory():
    cmd = ba.measured(["python", "tasks.py", "align", "S", "r.fa"])
    assert cmd[:4] == ["bash", "-c", ba.MEASURE_SCRIPT, "measure"]
    assert cmd[4:] == ["python", "tasks.py", "align", "S", "r.fa"]
    assert "/sys/fs/cgroup/memory.stat" in ba.MEASURE_SCRIPT and "memory.peak" in ba.MEASURE_SCRIPT
    assert 'wait "$pid"' in ba.MEASURE_SCRIPT and "exit $rc" in ba.MEASURE_SCRIPT


def test_parse_measurements_takes_the_last_aligner_timer():
    m = ba.parse_measurements(MEASURED_OUTPUT)
    assert m == {"peak_anon_bytes": 7516192768, "cgroup_peak_bytes": 12884901888, "markdup_reads": 33475012,
                 "markdup_duplicates": 2911218, "aligner_real_s": 812.345, "aligner_cpu_s": 11893.21}
    assert ba.parse_measurements("nothing here") == {}


BWA_MEM2_PROFILE = """[0000] Computation ends..
Runtime profile:
\tTime taken for main_mem function: 459.83 sec
\tIO times (sec) :
\tWriting IO time (SAM) avg: 158.72, (158.72, 158.72)
"""


def test_parse_measurements_reads_bwa_mem2_profile():
    assert ba.parse_measurements(BWA_MEM2_PROFILE) == {"aligner_real_s": 459.83, "aligner_write_s": 158.72}


def test_load_measured_refreshes_saved_runs_from_their_logs(tmp_path):
    jpath, log = tmp_path / "align_x.json", tmp_path / "align_x.log"
    assert ba.load_measured(str(jpath), str(log)) is None
    ba.save_json(str(jpath), {"wall_s": 521.0, "markdup_reads": 1})
    log.write_text(BWA_MEM2_PROFILE)
    assert ba.load_measured(str(jpath), str(log)) == {"wall_s": 521.0, "markdup_reads": 1,
                                                      "aligner_real_s": 459.83, "aligner_write_s": 158.72}


def test_fastq_from_bam_writes_partial_files_then_renames():
    script = ba.fastq_from_bam_command("alignments/X.bam", "raw_reads/A_R1.fastq.gz", "raw_reads/A_R2.fastq.gz")[-1]
    assert script.startswith("set -e -o pipefail; trap 'rm -f /data/raw_reads/.partial-A_R1.fastq.gz")
    assert "samtools collate -u -O -@ 4 /data/alignments/X.bam" in script
    assert "samtools fastq -n -F 0x900" in script and "-s /dev/null" in script
    # temporary names keep the .gz suffix (samtools picks compression from it) and are renamed last
    assert script.index("-1 /data/raw_reads/.partial-A_R1.fastq.gz") < \
        script.index("mv /data/raw_reads/.partial-A_R1.fastq.gz /data/raw_reads/A_R1.fastq.gz")


def test_index_and_align_commands():
    assert ba.index_command("bwa-mem2", "ref.fa") == ["bash", "-c", "cd /data/reference && bwa-mem2 index ref.fa"]
    assert ba.align_command("S_bwa", "ref.fa") == ["python", "tasks.py", "align", "S_bwa", "ref.fa"]


def _happy(snp_fn, snp_fp, indel_fn, indel_fp):
    def row(fn, fp):
        return {"truth": 100, "tp": 100 - fn, "fn": fn, "fp": fp, "recall": 0.9, "precision": 0.9, "f1": 0.99}
    return {"SNP": row(snp_fn, snp_fp), "INDEL": row(indel_fn, indel_fp)}


def test_csv_rows_one_per_aligner_with_calls_and_concordance(tmp_path):
    align = {"bwa": {"sample": "S_bwa", "wall_s": 1500.04, "threads": "16", "peak_anon_bytes": 6 * ba.MIB,
                     "cgroup_peak_bytes": 9 * ba.MIB, "aligner_real_s": 1400.0, "aligner_cpu_s": 20000.0,
                     "markdup_reads": 10, "markdup_duplicates": 1, "bam_bytes": 123},
             "bwa-mem2": {"sample": "S_bwamem2", "wall_s": 800.0, "peak_anon_bytes": 12 * ba.MIB}}
    index = {"bwa": {"wall_s": 52.04, "peak_anon_bytes": 5 * ba.MIB}, "bwa-mem2": {}}
    happy = {"bwa": {"deepvariant": _happy(263, 52, 48, 15)}, "bwa-mem2": {"deepvariant": _happy(260, 50, 48, 15)}}
    concord = {"deepvariant": (80000, 30, 25)}
    rows = ba.csv_rows(list(ba.ALIGNERS), {"bwa": "0.7.18", "bwa-mem2": "2.2.1"}, align, index, happy, concord)
    assert [r["aligner"] for r in rows] == ["bwa", "bwa-mem2"]
    bwa, mem2 = rows
    assert bwa["align_stage_wall_s"] == 1500.0 and bwa["align_peak_anon_mib"] == 6
    assert bwa["index_build_wall_s"] == 52.0 and mem2["index_build_wall_s"] == ""
    assert bwa["deepvariant_snp_fn"] == 263 and mem2["deepvariant_snp_fp"] == 50
    assert bwa["deepvariant_calls_shared"] == mem2["deepvariant_calls_shared"] == 80000
    assert (bwa["deepvariant_calls_only_this"], mem2["deepvariant_calls_only_this"]) == (30, 25)

    path = tmp_path / "out.csv"
    ba.write_csv(str(path), rows)
    with open(path, newline="") as fh:
        read = list(csv.DictReader(fh))
    assert read[1]["aligner"] == "bwa-mem2" and read[0]["version"] == "0.7.18"

    text = ba.summary_markdown("S", rows, concord)
    assert "| bwa | 0.7.18 | 1500.0 | 1400.0 | 20000.0 | 6 | 9 | 52.0 |" in text
    assert "| bwa-mem2 | deepvariant | 260 / 50 |" in text
    assert "| deepvariant | 80,000 | 30 | 25 |" in text


def test_concordance_compares_pass_calls(tmp_path):
    def vcf(name, rows):
        path = tmp_path / "variants" / f"{name}.vcf.gz"
        path.parent.mkdir(exist_ok=True)
        body = "##fileformat=VCFv4.2\n" + "".join(f"chr20\t{p}\t.\t{r}\t{a}\t50\t{f}\t.\n" for p, r, a, f in rows)
        path.write_bytes(gzip.compress(body.encode()))
    vcf("A", [(1, "A", "G", "PASS"), (2, "C", "T", "PASS"), (3, "G", "A", "LowQual")])
    vcf("B", [(1, "A", "G", "PASS"), (4, "T", "C", "PASS"), (3, "G", "A", "PASS")])
    assert ba.concordance(str(tmp_path), "bcftools", "A", "B") == (1, 1, 2)


def test_ensure_truth_copies_once(tmp_path):
    (tmp_path / "truth").mkdir()
    (tmp_path / "truth" / "B.vcf.gz").write_bytes(b"v")
    (tmp_path / "truth" / "B.bed").write_text("chr20\t0\t1\n")
    ba.ensure_truth(str(tmp_path), "B", "B_bwa")
    assert (tmp_path / "truth" / "B_bwa.vcf.gz").read_bytes() == b"v"
    (tmp_path / "truth" / "B_bwa.bed").write_text("kept")
    ba.ensure_truth(str(tmp_path), "B", "B_bwa")
    assert (tmp_path / "truth" / "B_bwa.bed").read_text() == "kept"


def test_reads_are_required_without_from_bam(tmp_path):
    with pytest.raises(SystemExit):
        ba.main(["--root", str(tmp_path), "--base", "B", "--reference", "r.fa"])
