"""run_local.py: caller selection, docker commands and truth checks (no Docker needed)."""
import gzip
import json

import run_local


def names(jobs):
    return [j["name"] for j in jobs]


def test_default_caller_runs_exactly_the_state_machine_stages():
    jobs = run_local.pipeline_jobs("S1", "ref.fa")
    assert [j["cmd"] for j in jobs] == [
        run_local.stage_command(stage, "S1", "ref.fa", needs_ref) for stage, needs_ref in run_local.STAGES
    ]
    assert all(j["image"] == "geyser-app" and not j["gpus"] for j in jobs)


def test_deepvariant_caller_replaces_variants_stage_and_uses_gpu():
    jobs = run_local.pipeline_jobs("S1", "ref.fa", caller="deepvariant")
    assert names(jobs) == ["decompress", "qc", "align", "deepvariant"]
    dv = jobs[-1]
    assert dv["image"] == "geyser-deepvariant" and dv["gpus"]
    assert dv["cmd"] == ["/opt/geyser-venv/bin/python", "deepvariant_task.py", "S1", "ref.fa"]


def test_both_callers_run_bcftools_then_deepvariant():
    assert names(run_local.pipeline_jobs("S1", "ref.fa", caller="both")) == [
        "decompress", "qc", "align", "variants", "deepvariant"]


def test_skip_to_variants_starts_at_variant_calling():
    assert names(run_local.pipeline_jobs("S1", "r", caller="deepvariant", skip_to_variants=True)) == ["deepvariant"]
    assert names(run_local.pipeline_jobs("S1", "r", caller="both", skip_to_variants=True)) == [
        "variants", "deepvariant"]


def test_docker_command_adds_gpus_only_when_asked():
    with_gpu = run_local.docker_command(["x"], "root", "img", gpus=True)
    without = run_local.docker_command(["x"], "root", "img")
    assert with_gpu[:5] == ["docker", "run", "--rm", "--gpus", "all"]
    assert "--gpus" not in without
    assert without[-2:] == ["img", "x"]
    assert "STORAGE_BACKEND=local" in without and "LOCAL_STORAGE_DIR=/data" in without


def test_no_gpu_flag_reaches_the_job():
    jobs = run_local.pipeline_jobs("S1", "r", caller="deepvariant", gpus=False)
    assert not jobs[-1]["gpus"]


def write_vcf(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS\n"
    body += "".join(f"c\t{pos}\t.\t{ref}\t{alt}\t50\t{flt}\t.\tGT\t1/1\n" for pos, ref, alt, flt in rows)
    path.write_bytes(gzip.compress(body.encode()))


def test_read_vcf_calls_skips_refcall_and_placeholder_alts(tmp_path):
    vcf = tmp_path / "a.vcf.gz"
    write_vcf(vcf, [(10, "A", "G", "PASS"), (20, "C", "T", "RefCall"), (30, "G", "<*>", "."), (40, "T", "A,C", ".")])
    assert run_local.read_vcf_calls(vcf) == {(10, "A", "G"), (40, "T", "A"), (40, "T", "C")}


def test_check_against_truth_per_caller(tmp_path):
    (tmp_path / "truth").mkdir()
    (tmp_path / "truth" / "S1.json").write_text(json.dumps({"snps": [{"pos": 10, "ref": "A", "alt": "G"}]}))
    write_vcf(tmp_path / "variants" / "S1.vcf.gz", [(10, "A", "G", ".")])
    write_vcf(tmp_path / "variants_deepvariant" / "S1.vcf.gz", [(10, "A", "G", "PASS"), (99, "C", "T", "PASS")])
    assert run_local.check_against_truth(str(tmp_path), "S1", "bcftools")
    assert not run_local.check_against_truth(str(tmp_path), "S1", "deepvariant")  # one extra call


def test_giab_truth_is_counted_inside_bed_only(tmp_path):
    write_vcf(tmp_path / "truth" / "S1.vcf.gz", [(150, "A", "G", "PASS"), (160, "C", "T", "PASS"), (999, "G", "A", "PASS")])
    (tmp_path / "truth" / "S1.bed").write_text("c\t100\t200\n")
    write_vcf(tmp_path / "variants_deepvariant" / "S1.vcf.gz", [(150, "A", "G", "PASS"), (170, "T", "C", "PASS"),
                                                                (999, "G", "A", "PASS")])
    counts = run_local.truth_counts(str(tmp_path), "S1", "deepvariant")
    assert (counts["kind"], counts["truth"], counts["tp"]) == ("giab", 2, 1)
    assert len(counts["missed"]) == 1 and len(counts["extra"]) == 1
    assert run_local.check_against_truth(str(tmp_path), "S1", "deepvariant")  # report only


def test_missing_truth_fails_the_check(tmp_path):
    assert not run_local.check_against_truth(str(tmp_path), "S1")
