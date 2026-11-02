"""scripts/benchmark_calls.py and the hg003-chr20-vg dataset (no Docker needed)."""

import pytest

import benchmark_calls as bc
import benchmark_giraffe as bg
import fetch_dv_testdata as fetch


def test_vg_dataset_is_googles_giraffe_bam_with_hg003_truth():
    vg, bwa = fetch.DATASETS["hg003-chr20-vg"], fetch.DATASETS["hg003-chr20"]
    url, size, key, how = vg["files"][0]
    assert url == "vg-case-study/HG003.novaseq.pcr-free.35x.vg-1.55.0.chr20.bam" and size == 947058313
    assert key == "alignments/HG003_chr20_vg.bam" and how == "copy" and vg["sample"] == "HG003_chr20_vg"
    assert vg["reference"] == bwa["reference"] and "hg003-chr20-vg" in fetch.GRCH38_DATASETS
    # same truth files as the BWA case study, under the sample's own name
    assert [f[0] for f in vg["files"][1:]] == [f[0] for f in bwa["files"][1:]]
    assert [f[2] for f in vg["files"][2:]] == ["truth/HG003_chr20_vg.vcf.gz", "truth/HG003_chr20_vg.bed"]
    assert fetch.dataset_files("hg003-chr20-vg", pangenome="chr20")[-1][2] == "pangenome/chroms/chr20.vg"


def test_google_preset_pairs_each_caller_on_both_alignments():
    configs = bc.PRESETS["google-case-studies"]["configs"]
    assert bc.PRESETS["google-case-studies"]["base"] == "HG003_chr20"
    by_source = {}
    for label, aligner, sid, source, caller, env, memory in configs:
        by_source.setdefault(source, []).append((caller, env.get("DEEPVARIANT_EXTRA_ARGS", "")))
        assert aligner == ("giraffe" if source.endswith("_vg") else "bwa")
        assert label.lower().startswith(aligner)
        # a configuration on its own sample name is a hard link of its alignment (MAPQ>=5 runs)
        assert (sid == source) != sid.endswith("_mq5")
        assert memory == ("14g" if caller == "pangenome" else "12g")
    assert set(by_source) == {"HG003_chr20", "HG003_chr20_vg"}
    for runs in by_source.values():
        assert [c for c, _ in runs] == ["deepvariant", "pangenome", "pangenome"]
        assert runs[2][1].endswith("min_mapping_quality=5")
    # linear DV on giraffe reads uses Google's VG flags; on BWA reads the defaults
    assert by_source["HG003_chr20"][0][1] == "" and by_source["HG003_chr20_vg"][0][1] == bg.VG_LINEAR_DV_ARGS


def test_mq5_settings_extend_the_pangenome_defaults():
    assert bc.PANGENOME_MQ5["DEEPVARIANT_SHARDS"] == bg.PANGENOME_ENV["DEEPVARIANT_SHARDS"]
    assert bc.PANGENOME_MQ5["DEEPVARIANT_EXTRA_ARGS"].startswith(bg.PANGENOME_ENV["DEEPVARIANT_EXTRA_ARGS"])
    assert "min_mapping_quality=5" not in bg.PANGENOME_ENV["DEEPVARIANT_EXTRA_ARGS"]


def test_summary_totals_errors():
    row = {
        "configuration": "giraffe + pangenome DV",
        "snp_fn": 72,
        "snp_fp": 106,
        "snp_f1": 0.998732,
        "indel_fn": 34,
        "indel_fp": 24,
        "indel_f1": 0.997317,
        "segdups_snp_errors": 10,
        "lowmappabilityall_snp_errors": 60,
    }
    text = bc.summary_markdown("p", [row])
    assert "| giraffe + pangenome DV | 72 / 106 | 0.998732 | 34 / 24 | 0.997317 | 236 | 10 | 60 |" in text


def test_missing_alignment_stops_before_any_run(tmp_path):
    with pytest.raises(SystemExit):
        bc.main(["--root", str(tmp_path), "--preset", "google-case-studies", "--reference", "r.fa"])


def test_error_total_is_blank_when_a_count_is_missing():
    row = {"snp_fn": 1, "snp_fp": 2, "indel_fn": 3, "indel_fp": 4}
    assert bc.error_total(row) == 10
    assert bc.error_total({**row, "indel_fp": ""}) == ""


def test_missing_inputs_lists_everything_up_front(tmp_path):
    preset = bc.PRESETS["google-case-studies"]
    missing = bc.missing_inputs(str(tmp_path), preset, "GRCh38_chr20.fa")
    assert "truth/HG003_chr20.vcf.gz" in missing and "reference/GRCh38_chr20.fa" in missing
    assert any(m.startswith("pangenome/") for m in missing)
    assert "alignments/HG003_chr20.cram or .bam" in missing and "alignments/HG003_chr20_vg.cram or .bam" in missing


def test_r2_preset_compares_graphs_on_both_alignments():
    configs = [bc.unpack(c) for c in bc.PRESETS["hprc-r2"]["configs"]]
    r2 = [c for c in configs if c[7] == bc.R2_GRAPH]
    assert [c[2] for c in r2] == ["HG003_chr20_r2", "HG003_chr20_vg_r2"]  # own sample names: hard links
    assert all(c[4] == "pangenome" and c[3] + "_r2" == c[2] for c in r2)
    # each r2 run has its v1.1 counterpart on the same alignment, with the same settings
    for c in r2:
        twin = next(o for o in configs if o[3] == c[3] and o[4] == "pangenome" and o[7] is None)
        assert twin[5] == c[5] and twin[2] == c[3]
    assert bc.R2_GRAPH == fetch.PANGENOMES["r2-chr20"]["build"].split("/")[-1]


def test_unpack_defaults_the_graph():
    assert bc.unpack(("L", "bwa", "S", "S", "deepvariant", {}, "12g"))[7] is None
    assert bc.unpack(("L", "bwa", "S", "S", "pangenome", {}, "14g", "g.gbz"))[7] == "g.gbz"


def test_missing_inputs_lists_each_graph_once(tmp_path):
    missing = bc.missing_inputs(str(tmp_path), bc.PRESETS["hprc-r2"], "GRCh38_chr20.fa")
    graphs = [m for m in missing if m.startswith("pangenome/")]
    assert sorted(graphs) == sorted([f"pangenome/{bg_default()}", f"pangenome/{bc.R2_GRAPH}"])


def bg_default():
    import run_local

    return run_local.DEFAULT_PANGENOME
