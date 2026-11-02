"""scripts/benchmark_giraffe.py helpers (no Docker needed)."""

import os

import pytest

import benchmark_giraffe as bg
import run_local

READS = ["raw_reads/S_bwa_R1.fastq.gz", "raw_reads/S_bwa_R2.fastq.gz"]
KEYS = bg.graph_keys("hprc-v1.1-mc-grch38.chr20.gbz")


def test_graph_keys_sit_next_to_the_graph():
    assert KEYS == {
        "gbz": "pangenome/hprc-v1.1-mc-grch38.chr20.gbz",
        "dist": "pangenome/hprc-v1.1-mc-grch38.chr20.dist",
        "ri": "pangenome/hprc-v1.1-mc-grch38.chr20.ri",
        "hapl": "pangenome/hprc-v1.1-mc-grch38.chr20.hapl",
        "paths": "pangenome/hprc-v1.1-mc-grch38.chr20.GRCh38.paths.txt",
    }


def test_index_commands_build_in_order_and_rename_when_complete():
    cmds = bg.vg_index_commands(KEYS, 8)
    assert list(cmds) == ["dist", "ri", "hapl", "paths"]  # hapl needs dist and ri
    for name, cmd in cmds.items():
        final = f"/data/{KEYS[name]}"
        assert cmd.endswith(f"mv {final}.part {final}")
    assert "vg index -t 8 -j" in cmds["dist"]
    assert "vg gbwt -p --num-threads 8 -r" in cmds["ri"]
    assert f"-d /data/{KEYS['dist']} -r /data/{KEYS['ri']} -H" in cmds["hapl"]
    assert "vg paths -x" in cmds["paths"] and "-L -Q GRCh38" in cmds["paths"]


def test_kmc_counts_both_mates_with_the_haplotype_kmer_length():
    script = bg.kmc_command(READS, "pangenome/kmc/S_bwa.kff", 16)[-1]
    assert "/data/raw_reads/S_bwa_R1.fastq.gz\\n/data/raw_reads/S_bwa_R2.fastq.gz" in script
    assert f"kmc -k{bg.KMER_LENGTH} -m8 -okff -t16 @/tmp/reads.txt /data/pangenome/kmc/S_bwa.part" in script
    assert script.endswith("mv /data/pangenome/kmc/S_bwa.part.kff /data/pangenome/kmc/S_bwa.kff")
    assert bg.KMER_LENGTH == 29


def test_giraffe_uses_google_case_study_flags():
    script = bg.giraffe_command("S_giraffe", READS, KEYS, "pangenome/kmc/S_bwa.kff", "alignments/u.bam", 16)[-1]
    for part in (
        "-o BAM",
        f"--ref-paths /data/{KEYS['paths']}",
        "-P -L 3000",
        f"-Z /data/{KEYS['gbz']}",
        "--kff-name /data/pangenome/kmc/S_bwa.kff",
        f"--haplotype-name /data/{KEYS['hapl']}",
        "--sample S_giraffe",
        "SM:S_giraffe",
        "-t 16",
        "-f /data/raw_reads/S_bwa_R1.fastq.gz -f /data/raw_reads/S_bwa_R2.fastq.gz",
    ):
        assert part in script
    # sampled graph files go to a per-sample folder, not next to the shared graph
    assert "--index-basename /data/pangenome/giraffe_S_giraffe/sampled" in script
    assert script.endswith("> /data/alignments/u.bam.part; mv /data/alignments/u.bam.part /data/alignments/u.bam")


def test_postprocess_renames_contigs_then_matches_the_align_stage():
    script = bg.postprocess_command("alignments/u.bam", "alignments/S.bam", 16)[-1]
    order = [
        "sed -e 's/GRCh38#0#//g'",
        "samtools fixmate -m -u - -",
        "samtools sort -u -@ 4 -m 512M",
        "samtools markdup -s -@ 16",
        "mv /data/alignments/S.bam.part /data/alignments/S.bam",
    ]
    positions = [script.index(part) for part in order]
    assert positions == sorted(positions)


def test_giraffe_mapping_seconds():
    assert bg.giraffe_mapping_seconds("Mapped 18997779 reads across 16 threads in 1234.5 seconds with ...") == 1234.5
    assert bg.giraffe_mapping_seconds("nothing") is None


def test_configs_compare_like_with_like():
    labels = [c[0] for c in bg.CONFIGS]
    assert labels == [
        "bwa-mem2 + linear DV",
        "bwa-mem2 + pangenome DV",
        "giraffe + linear DV",
        "giraffe + pangenome DV",
    ]
    # every pangenome run uses the settings of the earlier benchmarks; MAPQ>=5 runs add only the read filter
    for _, _, _, caller, env, memory in bg.CONFIGS + bg.MAPQ5_CONFIGS:
        if caller == "pangenome":
            assert env["DEEPVARIANT_SHARDS"] == "6" and "batch_size=256" in env["DEEPVARIANT_EXTRA_ARGS"]
            assert memory == "14g"
    for _, _, suffix, _, env, _ in bg.MAPQ5_CONFIGS:
        assert suffix.endswith("_mq5") and env["DEEPVARIANT_EXTRA_ARGS"].endswith("min_mapping_quality=5")
    assert set(bg.PANGENOME_ENV) <= set(run_local.DEEPVARIANT_ENV_VARS)
    giraffe_linear = bg.CONFIGS[2][4]["DEEPVARIANT_EXTRA_ARGS"]
    assert "min_mapping_quality=0,keep_legacy_allele_counter_behavior=true,normalize_reads=true" in giraffe_linear


def test_run_caller_sets_and_restores_deepvariant_env(monkeypatch):
    seen = {}
    monkeypatch.setenv("DEEPVARIANT_SHARDS", "16")
    monkeypatch.delenv("DEEPVARIANT_EXTRA_ARGS", raising=False)

    def fake_main(args):
        seen["args"] = args
        seen["env"] = {k: os.environ.get(k) for k in run_local.DEEPVARIANT_ENV_VARS}
        return 0

    monkeypatch.setattr(run_local, "main", fake_main)
    assert bg.run_caller("/r", "S_giraffe", "ref.fa", "pangenome", bg.PANGENOME_ENV, "14g") == 0
    assert seen["env"]["DEEPVARIANT_SHARDS"] == "6" and "batch_size=256" in seen["env"]["DEEPVARIANT_EXTRA_ARGS"]
    assert seen["args"][seen["args"].index("--caller") :] == [
        "--caller",
        "pangenome",
        "--skip-to-variants",
        "--dv-memory",
        "14g",
    ]
    assert seen["args"][:6] == ["--root", "/r", "--sample", "S_giraffe", "--reference", "ref.fa"]
    assert os.environ["DEEPVARIANT_SHARDS"] == "16" and "DEEPVARIANT_EXTRA_ARGS" not in os.environ


def test_result_row_and_summary():
    summary = {"SNP": {"fn": 72, "fp": 106, "f1": 0.998732}, "INDEL": {"fn": 34, "fp": 24, "f1": 0.997317}}
    extended = {("segdups", "SNP"): {"fn": 5, "fp": 6}, ("lowmappabilityall", "SNP"): {"fn": 50, "fp": 10}}
    row = bg.result_row(
        "giraffe + pangenome DV",
        "giraffe",
        "S_giraffe",
        "pangenome",
        summary,
        extended,
        {"align_wall_s": 900.0, "align_peak_mib": 6000},
    )
    assert row["snp_fn"] == 72 and row["indel_fp"] == 24 and row["segdups_snp_errors"] == 11
    assert row["lowmappabilityall_snp_errors"] == 60 and row["segdups_indel_errors"] == ""
    text = bg.summary_markdown("S", [row], {"giraffe": {"wall_s": 800.4, "peak_anon_bytes": 2 * 1024**3}})
    assert "| giraffe + pangenome DV | 72 / 106 | 0.998732 | 34 / 24 | 0.997317 | 11 | 60 |" in text
    assert "| giraffe | 800 | 2048 |" in text


def test_has_subsets(tmp_path):
    prefix = str(tmp_path / "pangenome")
    with open(prefix + ".extended.csv", "w", encoding="utf-8") as fh:
        fh.write("Type,Subtype,Subset,Filter\nSNP,*,*,PASS\nSNP,*,segdups,PASS\n")
    import benchmark_happy

    assert benchmark_happy.has_subsets(prefix + ".extended.csv", ["*", "segdups"])
    assert not benchmark_happy.has_subsets(prefix + ".extended.csv", ["*", "segdups", "lowmappabilityall"])


def test_needs_the_bwa_mem2_benchmark_first(tmp_path):
    with pytest.raises(SystemExit):
        bg.main(["--root", str(tmp_path), "--base", "S", "--reference", "r.fa"])


def test_caller_settings_override_only_pangenome_runs():
    env, memory = bg.caller_settings("pangenome", bg.PANGENOME_ENV, "14g", 12, "18g")
    assert env["DEEPVARIANT_SHARDS"] == "12" and memory == "18g"
    assert env["DEEPVARIANT_EXTRA_ARGS"] == bg.PANGENOME_ENV["DEEPVARIANT_EXTRA_ARGS"]
    assert bg.PANGENOME_ENV["DEEPVARIANT_SHARDS"] == "6"  # the shared default is not modified
    linear_env = {"DEEPVARIANT_EXTRA_ARGS": bg.VG_LINEAR_DV_ARGS}
    assert bg.caller_settings("deepvariant", linear_env, "12g", 12, "18g") == (linear_env, "12g")


def test_giraffe_cost_separates_per_sample_and_one_off_steps():
    gib = 1024**3
    costs = {
        "giraffe": {"wall_s": 545.7, "peak_anon_bytes": int(1.5 * gib), "mapping_s": 461.6},
        "postprocess": {"wall_s": 157.7, "peak_anon_bytes": int(2.3 * gib)},
        "kmc": {"wall_s": 37.2, "peak_anon_bytes": 7 * gib},
        "index_dist": {"wall_s": 39.0},
        "index_ri": {"wall_s": 6.0},
        "index_hapl": {"wall_s": 297.0},
        "index_paths": {"wall_s": 3.0},
    }
    c = bg.giraffe_cost(costs)
    assert c["align_wall_s"] == 703.4 and c["align_peak_mib"] == round(2.3 * 1024)
    assert c["giraffe_mapping_s"] == 461.6 and c["kmc_wall_s"] == 37.2 and c["kmc_peak_mib"] == 7 * 1024
    assert c["graph_index_wall_s"] == 345.0
    assert bg.giraffe_cost({})["align_peak_mib"] == ""


def test_bwa_mem2_cost():
    assert bg.bwa_mem2_cost({"wall_s": 521.04, "peak_anon_bytes": 7593 * 1024**2}) == {
        "align_wall_s": 521.0,
        "align_peak_mib": 7593,
    }
    assert bg.bwa_mem2_cost({}) == {"align_wall_s": "", "align_peak_mib": ""}


def test_sort_settings_match_the_align_stage():
    import tasks

    assert (bg.SORT_THREADS, bg.SORT_MEMORY) == (tasks.SORT_MAX_THREADS, tasks.SORT_MEMORY_PER_THREAD)
    script = bg.postprocess_command("alignments/u.bam", "alignments/S.bam", 16)[-1]
    assert f"samtools sort -u -@ {bg.SORT_THREADS} -m {bg.SORT_MEMORY}" in script


def test_alignment_path_prefers_cram_like_the_callers(tmp_path):
    import benchmark_aligners as ba
    import tasks

    assert ba.ALIGNMENT_FORMS == tasks.ALIGNMENT_FORMS
    (tmp_path / "alignments").mkdir()
    assert ba.alignment_path(str(tmp_path), "S") is None
    (tmp_path / "alignments" / "S.bam").write_bytes(b"b")
    assert ba.alignment_path(str(tmp_path), "S").endswith("S.bam")
    (tmp_path / "alignments" / "S.cram").write_bytes(b"c")
    assert ba.alignment_path(str(tmp_path), "S").endswith("S.cram")


def test_link_or_copy_refreshes_a_stale_link(tmp_path):
    src, dest = tmp_path / "a.bam", tmp_path / "a_mq5.bam"
    src.write_bytes(b"old")
    bg.link_or_copy(str(src), str(dest))
    assert dest.read_bytes() == b"old"
    bg.link_or_copy(str(src), str(dest))  # unchanged: kept
    src.unlink()
    src.write_bytes(b"new alignment")  # regenerated: a new file, as mv of a .part does
    bg.link_or_copy(str(src), str(dest))
    assert dest.read_bytes() == b"new alignment"


def test_remove_downstream_clears_calls_and_scores(tmp_path):
    root = str(tmp_path)
    for caller in ("deepvariant", "pangenome"):
        path = run_local.vcf_path(root, caller, "S_giraffe")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "wb").close()
    (tmp_path / "benchmarks" / "S_giraffe").mkdir(parents=True)
    bg.remove_downstream(root, "S_giraffe")
    assert not os.path.exists(run_local.vcf_path(root, "pangenome", "S_giraffe"))
    assert not (tmp_path / "benchmarks" / "S_giraffe").exists()
    bg.remove_downstream(root, "S_giraffe")  # nothing left: no error


def test_run_step_reruns_when_its_output_is_gone(tmp_path, monkeypatch):
    import benchmark_aligners as ba

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    ba.save_json(str(out_dir / "kmc.json"), {"wall_s": 37.0})
    calls = []
    monkeypatch.setattr(ba, "run_measured", lambda *args, **kw: calls.append(args) or (0, 40.0, {}))
    (out_dir / "kmc.log").write_text("")
    results = {}
    (tmp_path / "pangenome").mkdir()
    (tmp_path / "pangenome" / "S.kff").write_bytes(b"k")
    assert bg.run_step("kmc", ["x"], str(tmp_path), "img", str(out_dir), results, outputs=["pangenome/S.kff"])
    assert calls == [] and results["kmc"]["wall_s"] == 37.0  # saved and output present: reused
    (tmp_path / "pangenome" / "S.kff").unlink()
    assert bg.run_step("kmc", ["x"], str(tmp_path), "img", str(out_dir), results, outputs=["pangenome/S.kff"])
    assert len(calls) == 1 and results["kmc"]["wall_s"] == 40.0  # output gone: rerun
    assert bg.run_step("kmc", ["x"], str(tmp_path), "img", str(out_dir), results, force=True)
    assert len(calls) == 2


def test_graph_index_cost_is_blank_when_not_measured():
    assert bg.graph_index_seconds({}) == ""
    assert bg.graph_index_seconds({"index_dist": {"wall_s": 39.0}, "index_hapl": {"wall_s": 297.04}}) == 336.0


def test_caller_settings_keep_the_configuration_memory_without_override():
    assert bg.caller_settings("pangenome", bg.PANGENOME_ENV, "14g", 12, None)[1] == "14g"


def test_call_settings_ignore_shard_count():
    a = bg.call_settings("pangenome", {**bg.PANGENOME_ENV, "DEEPVARIANT_SHARDS": "6"})
    b = bg.call_settings("pangenome", {**bg.PANGENOME_ENV, "DEEPVARIANT_SHARDS": "12"})
    assert a == b and a["caller"] == "pangenome" and "DEEPVARIANT_SHARDS" not in a
    assert a != bg.call_settings("pangenome", {"DEEPVARIANT_EXTRA_ARGS": "x"})


def test_remove_caller_outputs_keeps_other_callers(tmp_path):
    root = str(tmp_path)
    for caller in ("deepvariant", "pangenome"):
        vcf = run_local.vcf_path(root, caller, "S")
        os.makedirs(os.path.dirname(vcf), exist_ok=True)
        open(vcf, "wb").close()
    happy = tmp_path / "benchmarks" / "S"
    happy.mkdir(parents=True)
    for name in ("pangenome.summary.csv", "pangenome.extended.csv", "deepvariant.summary.csv"):
        (happy / name).write_text("x")
    bg.remove_caller_outputs(root, "S", "pangenome")
    assert not os.path.exists(run_local.vcf_path(root, "pangenome", "S"))
    assert os.path.exists(run_local.vcf_path(root, "deepvariant", "S"))
    assert sorted(os.listdir(happy)) == ["deepvariant.summary.csv"]


SUMMARY = "\n".join(
    [
        "Type,Filter,TRUTH.TOTAL,TRUTH.TP,TRUTH.FN,QUERY.FP,METRIC.Recall,METRIC.Precision,METRIC.F1_Score",
        "SNP,PASS,10,9,1,2,0.9,0.8,0.85",
        "INDEL,PASS,5,5,0,1,1.0,0.8,0.9",
        "",
    ]
)


@pytest.fixture
def scoring(tmp_path, monkeypatch):
    """A root with an alignment and truth, and fake run_caller / hap.py that count their runs."""
    import benchmark_happy

    root = tmp_path
    (root / "alignments").mkdir()
    (root / "alignments" / "S.bam").write_bytes(b"aln")
    (root / "truth").mkdir()
    runs = {"calls": 0, "happy": 0}

    def fake_caller(r, sid, reference, caller, env, memory, pangenome=None):
        runs["calls"] += 1
        vcf = run_local.vcf_path(r, caller, sid)
        os.makedirs(os.path.dirname(vcf), exist_ok=True)
        with open(vcf, "wb") as fh:
            fh.write(b"vcf %d" % runs["calls"])
        return 0

    def fake_happy(args):
        runs["happy"] += 1
        out = root / "benchmarks" / "S"
        out.mkdir(parents=True, exist_ok=True)
        (out / "pangenome.summary.csv").write_text(SUMMARY)
        (out / "pangenome.extended.csv").write_text(
            "\n".join(
                [
                    "Type,Subtype,Subset,Filter,METRIC.F1_Score,TRUTH.TOTAL,TRUTH.FN,QUERY.FP",
                    "SNP,*,*,PASS,0.85,10,1,2",
                    "",
                ]
            )
        )
        return 0

    monkeypatch.setattr(bg, "run_caller", fake_caller)
    monkeypatch.setattr(benchmark_happy, "main", fake_happy)
    out_dir = root / "out"
    out_dir.mkdir()

    def score(env=bg.PANGENOME_ENV):
        return bg.score_config(
            str(root), str(out_dir), str(out_dir / "calls.json"), "L", "S", "pangenome", env, "14g", "r.fa", "S"
        )

    return root, runs, score


def test_score_config_calls_once_then_reuses(scoring):
    root, runs, score = scoring
    summary, _, info = score()
    assert runs == {"calls": 1, "happy": 1} and summary["SNP"]["fn"] == 1
    assert info["calls_provenance"] == "recorded" and info["calls_shards"] == "6"
    score()
    assert runs == {"calls": 1, "happy": 1}  # nothing changed: both reused


def test_score_config_redoes_calls_when_settings_or_alignment_change(scoring):
    root, runs, score = scoring
    score()
    score({**bg.PANGENOME_ENV, "DEEPVARIANT_SHARDS": "12"})  # speed only: reused
    assert runs == {"calls": 1, "happy": 1}
    score({**bg.PANGENOME_ENV, "DEEPVARIANT_EXTRA_ARGS": "--x"})  # other flags: called again, then re-scored
    assert runs == {"calls": 2, "happy": 2}
    (root / "alignments" / "S.bam").write_bytes(b"new alignment")
    score({**bg.PANGENOME_ENV, "DEEPVARIANT_EXTRA_ARGS": "--x"})
    assert runs == {"calls": 3, "happy": 3}


def test_score_config_reuses_an_unrecorded_vcf_and_says_so(scoring):
    root, runs, score = scoring
    vcf = run_local.vcf_path(str(root), "pangenome", "S")
    os.makedirs(os.path.dirname(vcf), exist_ok=True)
    open(vcf, "wb").close()
    _, _, info = score()
    assert runs == {"calls": 0, "happy": 1} and info["calls_provenance"] == "unrecorded"


def test_call_settings_record_a_chosen_graph_only():
    assert "graph" not in bg.call_settings("pangenome", bg.PANGENOME_ENV)
    with_graph = bg.call_settings("pangenome", bg.PANGENOME_ENV, "r2.gbz")
    assert with_graph["graph"] == "r2.gbz" and with_graph != bg.call_settings("pangenome", bg.PANGENOME_ENV)


def test_run_caller_passes_a_chosen_graph(monkeypatch):
    seen = {}
    monkeypatch.setattr(run_local, "main", lambda args: seen.setdefault("args", args) and 0)
    bg.run_caller("/r", "S", "ref.fa", "pangenome", {}, "14g", "hprc-v2.0-mc-grch38-eval.chr20.gbz")
    assert seen["args"][-2:] == ["--pangenome", "hprc-v2.0-mc-grch38-eval.chr20.gbz"]
    seen.clear()
    bg.run_caller("/r", "S", "ref.fa", "pangenome", {}, "14g")
    assert "--pangenome" not in seen["args"]
