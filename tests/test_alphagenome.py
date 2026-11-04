"""AlphaGenome annotation (3.0 P1): selection, backends, outputs, the annotate stage and run_local.

Offline only: the fake backend, and synthetic AVI tables (never real AVI or Atlas data: their terms). No key is
used; where one is needed, a made-up one checks that it never leaks.
"""

import gzip
import json
import shlex
import shutil
import subprocess
import traceback

import pytest

import alphagenome_annotation as ag
import provenance
import run_local
import tasks

SECRET = "AGtest-0000-not-a-real-key-SECRET"
CONTIGS = "##contig=<ID=chr20,length=64444167>\n##contig=<ID=chr21,length=46709983>\n"
HEADER = "##fileformat=VCFv4.2\n" + CONTIGS
BCSQ_HEADER = (
    '##INFO=<ID=BCSQ,Number=.,Type=String,Description="Haplotype-aware consequence annotation from BCFtools/csq">\n'
)
COLUMNS = "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS1\n"


def vcf(lines, csq=False):
    return HEADER + (BCSQ_HEADER if csq else "") + COLUMNS + "".join(line + "\n" for line in lines)


def rec(chrom, pos, ref, alt, qual=50, filt=".", info="DP=30"):
    return f"{chrom}\t{pos}\t.\t{ref}\t{alt}\t{qual}\t{filt}\t{info}\tGT\t0/1"


def read(path_text, has_csq=False, cap=500):
    path, text = path_text
    path.write_text(text)
    header, contigs, has_bcsq, records = ag.read_vcf(str(path))
    assert has_bcsq == has_csq
    return ag.select(records, contigs, has_bcsq, cap)


# --- csq parsing and selection -----------------------------------------------------------------------------


def test_bcsq_consequences_and_genes():
    info = "DP=3;BCSQ=missense|G1|T1|protein_coding|+|5A>5T|100A>T,*intron&splice_region|G2|T2|protein_coding,@123"
    assert ag.parse_bcsq(info) == (("missense", "intron", "splice_region"), ("G1", "G2"))
    assert ag.parse_bcsq("DP=3") == ((), ())


def test_selection_keeps_pass_snvs_and_indels_ranked_by_qual(tmp_path):
    lines = [
        rec("chr20", 100, "A", "G", qual=20),
        rec("chr20", 200, "AT", "A", qual=90),  # an indel
        rec("chr20", 300, "C", "T", qual=60, filt="LowQual"),  # not PASS
        rec("chr20", 400, "G", "<*>", qual=99),  # symbolic: not scorable
        rec("chr20", 500, "AC", "GT", qual=99),  # an MNP: not scorable
        rec("chr20", 600, "N", "A", qual=99),  # not ACGT
        rec("chr20", 700, "C", "A,T", qual=40, filt="PASS"),  # multi-allelic: one candidate per ALT
        rec("chr21", 50, "T", "C", qual="."),  # no QUAL: ranked last
        rec("chr20", 800, "G", "A", qual=40),  # ties with 700: position breaks it
    ]
    selected, counts = read((tmp_path / "c.vcf", vcf(lines)))
    assert [v.key for v in selected] == [
        "chr20:200:AT:A",
        "chr20:700:C:A",
        "chr20:700:C:T",
        "chr20:800:G:A",
        "chr20:100:A:G",
        "chr21:50:T:C",
    ]
    assert [v.kind for v in selected][:2] == ["indel", "snv"]
    assert counts == {
        "records": 9,
        "pass": 8,
        "snvs": 5,
        "indels": 1,
        "not_scorable": 3,
        "candidates": 6,
        "selected": 6,
        "dropped_by_cap": 0,
    }


def test_the_cap_keeps_the_highest_qual(tmp_path):
    lines = [rec("chr20", 100 + i, "A", "G", qual=i) for i in range(10)]
    selected, counts = read((tmp_path / "c.vcf", vcf(lines)), cap=3)
    assert [v.pos for v in selected] == [109, 108, 107]
    assert counts["candidates"] == 10 and counts["selected"] == 3 and counts["dropped_by_cap"] == 7


def test_with_consequences_only_near_gene_non_coding_variants_are_selected(tmp_path):
    lines = [
        rec("chr20", 100, "A", "G", info="BCSQ=missense|G1|T1|protein_coding|+|5A>5T|100A>T"),  # coding only
        rec("chr20", 200, "A", "G", info="BCSQ=intron|G2|T2|protein_coding"),
        rec("chr20", 300, "A", "G", info="BCSQ=3_prime_utr|G3|T3|protein_coding"),
        rec("chr20", 400, "A", "G", info="BCSQ=synonymous&splice_region|G4|T4|protein_coding|+|1A|3G>A"),
        rec("chr20", 500, "A", "G", info="DP=30"),  # intergenic: no consequence, not near a gene csq knows
    ]
    selected, counts = read((tmp_path / "c.vcf", vcf(lines, csq=True)), has_csq=True)
    assert sorted(v.pos for v in selected) == [200, 300, 400]
    assert counts["near_gene"] == 3 and counts["snvs"] == 5
    by_pos = {v.pos: v for v in selected}
    assert by_pos[400].consequences == ("synonymous", "splice_region") and by_pos[400].genes == ("G4",)
    assert by_pos[200].bcsq == "intron|G2|T2|protein_coding"


def test_an_empty_vcf_selects_nothing(tmp_path):
    selected, counts = read((tmp_path / "c.vcf", vcf([])))
    assert selected == [] and counts["selected"] == 0


@pytest.mark.parametrize("raw,expected", [(None, 500), ("", 500), ("25", 25), (" 7 ", 7)])
def test_max_variants_from_env(raw, expected):
    env = {} if raw is None else {ag.MAX_VARIANTS_ENV: raw}
    assert ag.max_variants_from_env(env) == expected


@pytest.mark.parametrize("raw", ["0", "-3", "ten", "2.5"])
def test_a_bad_max_variants_is_an_error(raw):
    with pytest.raises(ValueError, match=ag.MAX_VARIANTS_ENV):
        ag.max_variants_from_env({ag.MAX_VARIANTS_ENV: raw})


# --- backends ---------------------------------------------------------------------------------------------

V1 = ag.Variant("chr20", 100, "A", "G", 50.0)
V2 = ag.Variant("chr20", 200, "AT", "A", 40.0)


def test_the_fake_backend_is_deterministic_and_counts_calls():
    first, second = ag.FakeBackend(), ag.FakeBackend()
    assert first.score([V1, V2]) == second.score([V1, V2])
    assert first.api_calls == 2
    score = first.score([V1])[V1.key]
    assert score.source == "fake" and 0 <= score.avi <= 80 and score.top_track and score.gene
    assert -1 <= score.rna_lfc <= 1


def test_the_fake_backend_uses_recorded_responses(tmp_path, monkeypatch):
    path = tmp_path / "responses.json"
    path.write_text(json.dumps({V1.key: {"avi": 33.5, "top_track": "RNA_SEQ:liver", "gene": "GENE1"}}))
    monkeypatch.setenv(ag.FAKE_RESPONSES_ENV, str(path))
    backend = ag.backend_for("fake")
    scores = backend.score([V1, V2])
    assert scores[V1.key] == ag.Score("fake", avi=33.5, top_track="RNA_SEQ:liver", gene="GENE1")
    assert V2.key in scores  # not recorded: derived


def write_avi_table(path, rows, header=ag.AVI_FILE_COLUMNS):
    # Synthetic values only: real AVI data is never committed or used in tests.
    with gzip.open(path, "wt") as fh:
        fh.write("\t".join(header) + "\n")
        for row in rows:
            fh.write("\t".join(str(c) for c in row) + "\n")


def test_the_avi_file_backend_looks_up_snvs_only(tmp_path):
    table = tmp_path / "chr20.fa.avi.tsv.gz"
    write_avi_table(table, [("chr20", 100, "A", "G", 1.25, 22.5), ("chr20", 100, "A", "C", -0.5, 1.0)])
    backend = ag.backend_for("avi-file", table=str(table))
    scores = backend.score([V1, V2, ag.Variant("chr20", 999, "C", "T", 9.0)])
    assert scores == {V1.key: ag.Score("avi-file", avi=22.5, avi_raw=1.25)}  # no indel, no missing SNV
    assert backend.api_calls == 0 and backend.model_version == "chr20.fa.avi.tsv.gz"


def test_the_avi_file_backend_refuses_a_table_that_is_not_one(tmp_path):
    table = tmp_path / "t.tsv.gz"
    write_avi_table(table, [], header=("chrom", "pos", "ref", "alt", "avi"))
    with pytest.raises(ValueError, match="not an AVI table"):
        ag.backend_for("avi-file", table=str(table)).score([V1])


@pytest.mark.parametrize("source", ["atlas", "api"])
def test_keyed_backends_need_the_key_and_never_show_it(source, monkeypatch):
    monkeypatch.delenv(ag.API_KEY_ENV, raising=False)
    with pytest.raises(ValueError, match=ag.API_KEY_ENV):
        ag.backend_for(source)
    monkeypatch.setenv(ag.API_KEY_ENV, SECRET)
    backend = ag.backend_for(source)
    assert SECRET not in repr(backend) and SECRET not in str(vars(backend).keys())
    assert backend.score([]) == {}


def test_the_api_backend_is_not_wired_in_yet(monkeypatch):
    monkeypatch.setenv(ag.API_KEY_ENV, SECRET)
    with pytest.raises(NotImplementedError, match="not wired in yet") as err:
        ag.backend_for("api").score([V1])
    assert SECRET not in str(err.value)


# A stand-in for the Atlas client's answer: {scorer: AnnData}, with X, layers["quantiles"], var (tracks) and obs.
class Data:
    def __init__(self, x, quantiles, tracks, genes=None):
        self.X, self.layers, self.var, self.obs = x, {"quantiles": quantiles}, tracks, genes


DNASE = Data(
    [[0.01, -0.05]],
    [[0.2, -0.7]],
    [{"name": "DNase-seq", "biosample_name": "liver"}, {"name": "x", "biosample_name": "K562"}],
)
CHIP = Data([[0.02]], [[0.4]], [{"name": "h", "histone_mark": "H3K27ac", "biosample_name": "lung"}])
RNA = Data(
    [[0.01, 0.02], [-0.3, 0.05]],  # genes x tracks: raw log fold changes
    [[0.1, 0.2], [-0.95, 0.3]],
    [{"name": "RNA-seq", "biosample_name": "liver"}, {"name": "RNA-seq", "biosample_name": "lung"}],
    [{"gene_id": "ENSG1", "gene_name": "PAK5"}, {"gene_id": "ENSG2", "gene_name": "SNAP25"}],
)


def test_reduce_atlas_takes_the_largest_quantile_and_the_top_genes_lfc():
    score = ag.reduce_atlas({"DNASE": DNASE, "CHIP_HISTONE": CHIP, "RNA_SEQ": RNA})
    # |-0.95| (RNA_SEQ, SNAP25 in liver) beats DNASE's |-0.7|: quantiles compare across scorers, raw scores don't.
    assert score == ag.Score("atlas", top_track="RNA_SEQ:liver", gene="SNAP25", rna_lfc=-0.3)
    assert ag.reduce_atlas({"DNASE": DNASE, "CHIP_HISTONE": CHIP}) == ag.Score("atlas", top_track="DNASE:K562")
    assert ag.reduce_atlas({"CHIP_HISTONE": CHIP}).top_track == "CHIP_HISTONE:H3K27ac:lung"
    assert ag.reduce_atlas({}) is None
    assert ag.reduce_atlas({"DNASE": Data([], [], [])}) is None


class FakeAtlasClient:
    """query_variant(variant, requested_scorers=...) as the real client has it; NOT_FOUND is a ValueError there."""

    def __init__(self, missing=(), error=None):
        self.missing, self.error, self.asked = set(missing), error, []

    def query_variant(self, variant, *, requested_scorers):
        self.asked.append((variant, tuple(requested_scorers)))
        if self.error:
            raise self.error
        if variant in self.missing:
            raise ValueError(f"Variant {variant} not found")
        return {"DNASE": DNASE, "RNA_SEQ": RNA}


def atlas_backend(client):
    return ag.AtlasBackend(SECRET, client=client, make_variant=lambda *v: v, workers=2)


def test_the_atlas_backend_scores_snvs_and_leaves_missing_ones_unscored():
    v3 = ag.Variant("chr20", 300, "G", "C", 9.0)
    client = FakeAtlasClient(missing={("chr20", 300, "G", "C")})
    backend = atlas_backend(client)
    scores = backend.score([V1, V2, v3])  # V2 is an indel: not in the Atlas, never asked
    assert scores == {V1.key: ag.Score("atlas", top_track="RNA_SEQ:liver", gene="SNAP25", rna_lfc=-0.3)}
    assert sorted(v for v, _ in client.asked) == [("chr20", 100, "A", "G"), ("chr20", 300, "G", "C")]
    assert client.asked[0][1] == ag.ATLAS_SCORERS and not any(s.endswith("_ACTIVE") for s in client.asked[0][1])
    assert backend.api_calls == 2 and backend.not_found == 1
    diagnostics = backend.diagnostics()
    assert diagnostics["not_in_service"] == 1 and diagnostics["empty_answers"] == 0
    assert diagnostics["not_in_service_reasons"] == {"atlas: Variant ('chrN', N, 'G', 'C') not found": 1}
    assert diagnostics["not_in_service_examples"] == [
        ["chr20:300:G:C", "atlas: Variant ('chr20', 300, 'G', 'C') not found"]
    ]
    assert backend.model_version.startswith("alphagenome-client ")


def test_an_atlas_error_fails_the_annotation_with_the_key_redacted():
    backend = atlas_backend(FakeAtlasClient(error=PermissionError(f"API key {SECRET} not valid")))
    with pytest.raises(RuntimeError) as err:
        backend.score([V1])
    assert SECRET not in str(err.value) and "PermissionError" in str(err.value)
    assert SECRET not in "".join(traceback.format_exception(err.value))


def test_the_atlas_backend_without_the_client_says_how_to_install_it(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_alphagenome(name, *args, **kwargs):
        if name.startswith("alphagenome"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_alphagenome)
    with pytest.raises(RuntimeError, match="requirements-alphagenome.txt"):
        ag.AtlasBackend(SECRET).score([V1])


def test_a_client_error_containing_the_key_is_redacted(monkeypatch):
    monkeypatch.setenv(ag.API_KEY_ENV, SECRET)
    backend = ag.backend_for("api")

    def failing_client(variant):
        raise ConnectionError(f"401 for https://example.invalid/v1/score?key={SECRET} ({variant.key})")

    with pytest.raises(RuntimeError) as err:
        backend._call(failing_client, V1)
    assert SECRET not in str(err.value) and "<redacted>" in str(err.value) and "ConnectionError" in str(err.value)
    # Nothing a log would print carries it: not the message, not the traceback (no chained original).
    assert SECRET not in "".join(traceback.format_exception(err.value))
    assert backend.api_calls == 1


def test_the_api_backend_takes_only_the_allowed_intervals(monkeypatch):
    assert ag.ApiBackend(SECRET).interval == 1_048_576
    with pytest.raises(ValueError, match="one of"):
        ag.ApiBackend(SECRET, interval=1_000_000)


def test_an_unknown_source_is_an_error():
    with pytest.raises(ValueError, match="unknown AlphaGenome source"):
        ag.backend_for("gnomad")


# --- outputs ----------------------------------------------------------------------------------------------


def test_the_vcf_has_the_tags_a_source_has_sorted_by_position():
    v3 = ag.Variant("chr20", 50, "C", "T", 10.0, ("intron",), ("G9",), "intron|G9|T9|protein_coding")
    rows = [
        (V1, ag.Score("fake", avi=12.5, avi_raw=0.5, top_track="RNA_SEQ:liver", gene="G1", rna_lfc=-0.25)),
        (v3, ag.Score("avi-file", avi=3.0, avi_raw=-0.1)),
    ]
    text = ag.vcf_text(
        rows, HEADER.splitlines() + [BCSQ_HEADER.strip()], ["chr20", "chr21"], "chr20.fa", "fake", "fake-1"
    )
    lines = text.splitlines()
    for tag in ("AG_AVI", "AG_AVI_RAW", "AG_TOP_TRACK", "AG_GENE", "AG_RNA_LFC", "AG_SOURCE", "BCSQ"):
        assert any(line.startswith(f"##INFO=<ID={tag},") for line in lines), tag
    assert "##alphagenome_source=fake (deterministic test data, not predictions)" in lines
    assert "##AlphaGenomeTerms=" + ag.TERMS_NOTICE in lines  # the Output Terms' notice
    assert (
        "##contig=<ID=chr20,length=64444167>" in lines and lines[-3] == "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO"
    )
    assert (
        lines[-2]
        == "chr20\t50\t.\tC\tT\t10\tPASS\tAG_AVI=3;AG_AVI_RAW=-0.1;AG_GENE=G9;AG_SOURCE=avi-file;BCSQ=intron|G9|T9|protein_coding"
    )
    assert lines[-1] == (
        "chr20\t100\t.\tA\tG\t50\tPASS\tAG_AVI=12.5;AG_AVI_RAW=0.5;AG_TOP_TRACK=RNA_SEQ:liver;AG_GENE=G1;"
        "AG_RNA_LFC=-0.25;AG_SOURCE=fake"
    )


def test_the_tsv_is_ranked_by_avi_then_lfc():
    v3 = ag.Variant("chr20", 300, "G", "C", 99.0, ("intron",), ("G3",))
    rows = [
        (V1, ag.Score("fake", avi=5.0, rna_lfc=0.1)),
        (V2, ag.Score("fake", avi=20.0)),
        (v3, ag.Score("fake", avi=5.0, rna_lfc=-0.9)),
    ]
    lines = ag.tsv_text(rows).splitlines()
    assert lines[0] == "#AlphaGenomeTerms=" + ag.TERMS_NOTICE  # a comment line readers skip
    assert lines[1].split("\t") == list(ag.TSV_COLUMNS)
    table = [dict(zip(ag.TSV_COLUMNS, line.split("\t"), strict=True)) for line in lines[2:]]
    assert [(r["rank"], r["pos"]) for r in table] == [("1", "200"), ("2", "300"), ("3", "100")]
    assert table[1]["gene"] == "G3" and table[1]["csq"] == "intron" and table[0]["kind"] == "indel"
    assert table[0]["avi_raw"] == "" and table[0]["top_track"] == ""  # what the source doesn't have stays empty


def test_t3s_columns_are_all_there():
    # The web UI (#206) reads these by name.
    assert {"chrom", "pos", "ref", "alt", "gene", "avi", "top_track", "csq", "source", "rna_lfc"} <= set(ag.TSV_COLUMNS)


@pytest.mark.skipif(not shutil.which("bcftools"), reason="bcftools not installed (the Docker E2E checks it in CI)")
def test_bcftools_reads_the_annotated_vcf_back(tmp_path):
    rows = [(V1, ag.Score("fake", avi=12.5, top_track="RNA_SEQ:liver", gene="G1", rna_lfc=0.5))]
    plain = tmp_path / "a.vcf"
    plain.write_text(ag.vcf_text(rows, HEADER.splitlines(), ["chr20", "chr21"], "chr20.fa", "fake", "fake-1"))
    out = tmp_path / "a.vcf.gz"
    subprocess.run(["bcftools", "view", "-Oz", "-o", str(out), str(plain)], check=True)
    subprocess.run(["bcftools", "index", "-t", str(out)], check=True)
    query = subprocess.run(
        ["bcftools", "query", "-r", "chr20", "-f", "%POS %INFO/AG_AVI %INFO/AG_GENE %INFO/AG_SOURCE\n", str(out)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert query.stdout == "100 12.5 G1 fake\n"


# --- the annotate stage -----------------------------------------------------------------------------------


@pytest.fixture
def stage(local_env, monkeypatch):
    """Local storage with S1's calls, a faked bcftools (view -Oz: gzip; index: an empty .tbi), no key."""
    monkeypatch.setattr(provenance, "_current", None)
    for var in (ag.API_KEY_ENV, "ALPHAGENOME_SOURCE", ag.MAX_VARIANTS_ENV, "PIPELINE_CALLER"):
        monkeypatch.delenv(var, raising=False)
    commands = []

    def fake_run_shell(command):
        commands.append(command)
        words = shlex.split(command)
        out = words[words.index("-o") + 1]
        if words[:2] == ["bcftools", "view"]:
            with open(words[-1], "rb") as src, gzip.open(out, "wb") as dst:
                dst.write(src.read())
        else:
            open(out, "wb").close()

    monkeypatch.setattr(tasks, "run_shell", fake_run_shell)
    real_which = shutil.which
    monkeypatch.setattr(
        tasks.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "bcftools" else real_which(name)
    )
    store = tasks.storage()

    def put_vcf(key, lines, csq=False):
        path = local_env / key
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt") as fh:
            fh.write(vcf(lines, csq))

    def core_run():
        monkeypatch.setenv("RUN_NAME", "S1-20261104T010000Z-local")
        for task in provenance.CORE_STAGES:
            provenance.begin(task, "S1")
            provenance.finish(store, "Success", 1.0)

    return {"root": local_env, "store": store, "commands": commands, "put_vcf": put_vcf, "core_run": core_run}


def metric_lines(root):
    path = root / "metrics" / "metrics.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


LINES = [rec("chr20", 100 + i, "A", "G", qual=10 * i) for i in range(5)] + [rec("chr20", 900, "AT", "A", qual=7)]


def test_annotate_writes_the_vcf_tsv_part_and_metrics(stage, uploads, capsys):
    stage["core_run"]()
    stage["put_vcf"]("variants/S1.vcf.gz", LINES)
    assert tasks.main(["annotate", "S1", "chr20.fa", "--source", "fake", "--max-variants", "4"]) == 0
    keys = tasks.keys_for("S1")
    assert uploads[-3:] == [keys["alphagenome_tsv"], keys["alphagenome_tbi"], keys["alphagenome_vcf"]]  # VCF last
    with gzip.open(stage["root"] / keys["alphagenome_vcf"], "rt") as fh:
        records = [line for line in fh if not line.startswith("#")]
    assert [int(r.split("\t")[1]) for r in records] == [101, 102, 103, 104]  # top 4 by QUAL, in position order
    assert all("AG_SOURCE=fake" in r and "AG_AVI=" in r for r in records)
    tsv = (stage["root"] / keys["alphagenome_tsv"]).read_text().splitlines()
    assert tsv[0].startswith("#AlphaGenomeTerms=By using this information")
    assert tsv[1].split("\t") == list(ag.TSV_COLUMNS) and len(tsv) == 6

    part = provenance.read_part(stage["store"], "S1", "Annotate")
    assert part["status"] == "Success"
    summary = part["alphagenome"]
    assert summary["source"] == "fake" and summary["model_version"] == "fake-1" and summary["api_calls"] == 4
    assert summary["selection"]["candidates"] == 6 and summary["selection"]["dropped_by_cap"] == 2
    assert summary["scored"] == 4 and summary["not_scored"] == 0 and summary["input"] == keys["vcf"]
    assert {o["key"] for o in part["outputs"]} == {
        keys["alphagenome_tsv"],
        keys["alphagenome_tbi"],
        keys["alphagenome_vcf"],
    }
    record = provenance.read_record(stage["store"], "S1")
    assert record["status"] == "succeeded" and record["alphagenome"] == summary
    assert record["stages"]["Annotate"]["status"] == "Success"

    metrics = metric_lines(stage["root"])
    assert any(m["task"] == "Annotate" and m["status"] == "Success" for m in metrics)
    assert [m["count"] for m in metrics if m["task"] == "Annotate.ApiCalls"] == [4]
    assert "4 of 6 candidate variants selected" in capsys.readouterr().out


def test_annotate_prefers_the_csq_copy_and_uses_its_consequences(stage):
    stage["core_run"]()
    stage["put_vcf"]("variants/S1.vcf.gz", LINES)
    stage["put_vcf"](
        "variants/S1.csq.vcf.gz",
        [rec("chr20", 100, "A", "G", info="BCSQ=intron|G2|T2|protein_coding"), rec("chr20", 200, "C", "T")],
        csq=True,
    )
    assert tasks.main(["annotate", "S1", "chr20.fa", "--source", "fake"]) == 0
    summary = provenance.read_part(stage["store"], "S1", "Annotate")["alphagenome"]
    assert summary["input"] == "variants/S1.csq.vcf.gz" and summary["consequences"] is True
    assert summary["selection"]["near_gene"] == 1 and summary["scored"] == 1
    with gzip.open(stage["root"] / "variants" / "S1.alphagenome.vcf.gz", "rt") as fh:
        body = fh.read()
    assert "BCSQ=intron|G2|T2|protein_coding" in body and "AG_GENE=G2" in body


def test_annotate_with_the_avi_file(stage):
    stage["core_run"]()
    stage["put_vcf"]("variants/S1.vcf.gz", LINES)
    (stage["root"] / "reference" / "alphagenome").mkdir(parents=True)
    write_avi_table(stage["root"] / tasks.alphagenome_avi_key("chr20.fa"), [("chr20", 104, "A", "G", 2.0, 31.0)])
    assert tasks.main(["annotate", "S1", "chr20.fa", "--source", "avi-file"]) == 0
    part = provenance.read_part(stage["store"], "S1", "Annotate")
    assert part["alphagenome"]["scored"] == 1 and part["alphagenome"]["not_scored"] == 5  # the rest: not in the table
    assert part["alphagenome"]["api_calls"] == 0
    assert tasks.alphagenome_avi_key("chr20.fa") in {i["key"] for i in part["inputs"]}
    tsv = (stage["root"] / "stats" / "S1" / "alphagenome.tsv").read_text().splitlines()
    assert tsv[2].split("\t")[7:9] == ["31", "2"] and tsv[2].endswith("\tavi-file")


@pytest.mark.parametrize(
    "setup,source",
    [
        ("no_calls", "fake"),  # nothing to annotate
        ("calls", "api"),  # the API backend isn't wired in (and the key must not leak)
        ("calls", "avi-file"),  # no AVI table for this reference
    ],
)
def test_a_failed_annotation_is_noted_never_fatal(stage, capsys, monkeypatch, setup, source):
    stage["core_run"]()
    if setup == "calls":
        stage["put_vcf"]("variants/S1.vcf.gz", LINES)
    monkeypatch.setenv(ag.API_KEY_ENV, SECRET)
    assert tasks.main(["annotate", "S1", "chr20.fa", "--source", source]) == 0  # exit 0: noted, not fatal
    record = provenance.read_record(stage["store"], "S1")
    assert record["status"] == "succeeded" and record["complete"]  # the core run keeps its status
    assert record["stages"]["Annotate"]["status"] == "Failure" and record["stages"]["Annotate"]["error"]
    assert not (stage["root"] / "variants" / "S1.alphagenome.vcf.gz").exists()
    out = capsys.readouterr()
    assert "WARNING: annotation failed" in out.out
    everything = out.out + out.err + json.dumps(record) + json.dumps(metric_lines(stage["root"]))
    assert SECRET not in everything
    assert [m["count"] for m in metric_lines(stage["root"]) if m["task"] == "Annotate.ApiCalls"] == [0]


@pytest.mark.parametrize(
    "argv,env",
    [
        (["annotate", "S1", "chr20.fa"], {}),  # no --source and no $ALPHAGENOME_SOURCE: never a silent fake
        (["annotate", "S1", "chr20.fa", "--source", "gnomad"], {}),
        (["annotate", "S1", "chr20.fa", "--source", "fake", "--max-variants", "0"], {}),
        (["annotate", "S1", "chr20.fa", "--source", "fake"], {ag.MAX_VARIANTS_ENV: "lots"}),
        (["annotate", "S1"], {}),
    ],
)
def test_usage_errors_exit_2_before_anything_runs(stage, monkeypatch, argv, env):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit) as err:
        tasks.main(argv)
    assert err.value.code == 2
    assert provenance.read_part(stage["store"], "S1", "Annotate") is None and not metric_lines(stage["root"])


def test_the_source_can_come_from_the_environment(stage, monkeypatch):
    stage["put_vcf"]("variants/S1.vcf.gz", LINES)
    monkeypatch.setenv("ALPHAGENOME_SOURCE", "fake")
    assert tasks.main(["annotate", "S1", "chr20.fa"]) == 0
    assert provenance.read_part(stage["store"], "S1", "Annotate")["alphagenome"]["source"] == "fake"


def test_a_failed_annotate_part_does_not_fail_a_closed_run():
    assert "Annotate" in provenance.EXTRA_STAGES
    assert set(provenance.UPSTREAM["Annotate"]) >= set(provenance.CORE_STAGES) | {"QualityGate"}


# --- run_local --------------------------------------------------------------------------------------------


def test_run_local_knows_the_same_sources():
    assert run_local.ANNOTATE_SOURCES == ag.SOURCES


def test_run_local_adds_an_optional_annotate_job_last():
    jobs = run_local.pipeline_jobs("S1", "ref.fa", gate=True, annotate="alphagenome")
    assert [j["name"] for j in jobs][-2:] == ["gate", "annotate"]
    assert jobs[-1]["cmd"] == ["python", "tasks.py", "annotate", "S1", "ref.fa", "--source", "fake"]
    assert jobs[-1]["optional"] is True
    assert "annotate" not in [j["name"] for j in run_local.pipeline_jobs("S1", "ref.fa")]
    jobs = run_local.pipeline_jobs("S1", "ref.fa", annotate="alphagenome", annotate_source="avi-file")
    assert jobs[-1]["cmd"][-1] == "avi-file"


def test_run_local_passes_the_key_by_name_only(monkeypatch, tmp_path):
    monkeypatch.setenv(ag.API_KEY_ENV, SECRET)
    monkeypatch.setenv(ag.MAX_VARIANTS_ENV, "50")
    ran = []
    monkeypatch.setattr(
        run_local.subprocess, "run", lambda cmd, **k: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0)
    )
    job = run_local.pipeline_jobs("S1", "ref.fa", annotate="alphagenome", annotate_source="api")[-1]
    assert run_local.run_job(job, "docker", str(tmp_path)) == 0
    (cmd,) = ran
    assert SECRET not in " ".join(cmd)
    assert cmd[cmd.index(ag.API_KEY_ENV) - 1] == "-e"  # docker copies it from the environment
    assert f"{ag.MAX_VARIANTS_ENV}=50" in cmd


def test_run_local_carries_on_after_a_failed_optional_job(tmp_path, monkeypatch, capsys):
    (tmp_path / "raw_reads").mkdir()
    (tmp_path / "raw_reads" / "S1.fastq.gz").write_bytes(b"x")
    (tmp_path / "reference").mkdir()
    (tmp_path / "reference" / "ref.fa").write_text(">r\nA\n")
    ran = []
    monkeypatch.setattr(
        run_local, "run_job", lambda job, *a, **k: ran.append(job["name"]) or (1 if job["name"] == "annotate" else 0)
    )
    args = ["--root", str(tmp_path), "--sample", "S1", "--reference", "ref.fa", "--annotate", "alphagenome"]
    assert run_local.main(args) == 0
    assert ran[-1] == "annotate" and "optional stage 'annotate' failed" in capsys.readouterr().out


def test_without_bcftools_the_table_and_the_record_are_still_written(stage, monkeypatch, capsys):
    # A run outside the image (T1's native run): scoring works, only the bgzipped VCF needs htslib.
    stage["core_run"]()
    stage["put_vcf"]("variants/S1.vcf.gz", LINES)
    monkeypatch.setattr(tasks.shutil, "which", lambda name: None)
    assert tasks.main(["annotate", "S1", "chr20.fa", "--source", "fake"]) == 0  # noted, never fatal
    assert "bcftools/bgzip not found: run inside the image or install htslib" in capsys.readouterr().out
    assert not stage["commands"]  # bcftools never tried
    keys = tasks.keys_for("S1")
    tsv = (stage["root"] / keys["alphagenome_tsv"]).read_text().splitlines()
    assert len(tsv) == 8  # the terms line, the header and all 6 variants
    assert not (stage["root"] / keys["alphagenome_vcf"]).exists()
    part = provenance.read_part(stage["store"], "S1", "Annotate")
    assert part["status"] == "Failure" and "bcftools/bgzip not found" in part["error"]
    summary = part["alphagenome"]
    assert summary["scored"] == 6 and summary["vcf"] == "failed: bcftools not found"
    assert summary["not_scored_by_kind"] == {"snv": 0, "indel": 0}
    assert keys["alphagenome_tsv"] in {o["key"] for o in part["outputs"]}
    assert provenance.read_record(stage["store"], "S1")["status"] == "succeeded"


def test_the_record_says_which_kinds_went_unscored(stage):
    # avi-file takes SNVs only: the indel and the SNVs missing from the table show up by kind.
    stage["core_run"]()
    stage["put_vcf"]("variants/S1.vcf.gz", LINES)
    (stage["root"] / "reference" / "alphagenome").mkdir(parents=True)
    write_avi_table(stage["root"] / tasks.alphagenome_avi_key("chr20.fa"), [("chr20", 104, "A", "G", 2.0, 31.0)])
    assert tasks.main(["annotate", "S1", "chr20.fa", "--source", "avi-file"]) == 0
    summary = provenance.read_part(stage["store"], "S1", "Annotate")["alphagenome"]
    assert summary["not_scored_by_kind"] == {"snv": 4, "indel": 1} and summary["vcf"] == "written"
