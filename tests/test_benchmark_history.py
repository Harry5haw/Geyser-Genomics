"""Benchmark history (scripts/benchmark_history.py, #58): hap.py output -> one tracked series -> report.
hap.py itself is faked: these tests write its output files."""

import csv
import json
import os
import subprocess

import benchmark_happy
import benchmark_history as bh
import run_local

EXT_HEADER = (
    "Type,Subtype,Subset,Filter,Genotype,QQ.Field,QQ,METRIC.Recall,METRIC.Precision,METRIC.Frac_NA,"
    "METRIC.F1_Score,TRUTH.TOTAL,TRUTH.TP,TRUTH.FN,QUERY.FP"
)
SUM_HEADER = (
    "Type,Filter,TRUTH.TOTAL,TRUTH.TP,TRUTH.FN,QUERY.TOTAL,QUERY.FP,METRIC.Recall,METRIC.Precision,METRIC.F1_Score"
)


def ext_row(t, subset, fn, fp, f1, filt="PASS", subtype="*", genotype="*"):
    return f"{t},{subtype},{subset},{filt},{genotype},,,0.99,0.99,0,{f1},1000,{1000 - fn},{fn},{fp}"


def write_happy(prefix, rows=None, timestamp="Mon Nov 02 19:00:34 2026", region="chr20", extended=True, snp_fn=10):
    """hap.py's files for one caller: extended.csv (unless extended=False), summary.csv and runinfo.json."""
    os.makedirs(os.path.dirname(prefix), exist_ok=True)
    rows = rows or [
        ext_row("SNP", "*", snp_fn, 5, 0.9925),
        ext_row("INDEL", "*", 3, 2, 0.9975),
        ext_row("SNP", "segdups", 4, 1, 0.97),
        ext_row("SNP", "*", 99, 99, 0.5, filt="ALL"),  # not PASS
        ext_row("SNP", "*", 99, 99, 0.5, subtype="ti"),  # a subtype
        ext_row("SNP", "*", 99, 99, 0.5, genotype="het"),  # a genotype split
    ]
    if extended:
        with open(prefix + ".extended.csv", "w") as fh:
            fh.write(EXT_HEADER + "\n" + "\n".join(rows) + "\n")
    with open(prefix + ".summary.csv", "w") as fh:
        fh.write(SUM_HEADER + "\n")
        fh.write(f"SNP,PASS,1000,{1000 - snp_fn},{snp_fn},900,5,0.99,0.99,0.9925\n")
        fh.write("INDEL,PASS,100,97,3,90,2,0.97,0.98,0.9975\nSNP,ALL,1,1,1,1,1,1,1,0.1\n")
    with open(prefix + ".runinfo.json", "w") as fh:
        json.dump({"timestamp": timestamp, "final_args": {"locations": region}}, fh)


def test_read_happy_keeps_pass_rows_over_all_subtypes_and_genotypes(tmp_path):
    prefix = str(tmp_path / "b" / "deepvariant")
    write_happy(prefix)
    rows = {(r["subset"], r["type"]): r for r in bh.read_happy(prefix)}
    assert set(rows) == {("*", "SNP"), ("*", "INDEL"), ("segdups", "SNP")}
    assert rows[("*", "SNP")]["fn"] == 10 and rows[("*", "SNP")]["fp"] == 5 and rows[("*", "SNP")]["f1"] == 0.9925


def test_read_happy_falls_back_to_the_summary(tmp_path):
    prefix = str(tmp_path / "b" / "bcftools")
    write_happy(prefix, extended=False)
    rows = bh.read_happy(prefix)
    assert {(r["subset"], r["type"]) for r in rows} == {("*", "SNP"), ("*", "INDEL")}


def test_run_info_date_and_region(tmp_path):
    prefix = str(tmp_path / "b" / "x")
    write_happy(prefix, timestamp="Sun Nov 01 22:50:08 2026")
    assert bh.happy_run_info(prefix) == {"date": "2026-11-01", "region": "chr20"}
    os.remove(prefix + ".runinfo.json")
    assert bh.happy_run_info(prefix)["date"]  # file mtime


def test_settings_tag_names_only_what_changes_results():
    assert bh.settings_tag("--call_variants_extra_args=batch_size=256") == ""
    assert (
        bh.settings_tag("--call_variants_extra_args=batch_size=256 --make_examples_extra_args=min_mapping_quality=5")
        == "MAPQ>=5"
    )
    vg = (
        "--make_examples_extra_args=min_mapping_quality=0,keep_legacy_allele_counter_behavior=true,normalize_reads=true"
    )
    assert bh.settings_tag(vg) == "VG flags"


def test_metadata_from_known_runs_then_given_values():
    meta = bh.run_metadata("HG003_chr20_vg", "deepvariant")
    assert meta["aligner"] == "vg giraffe" and meta["caller_settings"] == "VG flags" and meta["source"] == "#145"
    assert bh.run_metadata("HG005_chr20_mq5", "pangenome")["caller_settings"] == "MAPQ>=5"
    assert bh.run_metadata("HG005_chr20", "pangenome")["graph"] == bh.PANGENOME_GRAPH
    new = bh.run_metadata("NA12878_chr20", "bcftools", aligner="bwa-mem2", commit="abc1234")
    assert (new["sample"], new["aligner"], new["commit"], new["graph"]) == ("NA12878", "bwa-mem2", "abc1234", "")


def test_record_replaces_a_same_day_rescore_and_keeps_other_days(tmp_path):
    history = str(tmp_path / "history.csv")
    prefix = str(tmp_path / "b" / "deepvariant")
    meta = bh.run_metadata("S1", "deepvariant", aligner="bwa-mem2")
    write_happy(prefix, snp_fn=10)
    assert bh.record(prefix, meta, history) == 3
    write_happy(prefix, snp_fn=8)  # same day: replaces
    bh.record(prefix, meta, history)
    rows = bh.read_history(history)
    assert len(rows) == 3 and {r["fn"] for r in rows if r["type"] == "SNP" and r["subset"] == "*"} == {"8"}
    write_happy(prefix, snp_fn=6, timestamp="Tue Nov 03 09:00:00 2026")  # a later day: adds
    bh.record(prefix, meta, history)
    rows = bh.read_history(history)
    assert len(rows) == 6 and sorted({r["date"] for r in rows}) == ["2026-11-02", "2026-11-03"]
    with open(history, newline="") as fh:
        assert next(csv.reader(fh)) == bh.FIELDS
    # Report: the latest scoring, and the re-scored configuration listed by date.
    text = bh.report_markdown(rows)
    assert "| bwa-mem2 + linear DV | 0.992500 | 6 / 5 |" in text
    assert "## Re-scored configurations" in text and "| S1 | 2026-11-02 |" in text and "| S1 | 2026-11-03 |" in text


def test_backfill_records_known_folders_only(tmp_path):
    root = tmp_path / "data"
    write_happy(str(root / "benchmarks" / "HG003_chr20_giraffe" / "pangenome"))
    write_happy(str(root / "benchmarks" / "SOMETHING_ELSE" / "deepvariant"))
    history = str(tmp_path / "history.csv")
    assert bh.backfill(str(root), history) == 3
    rows = bh.read_history(history)
    assert {r["run"] for r in rows} == {"HG003_chr20_giraffe"}
    assert {r["aligner"] for r in rows} == {"vg giraffe"} and {r["source"] for r in rows} == {"#129"}
    assert bh.backfill(str(root), history) == 3 and len(bh.read_history(history)) == 3  # idempotent


def test_report_and_figure_csv(tmp_path):
    history = str(tmp_path / "history.csv")
    for caller, fn in (("deepvariant", 10), ("pangenome", 7)):
        prefix = str(tmp_path / "b" / caller)
        write_happy(prefix, snp_fn=fn)
        bh.record(prefix, bh.run_metadata("HG003_chr20", caller), history)
    rows = bh.read_history(history)
    md, fig = str(tmp_path / "README.md"), str(tmp_path / "fig.csv")
    bh.write_report(rows, md, fig)
    with open(fig, newline="") as fh:
        figure = list(csv.DictReader(fh))
    assert list(figure[0]) == bh.FIGURE_FIELDS
    by_caller = {r["caller"]: r for r in figure}
    assert by_caller["pangenome"]["errors"] == str(7 + 5 + 3 + 2)
    assert (
        by_caller["pangenome"]["snp_errors_segdups"] == "5"
        and by_caller["pangenome"]["snp_errors_lowmappabilityall"] == ""
    )
    text = open(md, encoding="utf-8").read()
    assert "## HG003 35x: Google case-study BAM" in text and "| bwa + pangenome DV |" in text


def test_benchmark_happy_appends_each_scored_caller(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "reference").mkdir(parents=True)
    (root / "reference" / "r.fa.fai").write_text("c\t10\t3\t10\t11\n")
    (root / "variants_deepvariant").mkdir()
    (root / "variants_deepvariant" / "S1.vcf.gz").write_bytes(b"x")

    def fake_happy(cmd, *a, **k):  # writes what hap.py would, for the caller in the command's output prefix
        out = next(c for c in cmd if c.startswith("/data/benchmarks/"))
        write_happy(str(root / out[len("/data/") :]))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(benchmark_happy.subprocess, "run", fake_happy)
    monkeypatch.setenv("DEEPVARIANT_EXTRA_ARGS", "--make_examples_extra_args=min_mapping_quality=5")
    monkeypatch.setattr(bh, "git_commit", lambda repo=None: "abc1234")
    args = [
        "--root",
        str(root),
        "--sample",
        "S1",
        "--reference",
        "r.fa",
        "--callers",
        "deepvariant",
        "--aligner",
        "bwa-mem2",
    ]
    assert benchmark_happy.main(args) == 0
    rows = bh.read_history(bh.HISTORY)  # the test's own history (conftest), never docs/benchmarks/
    assert {(r["run"], r["caller"], r["aligner"], r["caller_settings"], r["commit"]) for r in rows} == {
        ("S1", "deepvariant", "bwa-mem2", "MAPQ>=5", "abc1234")
    }
    assert benchmark_happy.main(args + ["--no-history"]) == 0 and len(bh.read_history(bh.HISTORY)) == len(rows)


def test_benchmark_happy_records_the_graph_for_pangenome_rows_only(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "reference").mkdir(parents=True)
    (root / "reference" / "r.fa.fai").write_text("c\t10\t3\t10\t11\n")
    for caller in ("deepvariant", "pangenome"):
        vcf = run_local.vcf_path(str(root), caller, "S1")
        os.makedirs(os.path.dirname(vcf), exist_ok=True)
        open(vcf, "wb").close()

    def fake_happy(cmd, *a, **k):
        out = next(c for c in cmd if c.startswith("/data/benchmarks/"))
        write_happy(str(root / out[len("/data/") :]))
        return subprocess.CompletedProcess(cmd, 0)

    commits = []
    monkeypatch.setattr(benchmark_happy.subprocess, "run", fake_happy)
    monkeypatch.setattr(bh, "git_commit", lambda repo=None: commits.append(1) or "abc1234")
    r2 = "hprc-v2.0-mc-grch38-eval.chr20.gbz"
    args = ["--root", str(root), "--sample", "S1", "--reference", "r.fa", "--graph", r2]
    assert benchmark_happy.main(args + ["--callers", "deepvariant", "pangenome"]) == 0
    graphs = {(r["caller"], r["graph"], r["commit"]) for r in bh.read_history(bh.HISTORY)}
    assert graphs == {("deepvariant", "", "abc1234"), ("pangenome", bh.GRAPH_LABELS[r2], "abc1234")}
    assert commits == [1]  # once per run, before the first record() rewrites the history


def test_git_commit_ignores_the_history_itself(tmp_path):
    repo = tmp_path / "repo"
    (repo / "docs" / "benchmarks").mkdir(parents=True)
    (repo / "docs" / "benchmarks" / "history.csv").write_text("a\n")
    (repo / "code.py").write_text("x = 1\n")
    git = ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t"]
    for cmd in (["init", "-q"], ["add", "."], ["commit", "-qm", "init"]):
        subprocess.run(git + cmd, check=True, capture_output=True)
    clean = bh.git_commit(str(repo))
    assert clean and not clean.endswith("-dirty")
    (repo / "docs" / "benchmarks" / "history.csv").write_text("a\nb\n")  # what record() does
    assert bh.git_commit(str(repo)) == clean
    (repo / "code.py").write_text("x = 2\n")
    assert bh.git_commit(str(repo)) == clean + "-dirty"


def test_a_rescore_with_other_settings_supersedes_and_a_sub_region_stays_separate(tmp_path):
    history = str(tmp_path / "history.csv")
    prefix = str(tmp_path / "b" / "deepvariant")
    write_happy(prefix, snp_fn=10)
    bh.record(prefix, bh.run_metadata("S1", "deepvariant", aligner="bwa"), history)
    write_happy(prefix, snp_fn=7, timestamp="Tue Nov 03 09:00:00 2026")  # re-scored with VG flags
    bh.record(prefix, bh.run_metadata("S1", "deepvariant", aligner="bwa", caller_settings="VG flags"), history)
    write_happy(prefix, snp_fn=1, timestamp="Wed Nov 04 09:00:00 2026", region="chr20:1-1000000")
    bh.record(prefix, bh.run_metadata("S1", "deepvariant", aligner="bwa"), history)
    configs = bh.configurations(bh.read_history(history))
    # One configuration per region: the whole region's latest scoring (the VG-flags re-score superseded the first),
    # and the sub-region's own, named, instead of the sub-region becoming the headline.
    assert sorted((c["configuration"], c["snp_fn"]) for c in configs) == [
        ("bwa + linear DV (chr20:1-1000000)", "1"),
        ("bwa + linear DV, VG flags", "7"),
    ]


def test_metadata_explicit_empty_clears_a_known_value_and_graphs_get_labels():
    assert bh.run_metadata("HG003_chr20_vg", "deepvariant")["caller_settings"] == bh.VG_FLAGS
    assert bh.run_metadata("HG003_chr20_vg", "deepvariant", caller_settings="")["caller_settings"] == ""
    assert bh.run_metadata("HG003_chr20_vg", "deepvariant", caller_settings=None)["caller_settings"] == bh.VG_FLAGS
    assert bh.run_metadata("X", "pangenome")["graph"] == bh.PANGENOME_GRAPH
    assert bh.run_metadata("X", "pangenome", graph="hprc-v2.0-mc-grch38-eval.chr20.gbz")["graph"].startswith(
        "HPRC v2.0"
    )
    assert bh.run_metadata("HG003_30x_chr20_mq5", "deepvariant")["source"] == "#51"


def test_history_functions_default_to_the_current_history_path(tmp_path, monkeypatch):
    monkeypatch.setattr(bh, "HISTORY", str(tmp_path / "other.csv"))
    prefix = str(tmp_path / "b" / "deepvariant")
    write_happy(prefix)
    assert bh.record(prefix, bh.run_metadata("S1", "deepvariant")) == 3  # no path: HISTORY now, not at import
    assert os.path.exists(tmp_path / "other.csv") and len(bh.read_history()) == 3


def test_tests_never_touch_the_tracked_history():
    assert os.path.dirname(bh.HISTORY) != os.path.join(bh.REPO_ROOT, "docs", "benchmarks")
