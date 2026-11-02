"""Scatter-gather variant calling (#108): regions, shards, the merge, the CLI and the local runner.

The tools are faked (see test_stage_io.py's fixtures); tests/test_e2e_docker.py runs the real ones
and checks the merged calls equal the unsharded calls.
"""

import json
import shlex

import pytest

import run_local
import tasks
from test_stage_io import GAPPED_REF, REF, put  # tool_calls, shell_commands, uploads: conftest.py

# The real samtools faidx index of GAPPED_REF (one contig "c": a 1,000-N line, then ACGT). The faked
# faidx in test_stage_io writes an empty .fai, which reference_regions rightly refuses.
GAPPED_FAI = b"c\t1004\t3\t1000\t1001\n"


def put_reference(local_env):
    put(local_env, f"reference/{REF}", GAPPED_REF)
    put(local_env, f"reference/{REF}.fai", GAPPED_FAI)


# --- regions -----------------------------------------------------------------------


def test_a_contig_shorter_than_the_window_is_one_region():
    assert tasks.regions_from_lengths([("chr20", 64_444_167)], 100_000_000) == ["chr20:1-64444167"]


def test_large_contigs_are_cut_into_windows_with_a_remainder():
    assert tasks.regions_from_lengths([("c", 25)], 10) == ["c:1-10", "c:11-20", "c:21-25"]
    assert tasks.regions_from_lengths([("c", 20)], 10) == ["c:1-10", "c:11-20"]  # exact multiple: no empty tail


def test_regions_keep_reference_order_across_contigs():
    assert tasks.regions_from_lengths([("chr2", 5), ("chr1", 12)], 10) == ["chr2:1-5", "chr1:1-10", "chr1:11-12"]


def test_contig_names_with_colons_are_braced():
    assert tasks.regions_from_lengths([("HLA-A*01:01", 3)], 10) == ["{HLA-A*01:01}:1-3"]


def test_window_must_be_positive():
    with pytest.raises(ValueError):
        tasks.regions_from_lengths([("c", 5)], 0)


def test_fai_and_fasta_give_the_same_lengths(tmp_path):
    fasta = tmp_path / "r.fa"
    fasta.write_text(">a desc\nACGT\nAC\n>b\nNNNNNNN\n")
    assert tasks.contig_lengths_from_fasta(str(fasta)) == [("a", 6), ("b", 7)]
    assert tasks.contig_lengths_from_fai("a\t6\t8\t4\t5\nb\t7\t24\t7\t8\n") == [("a", 6), ("b", 7)]
    # reference_regions prefers the .fai when there is one
    (tmp_path / "r.fa.fai").write_text("a\t6\t8\t4\t5\nb\t7\t24\t7\t8\n")
    assert tasks.reference_regions(str(fasta), 5) == ["a:1-5", "a:6-6", "b:1-5", "b:6-7"]


def test_part_keys_are_safe_and_distinct():
    a = tasks.region_part_key("S1", "{HLA-A*01:01}:1-3")
    b = tasks.region_part_key("S1", "{HLA-A*01_01}:1-3")  # same after sanitising, different region
    assert a.startswith("variants/S1/parts/") and a.endswith(".vcf.gz") and a != b
    assert all(c.isalnum() or c in "._-/" for c in a)
    assert tasks.region_part_key("S1", "c:1-10") == tasks.region_part_key("S1", "c:1-10")  # deterministic


# --- shard and merge ------------------------------------------------------------------


def test_a_shard_calls_one_region_and_uploads_its_part(local_env, tool_calls, shell_commands, uploads):
    put(local_env, "alignments/S1.cram", b"CRAM")
    put(local_env, f"reference/{REF}", GAPPED_REF)

    tasks.variants_shard_task("S1", REF, "c:1-500")

    index, call = shell_commands
    assert shlex.split(index)[:2] == ["samtools", "index"]  # -r needs random access
    words = shlex.split(call)
    assert words[words.index("-r") + 1] == "c:1-500"
    assert uploads == [tasks.region_part_key("S1", "c:1-500")]
    metrics = (local_env / "metrics" / "metrics.jsonl").read_text()
    assert json.loads(metrics.splitlines()[-1])["task"] == tasks.SHARD_METRIC
    assert not (local_env / "runs").exists()  # shards write no run-record part; the merge records them


def test_the_merge_concatenates_every_part_in_reference_order(local_env, tool_calls, shell_commands, uploads):
    put_reference(local_env)  # one contig "c" of 1,004 bases
    regions = tasks.regions_from_lengths([("c", 1004)], 300)
    for region in reversed(regions):  # upload order must not matter
        put(local_env, tasks.region_part_key("S1", region), region.encode())

    tasks.variants_task("S1", REF, window=300)

    (concat, index, stats) = shell_commands
    words = shlex.split(concat)
    assert words[:2] == ["bcftools", "concat"]
    assert uploads == ["stats/S1/bcftools.stats.txt", "variants/S1.vcf.gz.tbi", "variants/S1.vcf.gz"]
    record = json.loads((local_env / "runs" / "S1.provenance.json").read_text())
    assert [i["key"] for i in record["inputs"]] == sorted(tasks.region_part_key("S1", r) for r in regions)
    part = json.loads((local_env / "runs" / "S1.provenance" / "CallVariants.json").read_text())
    assert part["regions"] == len(regions) == 4 and part["region_window"] == 300


def test_the_merge_lists_parts_in_region_order(local_env, tool_calls, monkeypatch):
    put_reference(local_env)
    regions = tasks.regions_from_lengths([("c", 1004)], 300)
    for region in regions:
        put(local_env, tasks.region_part_key("S1", region), region.encode())
    seen = []

    def run_shell(command):
        words = shlex.split(command)
        if words[:2] == ["bcftools", "concat"]:
            with open(words[words.index("-f") + 1]) as fh:
                seen.extend(open(path, "rb").read().decode() for path in fh.read().split())
        out = words[words.index("-o") + 1] if "-o" in words else words[-1]
        with open(out, "wb") as fh:
            fh.write(b"x")

    monkeypatch.setattr(tasks, "run_shell", run_shell)
    tasks.variants_task("S1", REF, window=300)
    assert seen == regions  # each part's content is its region: concatenated in reference order


def test_a_missing_part_fails_the_merge_by_name_before_any_upload(local_env, tool_calls, shell_commands, uploads):
    put_reference(local_env)
    regions = tasks.regions_from_lengths([("c", 1004)], 300)
    for region in regions[:-1]:
        put(local_env, tasks.region_part_key("S1", region), b"x")

    with pytest.raises(FileNotFoundError, match=r"1 of 4 region parts missing .*c:901-1004"):
        tasks.variants_task("S1", REF, window=300)
    assert uploads == [] and shell_commands == []


def test_without_a_window_variants_is_the_unsharded_call(local_env, tool_calls, shell_commands):
    put(local_env, "alignments/S1.cram", b"CRAM")
    put(local_env, f"reference/{REF}", GAPPED_REF)
    tasks.variants_task("S1", REF)
    assert shell_commands[0].startswith("bcftools mpileup") and " -r " not in shell_commands[0]


# --- CLI ---------------------------------------------------------------------------------


@pytest.fixture
def dispatched(monkeypatch):
    calls = []
    monkeypatch.setattr(tasks, "variants_shard_task", lambda *a: calls.append(("shard", *a)))
    monkeypatch.setitem(tasks.TASKS, "variants", (lambda *a, **k: calls.append(("variants", *a, k)), True))
    return calls


def test_cli_dispatches_shard_merge_and_plain_variants(dispatched):
    tasks.main(["variants", "S1", "r.fa", "--region", "c:1-10"])
    tasks.main(["variants", "S1", "r.fa", "--window", "500"])
    tasks.main(["variants", "S1", "r.fa"])
    assert dispatched == [
        ("shard", "S1", "r.fa", "c:1-10"),
        ("variants", "S1", "r.fa", {"window": 500}),
        ("variants", "S1", "r.fa", {}),
    ]


@pytest.mark.parametrize(
    "argv",
    [
        ["qc", "S1", "--region", "c:1-10"],
        ["align", "S1", "r.fa", "--window", "5"],
        ["variants", "S1", "r.fa", "--region", "c:1-2", "--window", "5"],
        ["variants", "S1", "r.fa", "--window", "0"],  # 0 is falsy: must not fall through to an unsharded call
        ["qc", "S1", "--window", "0"],
    ],
)
def test_cli_rejects_shard_options_where_they_do_not_apply(argv, dispatched):
    with pytest.raises(SystemExit):
        tasks.main(argv)
    assert dispatched == []


def test_cli_regions_prints_the_regions_as_json(local_env, tool_calls, capsys):
    put_reference(local_env)
    assert tasks.main(["regions", REF, "--window", "600"]) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1]) == ["c:1-600", "c:601-1004"]


# --- local runner ---------------------------------------------------------------------------


def test_run_local_shards_variants_then_merges():
    jobs = run_local.pipeline_jobs("S1", "r.fa", regions=["c:1-10", "c:11-20"], shard_window=10)
    assert [j["name"] for j in jobs] == ["qc", "align", "variants-shard", "variants-shard", "variants"]
    assert jobs[2]["cmd"][-2:] == ["--region", "c:1-10"] and jobs[-1]["cmd"][-2:] == ["--window", "10"]
    assert run_local.pipeline_jobs("S1", "r.fa")[-1]["cmd"] == ["python", "tasks.py", "variants", "S1", "r.fa"]


def test_run_local_plans_regions_from_the_local_reference(tmp_path):
    (tmp_path / "reference").mkdir()
    (tmp_path / "reference" / "r.fa").write_bytes(GAPPED_REF)
    assert run_local.local_regions(str(tmp_path), "r.fa", 500) == ["c:1-500", "c:501-1000", "c:1001-1004"]


def test_an_empty_index_is_an_error_not_an_empty_merge(tmp_path):
    ref = tmp_path / "r.fa"
    ref.write_bytes(GAPPED_REF)
    (tmp_path / "r.fa.fai").write_bytes(b"")
    with pytest.raises(ValueError, match="No regions"):
        tasks.reference_regions(str(ref), 100)


# --- review fixes -----------------------------------------------------------------------


def test_parts_are_kept_per_run(monkeypatch):
    monkeypatch.setenv("RUN_NAME", "S1-20261102T100000Z-abc")
    first = tasks.region_part_key("S1", "c:1-10")
    monkeypatch.setenv("RUN_NAME", "S1-20261102T110000Z-def")
    second = tasks.region_part_key("S1", "c:1-10")
    assert first.startswith("variants/S1/parts/S1-20261102T100000Z-abc/") and first != second
    monkeypatch.delenv("RUN_NAME")
    assert tasks.region_part_key("S1", "c:1-10").startswith("variants/S1/parts/unnamed/")


def test_the_merge_never_uses_another_runs_parts(local_env, tool_calls, shell_commands, uploads, monkeypatch):
    put_reference(local_env)
    regions = tasks.regions_from_lengths([("c", 1004)], 300)
    monkeypatch.setenv("RUN_NAME", "old-run")
    for region in regions:
        put(local_env, tasks.region_part_key("S1", region), b"stale")
    monkeypatch.setenv("RUN_NAME", "new-run")
    for region in regions[:2]:  # the new run's shards for two regions didn't happen
        put(local_env, tasks.region_part_key("S1", region), b"fresh")

    with pytest.raises(FileNotFoundError, match="2 of 4 region parts missing"):
        tasks.variants_task("S1", REF, window=300)
    assert uploads == []


def test_the_merge_names_the_alignment_its_shards_read(local_env, tool_calls, shell_commands):
    put_reference(local_env)
    put(local_env, "alignments/S1.cram", b"CRAM")
    for region in tasks.regions_from_lengths([("c", 1004)], 600):
        put(local_env, tasks.region_part_key("S1", region), b"x")
    tasks.variants_task("S1", REF, window=600)
    part = json.loads((local_env / "runs" / "S1.provenance" / "CallVariants.json").read_text())
    assert "alignments/S1.cram" in [i["key"] for i in part["inputs"]]


def test_a_malformed_fai_names_the_line():
    with pytest.raises(ValueError, match="Malformed .fai line 2"):
        tasks.contig_lengths_from_fai("a\t6\t8\t4\t5\nbroken\n")


def test_a_fasta_header_without_a_name_is_a_clear_error(tmp_path):
    fasta = tmp_path / "r.fa"
    fasta.write_text(">\nACGT\n")
    with pytest.raises(ValueError, match="header without a name"):
        tasks.contig_lengths_from_fasta(str(fasta))


def test_run_local_plans_regions_only_for_a_bcftools_stage(tmp_path, monkeypatch):
    planned = []
    monkeypatch.setattr(run_local, "local_regions", lambda *a: planned.append(a) or ["c:1-10"])
    monkeypatch.setattr(run_local, "run_job", lambda *a, **k: 0)
    monkeypatch.setattr(run_local, "sample_keys", lambda *a: ["raw_reads/S1.fastq.gz"])
    run_local.main(["--root", str(tmp_path), "--sample", "S1", "--caller", "deepvariant", "--shard-window", "10"])
    assert planned == []  # no bcftools stage: nothing to shard, the local reference isn't read
    run_local.main(["--root", str(tmp_path), "--sample", "S1", "--shard-window", "10"])
    assert len(planned) == 1


def test_cli_regions_stores_the_list_for_the_state_machine(local_env, tool_calls, capsys):
    # The Map's planner: a Batch job can't return output, so Read_Regions reads this key from S3.
    put_reference(local_env)
    key = "variants/S1/regions/S1-20261102T100000Z-abc.json"
    assert tasks.main(["regions", REF, "--window", "600", "--output-key", key]) == 0
    assert json.loads((local_env / key).read_text()) == ["c:1-600", "c:601-1004"]
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1]) == ["c:1-600", "c:601-1004"]


def test_cli_regions_rejects_a_zero_window(local_env, tool_calls):
    with pytest.raises(SystemExit):
        tasks.main(["regions", REF, "--window", "0"])
