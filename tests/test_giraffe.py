"""ALIGNER=giraffe (#146): settings, commands, the contig rename and the align stage's plumbing.

vg, kmc and samtools are faked, so these run anywhere; tests/test_e2e_giraffe.py runs the real tools in the
geyser-giraffe image on the synthetic graph.
"""

import json
import shlex
import subprocess

import pytest

import benchmark_giraffe
import run_local
import tasks

REF = "ref.fa"
GRAPH = "ref.gbz"
FAI = "chr20\t64444167\t7\t60\t61\n"
GIRAFFE_HEADER = "@HD\tVN:1.5\tSO:unknown\n@SQ\tSN:GRCh38#0#chr20\tLN:64444167\n@RG\tID:S1\tSM:S1\n@PG\tID:0\tPN:vg\n"


def put(root, key, data=b"x"):
    path = root.joinpath(*key.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def read_json(root, key):
    return json.loads(root.joinpath(*key.split("/")).read_text())


# --- settings ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reference, expected",
    [
        ("chr20.fa", "chr20.gbz"),
        ("grch38/chr20.fa", "chr20.gbz"),
        ("GRCh38_chr20.fa.gz", "GRCh38_chr20.gbz"),
        ("synthetic_ref.fa", "synthetic_ref.gbz"),
    ],
)
def test_the_default_graph_is_named_after_the_reference(monkeypatch, reference, expected):
    monkeypatch.delenv("GIRAFFE_GRAPH", raising=False)
    monkeypatch.delenv("GIRAFFE_SAMPLING", raising=False)
    assert tasks.giraffe_settings(reference) == {"graph": expected, "sampling": True}


def test_giraffe_settings_from_the_environment(monkeypatch):
    monkeypatch.setenv("GIRAFFE_GRAPH", "hprc-v1.1-mc-grch38.chr20.gbz")
    monkeypatch.setenv("GIRAFFE_SAMPLING", "OFF")
    assert tasks.giraffe_settings("chr20.fa") == {"graph": "hprc-v1.1-mc-grch38.chr20.gbz", "sampling": False}


@pytest.mark.parametrize(
    "var, value, match",
    [
        ("GIRAFFE_GRAPH", "graph.gfa", "GIRAFFE_GRAPH"),
        ("GIRAFFE_GRAPH", "sub/graph.gbz", "GIRAFFE_GRAPH"),
        ("GIRAFFE_GRAPH", ".gbz", "GIRAFFE_GRAPH"),
        ("GIRAFFE_SAMPLING", "yes", "GIRAFFE_SAMPLING"),
    ],
)
def test_bad_giraffe_settings_are_rejected(monkeypatch, var, value, match):
    monkeypatch.setenv(var, value)
    with pytest.raises(ValueError, match=match):
        tasks.giraffe_settings("chr20.fa")


@pytest.mark.parametrize("value, expected", [("", tasks.DEFAULT_KMC_MEMORY_GB), ("12", 12)])
def test_kmc_memory(monkeypatch, value, expected):
    monkeypatch.setenv("KMC_MEMORY_GB", value)
    assert tasks.kmc_memory_gb() == expected


@pytest.mark.parametrize("value", ["0", "-1", "8g", "1.5"])
def test_bad_kmc_memory_is_rejected(monkeypatch, value):
    monkeypatch.setenv("KMC_MEMORY_GB", value)
    with pytest.raises(ValueError, match="KMC_MEMORY_GB"):
        tasks.kmc_memory_gb()


def test_giraffe_is_an_aligner_but_has_no_fasta_index():
    assert tasks.check_aligner("giraffe") == "giraffe"
    assert "giraffe" in tasks.ALIGNERS and "giraffe" not in tasks.ALIGNER_INDEX_EXTS
    assert set(run_local.ALIGNERS) == set(tasks.ALIGNERS)


def test_the_stage_and_the_benchmark_use_the_same_giraffe_settings():
    # #129's accuracy only carries over while the stage maps like scripts/benchmark_giraffe.py does.
    assert tasks.KMER_LENGTH == benchmark_giraffe.KMER_LENGTH
    bench = benchmark_giraffe.giraffe_command("S", ["r1", "r2"], benchmark_giraffe.graph_keys("g.gbz"), "k", "u", 8)[2]
    stage = tasks.giraffe_command(FILES, ["r1", "r2"], "u", 8, "S", "p", "k", "s")
    assert (
        f"-P -L {tasks.GIRAFFE_MAX_FRAGMENT_LENGTH} " in bench
        and f"-P -L {tasks.GIRAFFE_MAX_FRAGMENT_LENGTH} " in stage
    )
    for flag in ("--haplotype-name", "--kff-name", "--index-basename", "-o BAM", "--ref-paths"):
        assert flag in bench and flag in stage
    bench_index = benchmark_giraffe.vg_index_commands(benchmark_giraffe.graph_keys("g.gbz"), 8)
    stage_index = tasks.haplotype_index_command(FILES, 8).split(" && ")
    for step, words in zip(("dist", "ri", "hapl"), stage_index, strict=True):
        assert words.split()[:2] == bench_index[step].split()[:2]  # vg index / vg gbwt / vg haplotypes


def test_graph_files_sit_next_to_the_gbz(tmp_path):
    files = tasks.giraffe_files(str(tmp_path), "hprc-v1.1-mc-grch38.chr20.gbz")
    base = str(tmp_path / "hprc-v1.1-mc-grch38.chr20")
    assert files == {k: f"{base}.{k}" for k in ("gbz", "hapl", "dist", "min", "ri")}


# --- commands ------------------------------------------------------------------------------------------

FILES = tasks.giraffe_files("/w", GRAPH)


def test_giraffe_command_with_haplotype_sampling_single_end():
    cmd = tasks.giraffe_command(FILES, ["/w/S1.fastq.gz"], "/w/u.bam", 8, "S1", "/w/p.txt", "/w/k.kff", "/w/sampled")
    words = shlex.split(cmd)
    assert words[:6] == ["vg", "giraffe", "-t", "8", "-Z", FILES["gbz"]]
    pairs = dict(zip(words[6::], words[7::], strict=False))
    assert pairs["--haplotype-name"] == FILES["hapl"] and pairs["--kff-name"] == "/w/k.kff"
    assert pairs["--index-basename"] == "/w/sampled"
    assert pairs["-N"] == "S1" and pairs["-R"] == "S1"  # -> @RG ID:S1 SM:S1
    assert pairs["-o"] == "BAM" and pairs["--ref-paths"] == "/w/p.txt"
    assert "-P" in words and "-L" not in words  # -L only means something for pairs
    assert words[-4:] == ["-f", "/w/S1.fastq.gz", ">", "/w/u.bam"]


def test_giraffe_command_paired_end_gives_both_mates_and_the_fragment_cap():
    cmd = tasks.giraffe_command(FILES, ["/w/R1.fq.gz", "/w/R2.fq.gz"], "/w/u.bam", 8, "S1", "/w/p.txt")
    words = shlex.split(cmd)
    assert words[words.index("-L") + 1] == str(tasks.GIRAFFE_MAX_FRAGMENT_LENGTH)
    assert words[-6:] == ["-f", "/w/R1.fq.gz", "-f", "/w/R2.fq.gz", ">", "/w/u.bam"]
    # Without sampling: the whole graph; giraffe finds (or builds) its .dist/.min from the GBZ's name.
    assert not {"--haplotype-name", "--kff-name", "--index-basename", "-d", "-m"} & set(words)


def test_giraffe_command_checks_its_arguments():
    with pytest.raises(ValueError, match="FASTQs"):
        tasks.giraffe_command(FILES, ["a", "b", "c"], "/w/u.bam", 8, "S1", "/w/p.txt")
    with pytest.raises(ValueError, match="kff and sampled"):
        tasks.giraffe_command(FILES, ["a"], "/w/u.bam", 8, "S1", "/w/p.txt", kff="/w/k.kff")


def test_kmc_command_counts_k29_into_kff():
    words = shlex.split(tasks.kmc_command("/w/list.txt", "/w/kmers", "/w/tmp", 8, 4))
    assert words == ["kmc", "-k29", "-m4", "-okff", "-t8", "-hp", "@/w/list.txt", "/w/kmers", "/w/tmp"]
    assert tasks.KMER_LENGTH == 29  # vg haplotypes' default k; KFF counts at another k can't be used


def test_only_the_missing_haplotype_inputs_are_built():
    assert [s.split()[:2] for s in tasks.haplotype_index_command(FILES, 4, [".hapl"]).split(" && ")] == [
        ["vg", "haplotypes"]
    ]
    assert [s.split()[1] for s in tasks.haplotype_index_command(FILES, 4, [".ri", ".hapl"]).split(" && ")] == [
        "gbwt",
        "haplotypes",
    ]


def test_haplotype_index_is_built_from_distance_index_and_r_index():
    steps = [shlex.split(s) for s in tasks.haplotype_index_command(FILES, 4).split(" && ")]
    assert [s[:2] for s in steps] == [["vg", "index"], ["vg", "gbwt"], ["vg", "haplotypes"]]
    f = FILES
    assert steps[0][-3:] == ["-j", f["dist"], f["gbz"]]
    assert steps[1][steps[1].index("-r") + 1] == f["ri"]
    assert steps[2][-5:] == ["-r", f["ri"], "-H", f["hapl"], f["gbz"]]


def test_align_command_refuses_giraffe():
    with pytest.raises(ValueError, match="giraffe_align"):
        tasks.align_command("/w/ref.fa", "/w/S1.fastq.gz", "/w/S1.cram", 4, "/w", "S1", "giraffe")


@pytest.mark.parametrize("paired", [False, True])
def test_bwa_and_giraffe_share_the_post_processing(paired):
    fastqs = ["/w/R1.fq.gz", "/w/R2.fq.gz"] if paired else ["/w/S1.fq.gz"]
    bwa_tail = tasks.align_command("/w/ref.fa", fastqs, "/w/S1.cram", 4, "/w", "S1", "bwa").split(" | ")[1:]
    assert bwa_tail == tasks.postprocess_stages("/w/ref.fa", "/w/S1.cram", 4, "/w", paired)
    assert bwa_tail[0].startswith("samtools fixmate") == paired


# --- reference paths and contig rename ---------------------------------------------------------------

FAI3 = "chr1\t248956422\t6\t60\t61\nchr2\t242193529\t1\t60\t61\nchr10\t133797422\t1\t60\t61\n"


def test_reference_paths_come_in_the_fasta_order():
    # vg paths lists chr1, chr10, chr2; the CRAM's contigs must follow the .fai, as bwa's do.
    paths = "GRCh38#0#chr1\nGRCh38#0#chr10\nGRCh38#0#chr2\n"
    assert tasks.reference_path_order(paths, FAI3) == ["GRCh38#0#chr1", "GRCh38#0#chr2", "GRCh38#0#chr10"]


def test_reference_paths_of_another_sample_are_dropped():
    # vg paths -Q GRCh38 matches by prefix: GRCh38x is another sample.
    assert tasks.reference_path_order("GRCh38x#0#chr2\nGRCh38#0#chr1\n\n", FAI3) == ["GRCh38#0#chr1"]


@pytest.mark.parametrize(
    "paths, match",
    [
        ("GRCh38#0#chr1\nGRCh38#1#chr1\n", "both the reference's chr1"),
        ("GRCh38#0#chrM\n", "not a contig of the reference"),
        ("CHM13#0#chr1\n", "no GRCh38 paths"),
        ("", "no GRCh38 paths"),
    ],
)
def test_reference_paths_that_do_not_fit_the_fasta_are_refused(paths, match):
    with pytest.raises(ValueError, match=match):
        tasks.reference_path_order(paths, FAI3)


def test_rename_refuses_a_contig_twice():
    header = "@SQ\tSN:GRCh38#0#chr20\tLN:64444167\n@SQ\tSN:GRCh38#chr20\tLN:64444167\n"
    with pytest.raises(ValueError, match="chr20 twice"):
        tasks.rename_graph_contigs(header, FAI)


def test_rename_strips_the_reference_sample_from_sq_lines_only():
    renamed = tasks.rename_graph_contigs(GIRAFFE_HEADER, FAI)
    assert renamed.splitlines() == [
        "@HD\tVN:1.5\tSO:unknown",
        "@SQ\tSN:chr20\tLN:64444167",
        "@RG\tID:S1\tSM:S1",
        "@PG\tID:0\tPN:vg",
    ]


def test_rename_accepts_names_without_a_haplotype_number_and_names_already_plain():
    header = "@SQ\tSN:GRCh38#chr20\tLN:64444167\n@SQ\tSN:chrM\tLN:16569\n"
    fai = FAI + "chrM\t16569\t1\t60\t61\n"
    assert [line.split("\t")[1] for line in tasks.rename_graph_contigs(header, fai).splitlines()] == [
        "SN:chr20",
        "SN:chrM",
    ]


@pytest.mark.parametrize(
    "header, match",
    [
        ("@SQ\tSN:CHM13#0#chr20\tLN:64444167\n", "not a contig of the reference"),  # another sample's path
        ("@SQ\tSN:GRCh38#0#chr21\tLN:46709983\n", "not a contig of the reference"),
        ("@SQ\tSN:GRCh38#0#chr20\tLN:64000000\n", "64000000 bp"),  # same name, another build
        ("@HD\tVN:1.5\n@RG\tID:S1\n", "no @SQ lines"),
        ("@SQ\tLN:5\n", "without SN"),
    ],
)
def test_rename_refuses_a_graph_that_does_not_match_the_reference(header, match):
    with pytest.raises(ValueError, match=match):
        tasks.rename_graph_contigs(header, FAI)


# --- the align stage, tools faked ----------------------------------------------------------------------


@pytest.fixture
def giraffe_tools(local_env, shell_commands, monkeypatch):
    """run_shell faked per tool: vg/kmc write what they'd write; samtools and the stats tools go to the shared
    fake (conftest.shell_commands). samtools view -H (subprocess.run) returns GIRAFFE_HEADER."""
    commands, shared = shell_commands, tasks.run_shell

    def fake_run_shell(command):
        words = shlex.split(command)
        if words[0] == "vg" or words[0] == "kmc":
            commands.append(command)
            if words[:2] == ["vg", "paths"]:
                with open(words[-1], "w") as fh:
                    fh.write("GRCh38#0#chr20\n")
            elif words[:2] == ["vg", "giraffe"]:
                with open(words[-1], "wb") as fh:
                    fh.write(b"unsorted bam")
            elif words[0] == "kmc":
                open(words[-2] + ".kff", "wb").close()
            else:  # the haplotype index build: each step writes the file after -j / -r / -H
                for step in map(shlex.split, command.split(" && ")):
                    for flag in ("-j", "-r", "-H"):
                        if flag in step:
                            open(step[step.index(flag) + 1], "wb").close()
            return
        shared(command)

    def fake_run(cmd, check=False, capture_output=False, text=False, **kwargs):
        if cmd[:3] == ["samtools", "view", "-H"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=GIRAFFE_HEADER)
        raise AssertionError(f"unexpected command {cmd}")

    monkeypatch.setattr(tasks, "run_shell", fake_run_shell)
    monkeypatch.setattr(tasks.subprocess, "run", fake_run)
    monkeypatch.setattr(tasks.shutil, "which", lambda name: f"/usr/local/bin/{name}")  # as in geyser-giraffe
    monkeypatch.setenv("ALIGNER", "giraffe")
    put(local_env, "raw_reads/S1.fastq.gz", b"reads")
    put(local_env, f"reference/{REF}", b">chr20\nACGT\n")
    put(local_env, f"reference/{REF}.fai", FAI.encode())
    put(local_env, f"pangenome/{GRAPH}", b"graph")
    return commands


def tools(commands):
    return [" ".join(shlex.split(c)[:2]) for c in commands]


def test_align_with_giraffe_sampling(local_env, giraffe_tools, uploads, monkeypatch):
    put(local_env, "pangenome/ref.hapl")
    monkeypatch.setenv("RUN_NAME", "S1-run")
    seen = {}
    real = tasks.rename_graph_contigs
    monkeypatch.setattr(tasks, "rename_graph_contigs", lambda h, f: seen.setdefault("renamed", real(h, f)))

    tasks.align_task("S1", REF)

    assert tools(giraffe_tools) == [
        "vg paths",
        "kmc -k29",
        "vg giraffe",
        "samtools reheader",
        "samtools index",
        "samtools flagstat",
        "mosdepth -n",
    ]
    giraffe, post = giraffe_tools[2], giraffe_tools[3].split(" | ")
    assert "--haplotype-name" in giraffe and "ref.hapl" in giraffe and "kmers.kff" in giraffe
    assert "S1.fastq.gz" in giraffe  # giraffe reads the upload, still gzipped
    # reheader -> sort -> markdup to CRAM: no fixmate for single-end, the same tail as bwa.
    assert [s.split()[:2] for s in post] == [["samtools", "reheader"], ["samtools", "sort"], ["samtools", "markdup"]]
    assert "-O cram" in post[-1]
    assert "SN:chr20\t" in seen["renamed"]
    assert uploads[-2:] == ["alignments/S1.cram.crai", "alignments/S1.cram"]
    part = read_json(local_env, "runs/S1.provenance/Align.json")
    assert part["status"] == "Success" and part["aligner"] == "giraffe"
    assert part["graph"] == {"name": GRAPH, "haplotype_sampling": True}
    assert {"vg", "kmc", "samtools", "mosdepth"} <= set(part["tools"])
    assert [i["key"] for i in part["inputs"]] == ["raw_reads/S1.fastq.gz", f"pangenome/{GRAPH}", "pangenome/ref.hapl"]
    work = local_env.parent / "work"
    assert not work.exists() or not any(work.iterdir())


def test_a_stored_distance_index_and_r_index_are_used_to_build_the_hapl(local_env, giraffe_tools):
    put(local_env, "pangenome/ref.dist")
    put(local_env, "pangenome/ref.ri")

    tasks.align_task("S1", REF)

    assert tools(giraffe_tools)[0] == "vg haplotypes"  # no vg index / vg gbwt: their outputs were stored


def test_a_partly_stored_whole_graph_index_is_fetched_and_the_rest_left_to_giraffe(
    local_env, giraffe_tools, monkeypatch, capsys
):
    monkeypatch.setenv("GIRAFFE_SAMPLING", "off")
    put(local_env, "pangenome/ref.dist", b"dist")

    tasks.align_task("S1", REF)

    part = read_json(local_env, "runs/S1.provenance/Align.json")
    assert "pangenome/ref.dist" in [i["key"] for i in part["inputs"]]
    assert ".min not stored; giraffe builds it" in capsys.readouterr().out


def test_giraffe_fails_before_any_download_without_vg(local_env, giraffe_tools, monkeypatch):
    monkeypatch.setattr(tasks.shutil, "which", lambda name: None if name == "vg" else f"/bin/{name}")
    downloads = []
    monkeypatch.setattr(tasks.storage(), "download", lambda key, path: downloads.append(key))
    with pytest.raises(FileNotFoundError, match="needs vg.*geyser-giraffe"):
        tasks.align_task("S1", REF)
    assert downloads == []


def test_a_missing_hapl_is_built_in_the_job(local_env, giraffe_tools, capsys):
    tasks.align_task("S1", REF)

    assert tools(giraffe_tools)[:3] == ["vg index", "vg paths", "kmc -k29"]
    assert "vg haplotypes" in giraffe_tools[0]
    assert "ref.hapl missing; building .dist, .ri, .hapl" in capsys.readouterr().out


def test_align_with_giraffe_on_the_whole_graph(local_env, giraffe_tools, monkeypatch):
    monkeypatch.setenv("GIRAFFE_SAMPLING", "off")
    put(local_env, "pangenome/ref.dist", b"dist")
    put(local_env, "pangenome/ref.min", b"min")
    downloaded = []
    store = tasks.storage()
    real = store.download
    monkeypatch.setattr(store, "download", lambda key, path: downloaded.append(key) or real(key, path))

    tasks.align_task("S1", REF)

    assert tools(giraffe_tools)[:3] == ["vg paths", "vg giraffe", "samtools reheader"]  # no kmc, no .hapl
    assert {"pangenome/ref.dist", "pangenome/ref.min"} <= set(downloaded)
    assert not any(k.endswith((".hapl", ".ri")) for k in downloaded)
    assert "--haplotype-name" not in giraffe_tools[1]
    part = read_json(local_env, "runs/S1.provenance/Align.json")
    assert part["graph"]["haplotype_sampling"] is False and "kmc" not in part["tools"]


def test_paired_end_giraffe_gives_both_mates_and_runs_fixmate(local_env, giraffe_tools):
    (local_env / "raw_reads" / "S1.fastq.gz").unlink()
    put(local_env, "raw_reads/S1_R1.fastq.gz")
    put(local_env, "raw_reads/S1_R2.fastq.gz")
    put(local_env, "pangenome/ref.hapl")

    tasks.align_task("S1", REF)

    giraffe = shlex.split(next(c for c in giraffe_tools if c.startswith("vg giraffe")))
    reads = [giraffe[i + 1] for i, w in enumerate(giraffe) if w == "-f"]
    assert [r.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] for r in reads] == ["S1_R1.fastq.gz", "S1_R2.fastq.gz"]
    assert "-L" in giraffe
    post = next(c for c in giraffe_tools if c.startswith("samtools reheader")).split(" | ")
    assert post[1].startswith("samtools fixmate -m")


def test_a_missing_graph_names_the_key_and_the_setting(local_env, giraffe_tools):
    (local_env / "pangenome" / GRAPH).unlink()
    with pytest.raises(FileNotFoundError, match=r"pangenome.ref\.gbz \(GIRAFFE_GRAPH\)"):
        tasks.align_task("S1", REF)


def test_a_graph_without_reference_paths_fails_clearly(local_env, giraffe_tools, monkeypatch):
    put(local_env, "pangenome/ref.hapl")
    real = tasks.run_shell

    def no_paths(command):
        real(command)
        if command.startswith("vg paths"):
            open(shlex.split(command)[-1], "w").close()

    monkeypatch.setattr(tasks, "run_shell", no_paths)
    with pytest.raises(ValueError, match="no GRCh38 paths"):
        tasks.align_task("S1", REF)


@pytest.mark.parametrize("var, value", [("GIRAFFE_SAMPLING", "maybe"), ("KMC_MEMORY_GB", "lots")])
def test_bad_giraffe_settings_fail_before_any_download(local_env, giraffe_tools, monkeypatch, var, value):
    monkeypatch.setenv(var, value)
    downloads = []
    monkeypatch.setattr(tasks.storage(), "download", lambda key, path: downloads.append(key))
    with pytest.raises(ValueError, match=var):
        tasks.align_task("S1", REF)
    assert downloads == [] and giraffe_tools == []


# --- run_local -----------------------------------------------------------------------------------------


def test_run_local_runs_giraffe_align_in_its_own_image():
    image, env = run_local.align_settings("giraffe", "synthetic_ref.gbz", "geyser-giraffe")
    assert image == "geyser-giraffe" and env == {"ALIGNER": "giraffe", "GIRAFFE_GRAPH": "synthetic_ref.gbz"}
    jobs = run_local.pipeline_jobs("S1", "ref.fa", align_image=image, align_env=env, gate=True)
    by_name = {j["name"]: j for j in jobs}
    assert by_name["align"]["image"] == "geyser-giraffe" and by_name["align"]["env"] == env
    assert {j["image"] for j in jobs if j["name"] != "align"} == {"geyser-app"}


def test_run_local_aligner_defaults():
    assert run_local.align_settings(None, None, "geyser-giraffe") == (None, {})
    assert run_local.align_settings("bwa", "x.gbz", "geyser-giraffe") == (None, {"ALIGNER": "bwa"})
    # No --pangenome: tasks.py picks <reference stem>.gbz.
    assert run_local.align_settings("giraffe", None, "g") == ("g", {"ALIGNER": "giraffe"})
    assert "env" not in {j["name"]: j for j in run_local.pipeline_jobs("S1", "ref.fa")}["align"]


def test_run_local_main_passes_the_giraffe_image_and_graph(tmp_path, monkeypatch):
    put(tmp_path, "raw_reads/S1.fastq.gz")
    ran = []
    monkeypatch.setattr(run_local, "run_job", lambda job, *a, **k: ran.append(job) or 0)
    monkeypatch.delenv("ALIGNER", raising=False)
    args = ["--root", str(tmp_path), "--sample", "S1", "--reference", "ref.fa", "--no-gate"]
    assert run_local.main(args + ["--aligner", "giraffe", "--pangenome", "g.gbz"]) == 0
    align = next(j for j in ran if j["name"] == "align")
    assert align["image"] == "geyser-giraffe" and align["env"]["GIRAFFE_GRAPH"] == "g.gbz"

    ran.clear()
    # Aligner and pangenome caller without --pangenome: both get the caller's default graph, not two graphs.
    assert run_local.main(args + ["--aligner", "giraffe", "--caller", "pangenome"]) == 0
    align = next(j for j in ran if j["name"] == "align")
    assert align["env"]["GIRAFFE_GRAPH"] == run_local.DEFAULT_PANGENOME

    ran.clear()
    monkeypatch.setenv("ALIGNER", "giraffe")  # set on the host instead: still the giraffe image
    assert run_local.main(args) == 0
    assert next(j for j in ran if j["name"] == "align")["image"] == "geyser-giraffe"
