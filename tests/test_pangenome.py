"""Pangenome-aware DeepVariant: stage, local runner, test-data fetch and hap.py benchmark (no Docker needed)."""
import gzip
import json

import pytest

import benchmark_happy
import deepvariant_task as dv
import fetch_dv_testdata as fetch
import run_local

GRAPH = "hprc-v1.1-mc-grch38.gbz"

# run_pangenome_aware_deepvariant output: same banners as run_deepvariant, but its make_examples binary has a suffix.
PANGENOME_OUTPUT = [
    (0.0, dv.BANNER),
    (0.0, "time seq 0 7 | parallel -q --halt 2 --line-buffer "
          "/opt/deepvariant/bin/make_examples_pangenome_aware_dv --mode calling --ref /w/ref.fa"),
    (40.0, dv.BANNER),
    (40.0, "time /opt/deepvariant/bin/call_variants --outfile /w/i/cv.tfrecord.gz "
           "--examples /w/i/make_examples_pangenome_aware_dv.tfrecord@8.gz"),
    (70.0, dv.BANNER),
    (70.0, "time /opt/deepvariant/bin/postprocess_variants --ref /w/ref.fa --infile /w/i/cv.tfrecord.gz"),
]


# ---- the stage (app-deepvariant/deepvariant_task.py) ----

def test_pangenome_keys_follow_storage_contract():
    assert dv.deepvariant_keys("S1", GRAPH) == {
        "bam": "alignments/S1.bam",
        "vcf": "variants_pangenome/S1.vcf.gz",
        "pangenome": f"pangenome/{GRAPH}",
    }
    assert dv.deepvariant_keys("S1")["vcf"] == "variants_deepvariant/S1.vcf.gz"  # linear path unchanged


def test_pangenome_command_uses_its_own_binary_and_flags():
    cmd = dv.build_run_deepvariant_cmd("/w/ref.fa", "/w/S1.bam", "/w/out.vcf.gz", "/w/i", "S1", 8,
                                       pangenome="/w/g.gbz")
    assert cmd[0] == dv.RUN_PANGENOME_DEEPVARIANT
    for flag in ("--model_type=WGS", "--ref=/w/ref.fa", "--reads=/w/S1.bam", "--pangenome=/w/g.gbz",
                 "--output_vcf=/w/out.vcf.gz", "--num_shards=8", "--sample_name_reads=S1"):
        assert flag in cmd
    # run_pangenome_aware_deepvariant 1.9.0 has no --sample_name flag
    assert not any(c.startswith("--sample_name=") for c in cmd)


def test_linear_command_has_no_pangenome_flags():
    cmd = dv.build_run_deepvariant_cmd("/w/ref.fa", "/w/S1.bam", "/w/out.vcf.gz", "/w/i", "S1", 8)
    assert cmd[0] == dv.RUN_DEEPVARIANT
    assert "--sample_name=S1" in cmd
    assert not any(c.startswith(("--pangenome", "--sample_name_reads")) for c in cmd)


def test_step_timings_recognise_the_suffixed_make_examples_binary():
    timings = dv.parse_step_timings(PANGENOME_OUTPUT, end_time=75.0)
    assert timings == {"make_examples": 40.0, "call_variants": 30.0, "postprocess_variants": 5.0}


@pytest.fixture
def pg_inputs(local_env, monkeypatch):
    (local_env / "alignments").mkdir(parents=True)
    (local_env / "alignments" / "S1.bam").write_bytes(b"BAM\x01fake")
    (local_env / "reference").mkdir()
    (local_env / "reference" / "ref.fa").write_text(">c\nACGT\n")
    (local_env / "reference" / "ref.fa.fai").write_text("c\t4\t3\t4\t5\n")
    (local_env / "pangenome").mkdir()
    (local_env / "pangenome" / GRAPH).write_bytes(b"GBZ graph")
    monkeypatch.setattr(dv, "index_bam", lambda bam: None)
    monkeypatch.setattr(dv, "gpu_name", lambda: None)
    return local_env


def fake_pangenome_run(seen, vcf_bytes, code=0):
    def run(cmd):
        seen.append(cmd)
        graph = next(c.split("=", 1)[1] for c in cmd if c.startswith("--pangenome="))
        with open(graph, "rb") as fh:
            assert fh.read() == b"GBZ graph"  # the graph was fetched from storage before the run
        out = next(c.split("=", 1)[1] for c in cmd if c.startswith("--output_vcf="))
        with open(out, "wb") as fh:
            fh.write(vcf_bytes)
        return code, PANGENOME_OUTPUT, 75.0
    return run


def metrics_lines(root):
    return [json.loads(line) for line in (root / "metrics" / "metrics.jsonl").read_text().splitlines()]


def test_pangenome_stage_uploads_vcf_and_records_its_own_metrics(pg_inputs, monkeypatch):
    vcf = gzip.compress(b"##fileformat=VCFv4.2\n")
    seen = []
    monkeypatch.setattr(dv, "run_streaming", fake_pangenome_run(seen, vcf))

    dv.pangenome_deepvariant_task("S1", "ref.fa", GRAPH)

    assert seen[0][0] == dv.RUN_PANGENOME_DEEPVARIANT
    assert (pg_inputs / "variants_pangenome" / "S1.vcf.gz").read_bytes() == vcf
    assert not (pg_inputs / "variants_deepvariant").exists()  # linear DeepVariant output untouched
    recorded = {m["task"]: m for m in metrics_lines(pg_inputs)}
    assert set(recorded) == {"PangenomeDeepVariant", "PangenomeDeepVariant.MakeExamples",
                             "PangenomeDeepVariant.CallVariants", "PangenomeDeepVariant.PostprocessVariants"}
    assert recorded["PangenomeDeepVariant"]["status"] == "Success"


def test_pangenome_stage_fails_without_graph(pg_inputs, monkeypatch):
    (pg_inputs / "pangenome" / GRAPH).unlink()
    monkeypatch.setattr(dv, "run_streaming", fake_pangenome_run([], b"x"))
    with pytest.raises(Exception):
        dv.pangenome_deepvariant_task("S1", "ref.fa", GRAPH)
    last = metrics_lines(pg_inputs)[-1]
    assert (last["task"], last["status"]) == ("PangenomeDeepVariant", "Failure")


def test_pangenome_stage_reports_tool_exit_code(pg_inputs, monkeypatch):
    monkeypatch.setattr(dv, "run_streaming", fake_pangenome_run([], b"x", code=2))
    with pytest.raises(RuntimeError, match="run_pangenome_aware_deepvariant exited with code 2"):
        dv.pangenome_deepvariant_task("S1", "ref.fa", GRAPH)


def test_cli_dispatches_on_pangenome_flag(monkeypatch):
    calls = []
    monkeypatch.setattr(dv, "deepvariant_task", lambda *a: calls.append(("linear",) + a))
    monkeypatch.setattr(dv, "pangenome_deepvariant_task", lambda *a: calls.append(("pangenome",) + a))
    dv.main(["S1", "ref.fa"])
    dv.main(["S1", "ref.fa", "--pangenome", GRAPH])
    assert calls == [("linear", "S1", "ref.fa"), ("pangenome", "S1", "ref.fa", GRAPH)]


# ---- local runner (scripts/run_local.py) ----

def names(jobs):
    return [j["name"] for j in jobs]


def test_pangenome_caller_replaces_variants_stage_and_runs_on_cpu_with_shm():
    jobs = run_local.pipeline_jobs("S1", "ref.fa", caller="pangenome")
    assert names(jobs) == ["decompress", "qc", "align", "pangenome"]
    pg = jobs[-1]
    assert pg["image"] == "geyser-deepvariant-pangenome" and not pg["gpus"] and pg["shm"] == "12g"
    assert pg["cmd"] == ["/opt/geyser-venv/bin/python", "deepvariant_task.py", "S1", "ref.fa",
                         "--pangenome", GRAPH]


def test_all_caller_runs_three_callers_in_order():
    assert names(run_local.pipeline_jobs("S1", "r", caller="all", skip_to_variants=True)) == [
        "variants", "deepvariant", "pangenome"]
    assert run_local.CALLER_SETS["all"] == ["bcftools", "deepvariant", "pangenome"]


def test_existing_callers_unchanged():
    assert names(run_local.pipeline_jobs("S1", "r", caller="both", skip_to_variants=True)) == [
        "variants", "deepvariant"]
    assert "pangenome" not in names(run_local.pipeline_jobs("S1", "r", caller="deepvariant"))


def test_docker_command_shm_only_when_given():
    assert run_local.docker_command(["x"], "root", "img", shm="12g")[3:5] == ["--shm-size", "12g"]
    assert "--shm-size" not in run_local.docker_command(["x"], "root", "img")


def test_pangenome_job_gets_memory_cap_and_shm(monkeypatch):
    seen = []
    monkeypatch.setattr(run_local.subprocess, "run", lambda cmd: seen.append(cmd) or type("R", (), {"returncode": 0}))
    for job in run_local.pipeline_jobs("S1", "r", caller="all", skip_to_variants=True, pg_shm="8g"):
        run_local.run_job(job, "docker", "root", dv_memory="20g")
    variants, deepvariant, pangenome = seen
    assert "--memory" not in variants and "--memory" in deepvariant and "--memory" in pangenome
    assert "--shm-size" in pangenome and "--shm-size" not in deepvariant
    assert pangenome[pangenome.index("--shm-size") + 1] == "8g"


def test_vcf_path_for_pangenome(tmp_path):
    assert run_local.vcf_path(str(tmp_path), "pangenome", "S1").replace("\\", "/").endswith(
        "variants_pangenome/S1.vcf.gz")


# ---- test-data fetch (scripts/fetch_dv_testdata.py) ----

def test_pangenome_graph_added_only_on_request():
    plain = fetch.dataset_files("hg003-chr20")
    with_graph = fetch.dataset_files("hg003-chr20", pangenome=True)
    assert len(with_graph) == len(plain) + 1
    url, size, key, how = with_graph[-1]
    assert key == f"pangenome/{GRAPH}" and how == "copy" and size == 3289083600
    assert url.startswith("https://s3-us-west-2.amazonaws.com/human-pangenomics/")


def test_pangenome_refused_for_non_grch38_dataset():
    with pytest.raises(ValueError, match="GRCh38"):
        fetch.dataset_files("na12878-slice", pangenome=True)


def test_remote_url_keeps_absolute_urls():
    assert fetch.remote_url("https://example.org/a.gbz") == "https://example.org/a.gbz"
    assert fetch.remote_url("case-study-testdata/x.bam") == fetch.BASE_URL + "case-study-testdata/x.bam"


# ---- hap.py benchmark (scripts/benchmark_happy.py) ----

def test_happy_command_matches_google_case_study_settings():
    cmd = benchmark_happy.happy_command("HG003_chr20", "GRCh38_chr20.fa", "variants_pangenome/HG003_chr20.vcf.gz",
                                        "benchmarks/HG003_chr20/pangenome", region="chr20")
    assert cmd[:3] == [benchmark_happy.HAPPY_BIN, "/data/truth/HG003_chr20.vcf.gz",
                       "/data/variants_pangenome/HG003_chr20.vcf.gz"]
    for part in ("--engine=vcfeval", "--pass-only"):
        assert part in cmd
    assert cmd[cmd.index("-f") + 1] == "/data/truth/HG003_chr20.bed"
    assert cmd[cmd.index("-r") + 1] == "/data/reference/GRCh38_chr20.fa"
    assert cmd[cmd.index("-l") + 1] == "chr20"
    assert "-l" not in benchmark_happy.happy_command("S", "r.fa", "v.vcf.gz", "o")


SUMMARY_CSV = (
    "Type,Filter,TRUTH.TOTAL,TRUTH.TP,TRUTH.FN,QUERY.TOTAL,QUERY.FP,QUERY.UNK,FP.gt,FP.al,"
    "METRIC.Recall,METRIC.Precision,METRIC.Frac_NA,METRIC.F1_Score,TRUTH.TOTAL.TiTv_ratio\n"
    "INDEL,ALL,10628,10579,49,20891,20,10236,6,8,0.99539,0.998191,0.48998,0.996788,\n"
    "INDEL,PASS,10628,10579,49,20891,20,10236,6,8,0.99539,0.998191,0.48998,0.996788,\n"
    "SNP,ALL,70166,69926,240,86703,76,16690,13,8,0.99658,0.998915,0.192496,0.997746,2.29\n"
    "SNP,PASS,70166,69926,240,86703,76,16690,13,8,0.99658,0.998915,0.192496,0.997746,2.29\n"
)


def test_read_summary_keeps_pass_rows(tmp_path):
    path = tmp_path / "x.summary.csv"
    path.write_text(SUMMARY_CSV)
    r = benchmark_happy.read_summary(str(path))
    assert set(r) == {"SNP", "INDEL"}
    assert r["SNP"] == {"truth": 70166, "tp": 69926, "fn": 240, "fp": 76,
                        "recall": 0.99658, "precision": 0.998915, "f1": 0.997746}


def test_summary_table_lists_callers_in_order(tmp_path):
    path = tmp_path / "x.summary.csv"
    path.write_text(SUMMARY_CSV)
    r = benchmark_happy.read_summary(str(path))
    table = benchmark_happy.summary_table({"pangenome": r, "bcftools": r})
    rows = table.splitlines()[2:]
    assert [row.split(" | ")[0] for row in rows] == ["| bcftools", "| bcftools",
                                                      "| Pangenome-aware DeepVariant", "| Pangenome-aware DeepVariant"]
    assert "69,926" in table and "0.997746" in table


def test_write_fai_matches_samtools_layout(tmp_path):
    fasta = tmp_path / "ref.fa"
    fasta.write_bytes(b">chr20 description\nACGTACGTAC\nACGTAC\n>chrM\nGGGG\n")
    benchmark_happy.write_fai(str(fasta))
    assert (tmp_path / "ref.fa.fai").read_text() == "chr20\t16\t19\t10\t11\nchrM\t4\t43\t4\t5\n"
