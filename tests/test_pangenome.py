"""Pangenome-aware DeepVariant: stage, local runner, test-data fetch and hap.py benchmark (no Docker needed)."""

import gzip
import json

import pytest

import benchmark_happy
import deepvariant_task as dv
import fetch_dv_testdata as fetch
import run_local
from storage import ObjectNotFound

GRAPH = "hprc-v1.1-mc-grch38.gbz"

# run_pangenome_aware_deepvariant output: same banners as run_deepvariant, but its make_examples binary has a suffix.
PANGENOME_OUTPUT = [
    (0.0, dv.BANNER),
    (
        0.0,
        "time seq 0 7 | parallel -q --halt 2 --line-buffer "
        "/opt/deepvariant/bin/make_examples_pangenome_aware_dv --mode calling --ref /w/ref.fa",
    ),
    (40.0, dv.BANNER),
    (
        40.0,
        "time /opt/deepvariant/bin/call_variants --outfile /w/i/cv.tfrecord.gz "
        "--examples /w/i/make_examples_pangenome_aware_dv.tfrecord@8.gz",
    ),
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
    cmd = dv.build_run_deepvariant_cmd("/w/ref.fa", "/w/S1.bam", "/w/out.vcf.gz", "/w/i", "S1", 8, pangenome="/w/g.gbz")
    assert cmd[0] == dv.RUN_PANGENOME_DEEPVARIANT
    for flag in (
        "--model_type=WGS",
        "--ref=/w/ref.fa",
        "--reads=/w/S1.bam",
        "--pangenome=/w/g.gbz",
        "--output_vcf=/w/out.vcf.gz",
        "--num_shards=8",
        "--sample_name_reads=S1",
    ):
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
    assert set(recorded) == {
        "PangenomeDeepVariant",
        "PangenomeDeepVariant.MakeExamples",
        "PangenomeDeepVariant.CallVariants",
        "PangenomeDeepVariant.PostprocessVariants",
    }
    assert recorded["PangenomeDeepVariant"]["status"] == "Success"


def test_pangenome_stage_fails_without_graph(pg_inputs, monkeypatch):
    (pg_inputs / "pangenome" / GRAPH).unlink()
    monkeypatch.setattr(dv, "run_streaming", fake_pangenome_run([], b"x"))
    with pytest.raises(ObjectNotFound):
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


def test_pangenome_caller_replaces_variants_stage_and_uses_gpu_and_shm():
    jobs = run_local.pipeline_jobs("S1", "ref.fa", caller="pangenome")
    assert names(jobs) == ["qc", "align", "pangenome"]
    pg = jobs[-1]
    assert pg["image"] == "geyser-deepvariant-pangenome" and pg["gpus"] and pg["shm"] == "12g"
    assert pg["cmd"] == [
        "/opt/geyser-venv/bin/python",
        "deepvariant_task.py",
        "S1",
        "ref.fa",
        "--pangenome",
        "hprc-v1.1-mc-grch38.chr20.gbz",
    ]


def test_no_gpu_also_applies_to_pangenome():
    jobs = run_local.pipeline_jobs("S1", "r", caller="all", skip_to_variants=True, gpus=False)
    assert [j["gpus"] for j in jobs] == [False, False, False]


def test_pangenome_graph_can_be_overridden():
    job = run_local.pipeline_jobs("S1", "r", caller="pangenome", skip_to_variants=True, pangenome=GRAPH)[0]
    assert job["cmd"][-2:] == ["--pangenome", GRAPH]


def test_all_caller_runs_three_callers_in_order():
    assert names(run_local.pipeline_jobs("S1", "r", caller="all", skip_to_variants=True)) == [
        "variants",
        "deepvariant",
        "pangenome",
    ]
    assert run_local.CALLER_SETS["all"] == ["bcftools", "deepvariant", "pangenome"]


def test_existing_callers_unchanged():
    assert names(run_local.pipeline_jobs("S1", "r", caller="both", skip_to_variants=True)) == [
        "variants",
        "deepvariant",
    ]
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


def test_sample_keys_runs_a_paired_sample_once(tmp_path, trigger):
    raw = tmp_path / "raw_reads"
    raw.mkdir()
    for name in ("PAIR_R1.fastq.gz", "PAIR_R2.fastq.gz", "SINGLE.fastq.gz", "run_1.fastq.gz"):
        (raw / name).write_bytes(b"")
    assert run_local.sample_keys(trigger, str(tmp_path)) == [
        "raw_reads/PAIR.fastq.gz",
        "raw_reads/SINGLE.fastq.gz",
        "raw_reads/run_1.fastq.gz",
    ]
    assert run_local.sample_keys(trigger, str(tmp_path), "PAIR") == ["raw_reads/PAIR.fastq.gz"]
    assert run_local.sample_keys(trigger, str(tmp_path / "empty")) == []


def test_vcf_path_for_pangenome(tmp_path):
    assert (
        run_local.vcf_path(str(tmp_path), "pangenome", "S1").replace("\\", "/").endswith("variants_pangenome/S1.vcf.gz")
    )


# ---- test-data fetch (scripts/fetch_dv_testdata.py) ----


def test_pangenome_graph_added_only_on_request():
    plain = fetch.dataset_files("hg003-chr20")
    chr20 = fetch.dataset_files("hg003-chr20", pangenome="chr20")
    full = fetch.dataset_files("hg003-chr20", pangenome="full")
    assert len(chr20) == len(full) == len(plain) + 1
    url, size, key, how = chr20[-1]
    assert key == "pangenome/chroms/chr20.vg" and how == "copy" and size == 1279054691
    assert url.endswith("hprc-v1.1-mc-grch38.chroms/chr20.vg")
    url, size, key, how = full[-1]
    assert key == f"pangenome/{GRAPH}" and size == 3289083600
    assert url.startswith("https://s3-us-west-2.amazonaws.com/human-pangenomics/")


def test_chr20_graph_builds_to_the_runner_default():
    assert fetch.PANGENOMES["chr20"]["build"] == f"pangenome/{run_local.DEFAULT_PANGENOME}"
    assert "build" not in fetch.PANGENOMES["full"]


def test_vg_build_command_keeps_references_and_caps_memory(tmp_path):
    cmd = fetch.vg_build_command(str(tmp_path), "pangenome/chroms/chr20.vg", "pangenome/out.gbz")
    assert cmd[:5] == ["docker", "run", "--rm", "--memory", "8g"]
    assert fetch.VG_IMAGE in cmd
    script = cmd[-1]
    assert "vg convert -f /data/pangenome/chroms/chr20.vg" in script
    assert "--gbz-format" in script and "--set-reference GRCh38" in script and "--set-reference CHM13" in script
    assert script.index("mv /data/pangenome/out.gbz.part /data/pangenome/out.gbz") > script.index("vg gbwt")


def test_pangenome_refused_for_non_grch38_dataset():
    with pytest.raises(ValueError, match="GRCh38"):
        fetch.dataset_files("na12878-slice", pangenome="chr20")


def test_remote_url_keeps_absolute_urls():
    assert fetch.remote_url("https://example.org/a.gbz") == "https://example.org/a.gbz"
    assert fetch.remote_url("case-study-testdata/x.bam") == fetch.BASE_URL + "case-study-testdata/x.bam"


# ---- hap.py benchmark (scripts/benchmark_happy.py) ----


def test_happy_command_matches_google_case_study_settings():
    cmd = benchmark_happy.happy_command(
        "HG003_chr20",
        "GRCh38_chr20.fa",
        "variants_pangenome/HG003_chr20.vcf.gz",
        "benchmarks/HG003_chr20/pangenome",
        region="chr20",
    )
    assert cmd[:3] == [
        benchmark_happy.HAPPY_BIN,
        "/data/truth/HG003_chr20.vcf.gz",
        "/data/variants_pangenome/HG003_chr20.vcf.gz",
    ]
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
    assert r["SNP"] == {
        "truth": 70166,
        "tp": 69926,
        "fn": 240,
        "fp": 76,
        "recall": 0.99658,
        "precision": 0.998915,
        "f1": 0.997746,
    }


def test_summary_table_lists_callers_in_order(tmp_path):
    path = tmp_path / "x.summary.csv"
    path.write_text(SUMMARY_CSV)
    r = benchmark_happy.read_summary(str(path))
    table = benchmark_happy.summary_table({"pangenome": r, "bcftools": r})
    rows = table.splitlines()[2:]
    assert [row.split(" | ")[0] for row in rows] == [
        "| bcftools",
        "| bcftools",
        "| Pangenome-aware DeepVariant",
        "| Pangenome-aware DeepVariant",
    ]
    assert "69,926" in table and "0.997746" in table


def test_write_fai_matches_samtools_layout(tmp_path):
    fasta = tmp_path / "ref.fa"
    fasta.write_bytes(b">chr20 description\nACGTACGTAC\nACGTAC\n>chrM\nGGGG\n")
    benchmark_happy.write_fai(str(fasta))
    assert (tmp_path / "ref.fa.fai").read_text() == "chr20\t16\t19\t10\t11\nchrM\t4\t43\t4\t5\n"


# ---- HG005 / HG003 30x datasets and GIAB stratifications (#51 next steps) ----


def test_hg005_and_its_hg003_control_come_from_the_same_bam_set():
    hg005 = fetch.DATASETS["hg005-chr20"]["files"]
    control = fetch.DATASETS["hg003-30x-chr20"]["files"]
    (url5, size5, key5, how5), (url3, _, key3, how3) = hg005[0], control[0]
    assert url5.startswith(fetch.BRAIN_30X) and url3.startswith(fetch.BRAIN_30X)
    assert url5.endswith("HG005.novaseq.pcr-free.30x.dedup.grch38.bam") and size5 is None
    assert url3.endswith("HG003.novaseq.pcr-free.30x.dedup.grch38.bam")
    assert (key5, key3) == ("alignments/HG005_chr20.bam", "alignments/HG003_30x_chr20.bam")
    assert how5 == how3 == "region:chr20"
    # same reference FASTA as hg003-chr20, so the chr20 graph and existing .fai are shared
    for name in ("hg005-chr20", "hg003-30x-chr20"):
        ds = fetch.DATASETS[name]
        assert ds["reference"] == "GRCh38_chr20.fa" and name in fetch.GRCH38_DATASETS
        assert [k for _, _, k, _ in ds["files"]][1:] == [
            "reference/GRCh38_chr20.fa",
            f"truth/{ds['sample']}.vcf.gz",
            f"truth/{ds['sample']}.bed",
        ]
    assert hg005[2][0].startswith(fetch.GIAB) and hg005[2][0].endswith("HG005_GRCh38_1_22_v4.2.1_benchmark.vcf.gz")
    # the control's truth is the HG003 truth hg003-chr20 uses
    assert [f[0] for f in control[2:]] == [f[0] for f in fetch.DATASETS["hg003-chr20"]["files"][2:]]


def test_pangenome_accepted_for_new_grch38_datasets():
    files = fetch.dataset_files("hg005-chr20", pangenome="chr20")
    assert files[-1][2] == "pangenome/chroms/chr20.vg"


def test_bam_region_command_fetches_one_contig_atomically(tmp_path):
    cmd = fetch.bam_region_command(str(tmp_path), "https://h/x.bam", "alignments/S.bam", "chr20")
    assert cmd[:3] == ["docker", "run", "--rm"] and fetch.APP_IMAGE in cmd
    script = cmd[-1]
    assert "samtools view -b -o /data/alignments/S.bam.part https://h/x.bam chr20" in script
    assert script.index("samtools quickcheck /data/alignments/S.bam.part") < script.index(
        "mv /data/alignments/S.bam.part /data/alignments/S.bam"
    )
    assert script.startswith("set -e; cd /tmp")  # the downloaded remote .bai lands outside /data
    assert fetch.bam_region_command(str(tmp_path), "u", "k", "c", image="other")[-4] == "other"


def test_stratifications_only_on_request_and_only_for_grch38():
    plain = fetch.dataset_files("hg005-chr20")
    strat = fetch.dataset_files("hg005-chr20", stratifications=True)
    added = strat[len(plain) :]
    assert len(added) == len(fetch.STRATIFICATIONS)
    for (name, path), (url, size, key, how) in zip(fetch.STRATIFICATIONS, added, strict=True):
        assert url == fetch.STRAT_BASE + path and size is None
        assert key == f"stratifications/GRCh38_chr20/{name}.bed" and how == "only:chr20"
    with pytest.raises(ValueError, match="--stratifications needs a GRCh38"):
        fetch.dataset_files("na12878-slice", stratifications=True)


def test_stratification_tsv_paths_are_relative_to_the_tsv():
    key, text = fetch.stratification_tsv("chr20")
    assert key == "stratifications/GRCh38_chr20.tsv"
    lines = text.splitlines()
    assert lines[0] == "alldifficultregions\tGRCh38_chr20/alldifficultregions.bed"
    assert len(lines) == len(fetch.STRATIFICATIONS)


def test_dataset_contig_from_reference_entry():
    assert fetch.dataset_contig("hg005-chr20") == "chr20"
    with pytest.raises(ValueError):
        fetch.dataset_contig("na12878-slice")


def test_keep_contig_gunzips_a_bed_gz_to_plain_bed(tmp_path):
    src = tmp_path / "in.bed.gz"
    src.write_bytes(gzip.compress(b"chr1\t0\t5\nchr20\t10\t20\n"))
    assert fetch.keep_contig(str(src), str(tmp_path / "out.bed"), "chr20") == 1
    assert (tmp_path / "out.bed").read_bytes() == b"chr20\t10\t20\n"


def test_happy_command_adds_stratification():
    cmd = benchmark_happy.happy_command("S", "r.fa", "v.vcf.gz", "o", stratification="stratifications/GRCh38_chr20.tsv")
    assert cmd[cmd.index("--stratification") + 1] == "/data/stratifications/GRCh38_chr20.tsv"
    assert "--stratification" not in benchmark_happy.happy_command("S", "r.fa", "v.vcf.gz", "o")


EXTENDED_CSV = (
    "Type,Subtype,Subset,Filter,METRIC.F1_Score,TRUTH.TOTAL,TRUTH.FN,QUERY.FP\n"
    "SNP,*,*,PASS,0.997746,70166.0,240.0,76.0\n"
    "SNP,*,*,ALL,0.99,70166.0,240.0,99.0\n"
    "SNP,ti,*,PASS,0.998,47000.0,100.0,40.0\n"
    "SNP,*,segdups,PASS,0.9812,1500.0,30.0,27.0\n"
    "SNP,*,TS_contained,PASS,0.997,70000.0,239.0,70.0\n"
    "INDEL,*,segdups,PASS,,0.0,0.0,2.0\n"
)


def test_read_extended_keeps_pass_all_subtypes_for_named_regions(tmp_path):
    path = tmp_path / "x.extended.csv"
    path.write_text(EXTENDED_CSV)
    r = benchmark_happy.read_extended(str(path), ["*", "segdups"])
    assert set(r) == {("*", "SNP"), ("segdups", "SNP"), ("segdups", "INDEL")}
    assert r[("segdups", "SNP")] == {"truth": 1500, "fn": 30, "fp": 27, "f1": 0.9812}
    assert r[("segdups", "INDEL")] == {"truth": 0, "fn": 0, "fp": 2, "f1": None}


def test_stratified_table_one_row_per_region_and_type(tmp_path):
    path = tmp_path / "x.extended.csv"
    path.write_text(EXTENDED_CSV)
    r = benchmark_happy.read_extended(str(path), ["*", "segdups"])
    table = benchmark_happy.stratified_table({"pangenome": r, "deepvariant": r}, ["*", "segdups"])
    lines = table.splitlines()
    assert "DeepVariant (linear) FN / FP (F1)" in lines[0]
    assert lines[0].index("DeepVariant (linear)") < lines[0].index("Pangenome-aware")
    assert lines[2].startswith("| all confident regions | SNP | 70,166 | 240 / 76 (0.99775)")
    assert lines[3].startswith("| segdups | SNP | 1,500 | 30 / 27 (0.98120)")
    assert lines[4] == "| segdups | INDEL | 0 | 0 / 2 | 0 / 2 |"
    assert len(lines) == 5


def test_read_stratification_names(tmp_path):
    tsv = tmp_path / "s.tsv"
    tsv.write_text("segdups\tGRCh38_chr20/segdups.bed\n\nlowmap\tGRCh38_chr20/lowmap.bed\n")
    assert benchmark_happy.read_stratification_names(str(tsv)) == ["segdups", "lowmap"]


def test_bam_region_command_removes_partial_file_on_failure(tmp_path):
    script = fetch.bam_region_command(str(tmp_path), "https://h/x.bam", "alignments/S.bam", "chr20")[-1]
    assert "trap 'rm -f /data/alignments/S.bam.part' ERR" in script
    assert script.index("trap") < script.index("samtools view")


def test_region_fetch_needs_the_app_image(tmp_path, monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return type("R", (), {"returncode": 1})()

    monkeypatch.setattr(fetch.subprocess, "run", fake_run)
    monkeypatch.setattr(
        fetch, "dataset_files", lambda *a: [("https://h/x.bam", None, "alignments/S.bam", "region:chr20")]
    )
    with pytest.raises(RuntimeError, match="docker build"):
        fetch.fetch_dataset("hg005-chr20", str(tmp_path), app_image="missing-image")
    assert calls == [["docker", "image", "inspect", "missing-image"]]


def test_number_treats_nan_as_missing():
    assert benchmark_happy._number("nan", int) is None
    assert benchmark_happy._number("NaN") is None
    assert benchmark_happy._number("12.0", int) == 12


def test_read_stratification_rejects_a_line_without_a_tab(tmp_path):
    tsv = tmp_path / "s.tsv"
    tsv.write_text("segdups GRCh38_chr20/segdups.bed\n")
    with pytest.raises(ValueError, match="expected '<name><TAB><bed>'"):
        benchmark_happy.read_stratification(str(tsv))


def test_read_stratification_resolves_beds_next_to_the_tsv(tmp_path):
    tsv = tmp_path / "s.tsv"
    tsv.write_text("segdups\tGRCh38_chr20/segdups.bed\r\n")
    [(name, bed)] = benchmark_happy.read_stratification(str(tsv))
    assert name == "segdups"
    assert bed == str(tmp_path / "GRCh38_chr20" / "segdups.bed")


def test_stratification_with_missing_bed_stops_before_hap_py(tmp_path, monkeypatch):
    (tmp_path / "stratifications").mkdir()
    (tmp_path / "stratifications" / "GRCh38_chr20.tsv").write_text("segdups\tGRCh38_chr20/segdups.bed\n")
    ran = []
    monkeypatch.setattr(benchmark_happy.subprocess, "run", lambda *a, **k: ran.append(a))
    with pytest.raises(SystemExit):
        benchmark_happy.main(
            [
                "--root",
                str(tmp_path),
                "--sample",
                "S",
                "--reference",
                "r.fa",
                "--stratification",
                r"stratifications\GRCh38_chr20.tsv",
            ]
        )
    assert ran == []


def test_r2_graph_is_the_benchmark_build_cut_from_its_gbz(tmp_path):
    graph = fetch.PANGENOMES["r2-chr20"]
    assert "benchmark-graphs/hprc-v2.0-mc-grch38-eval/" in graph["url"] and graph["url"].endswith("-eval.gbz")
    assert graph["size"] == 5386150200 and graph["from"] == "gbz" and graph["path"] == "GRCh38#0#chr20"
    assert graph["build"] == "pangenome/hprc-v2.0-mc-grch38-eval.chr20.gbz"
    script = fetch.gbz_chunk_command(str(tmp_path), graph["key"], graph["build"], graph["path"])[-1]
    assert f"vg chunk -x /data/{graph['key']} -p GRCh38#0#chr20 -C -O gfa" in script
    assert "--set-reference GRCh38 --set-reference CHM13" in script
    assert script.index("vg gbwt -G") < script.index(f"mv /data/{graph['build']}.part /data/{graph['build']}")
    assert fetch.dataset_files("hg003-chr20", pangenome="r2-chr20")[-1][2] == graph["key"]
