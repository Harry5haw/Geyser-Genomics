"""scripts/eval_alphagenome.py: AUROC, bootstrap, matching, inputs and the report, on synthetic fixtures only."""

import gzip
import json
import random

import pytest

import eval_alphagenome as ev


def write_tsv(path, header, rows):
    text = "\t".join(header) + "\n" + "".join("\t".join(str(x) for x in r) + "\n" for r in rows)
    if str(path).endswith(".gz"):
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            fh.write(text)
    else:
        path.write_text(text, encoding="utf-8")
    return str(path)


# --- AUROC and bootstrap -------------------------------------------------------------------------------------------


def test_auroc_perfect_reversed_and_chance():
    assert ev.auroc([3, 4, 5], [0, 1, 2]) == 1.0
    assert ev.auroc([0, 1, 2], [3, 4, 5]) == 0.0
    assert ev.auroc([1, 1], [1, 1]) == 0.5  # all ties count half


def test_auroc_matches_pairwise_definition_with_ties():
    rng = random.Random(1)
    pos = [rng.choice([0.0, 0.5, 1.0, 1.5]) for _ in range(30)]
    neg = [rng.choice([0.0, 0.5, 1.0]) for _ in range(40)]
    pairwise = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg))
    assert ev.auroc(pos, neg) == pytest.approx(pairwise)


def test_auroc_empty_group_is_none():
    assert ev.auroc([], [1]) is None
    assert ev.auroc([1], []) is None


def test_bootstrap_ci_is_seeded_and_brackets_the_estimate():
    rng = random.Random(2)
    pos = [rng.gauss(1, 1) for _ in range(50)]
    neg = [rng.gauss(0, 1) for _ in range(200)]
    ci = ev.bootstrap_ci(pos, neg, 300, seed=7)
    assert ci == ev.bootstrap_ci(pos, neg, 300, seed=7)
    assert ci[0] <= ev.auroc(pos, neg) <= ci[1]
    assert ev.bootstrap_ci(pos, [], 300, seed=7) is None


# --- Parsing -------------------------------------------------------------------------------------------------------


def test_chromosome_names_and_variant_keys_normalise():
    assert ev.norm_chrom("20") == ev.norm_chrom("chr20") == ev.norm_chrom("CHR20") == "chr20"
    assert ev.variant_key("20", "100", "a", "g") == ("chr20", 100, "A", "G")


def test_score_specs_and_flags():
    assert ev.parse_score_spec("AG_RNA_LFC:abs") == ("AG_RNA_LFC", True)
    assert ev.parse_score_spec("AG_AVI") == ("AG_AVI", False)
    with pytest.raises(ValueError):
        ev.parse_score_spec("AG_AVI:log")
    assert ev.parse_flag("AG_AVI_PHRED>=20") == ("AG_AVI_PHRED", 20.0, False)
    assert ev.parse_flag("AG_RNA_LFC:abs>=0.5") == ("AG_RNA_LFC", 0.5, True)
    with pytest.raises(ValueError):
        ev.parse_flag("AG_AVI_PHRED>20")


def test_bcsq_consequences():
    assert ev.bcsq_consequence("missense|DEFB125|ENST00000382410|protein_coding|+|10I>10T|1234T>C") == "missense"
    assert ev.bcsq_consequence("@12345") == "missense"  # compound, recorded at another site
    assert ev.bcsq_consequence("*synonymous|G|T|protein_coding") == "synonymous"
    assert ev.bcsq_consequence("") is None and ev.bcsq_consequence(".") is None
    assert ev.is_protein_level("missense") and ev.is_protein_level("splice_donor&intron")
    assert not ev.is_protein_level("synonymous") and not ev.is_protein_level("3_prime_utr")
    assert not ev.is_protein_level(None)


def test_read_eqtls_both_layouts_keeps_max_pip_and_genes(tmp_path):
    path = write_tsv(
        tmp_path / "cs.tsv.gz",
        ["molecular_trait_id", "variant", "pip"],
        [["ENSG01.5", "chr20_100_A_G", 0.4], ["ENSG02", "chr20_100_A_G", 0.95], ["ENSG03", "chr20_200_C_T", 0.1]],
    )
    eqtls = ev.read_eqtls(path)
    assert eqtls[("chr20", 100, "A", "G")] == (0.95, {"ENSG01", "ENSG02"})
    path2 = write_tsv(
        tmp_path / "cs2.tsv", ["chromosome", "position", "ref", "alt", "pip"], [["20", 300, "G", "A", 0.99]]
    )
    assert ev.read_eqtls(path2) == {("chr20", 300, "G", "A"): (0.99, set())}


def test_read_vcf_pass_only_multiallelic_and_bcsq(tmp_path):
    vcf = tmp_path / "a.vcf"
    vcf.write_text(
        "##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        "chr20\t100\t.\tA\tG,T\t50\tPASS\tAG_AVI=1.5,2.5;AG_GENE=ENSG01;BCSQ=missense|G|T|protein_coding\n"
        "chr20\t200\t.\tC\tT\t50\tLowQual\tAG_AVI=3\n"
        "chr20\t300\t.\tG\tA\t50\t.\tAG_AVI=.\n",
        encoding="utf-8",
    )
    rows = ev.read_annotated(str(vcf), ev.DEFAULTS["vcf"]["columns"], ["AG_AVI"])
    assert [r["key"][1:] for r in rows] == [(100, "A", "G"), (100, "A", "T"), (300, "G", "A")]
    assert [r["scores"]["AG_AVI"] for r in rows] == [1.5, 2.5, None]
    assert rows[0]["csq"] == "missense" and rows[0]["gene"] == "ENSG01"
    assert rows[2]["csq"] is None


def test_tsv_missing_core_column_is_an_error(tmp_path):
    path = write_tsv(tmp_path / "a.tsv", ["chrom", "pos", "ref"], [["chr20", 1, "A"]])
    with pytest.raises(ValueError, match="alt"):
        ev.read_annotated(path, ev.DEFAULTS["tsv"]["columns"], ["avi"])


def test_vcf_defaults_follow_the_contract(synthetic, tmp_path):
    vcf = tmp_path / "S1.alphagenome.vcf"
    vcf.write_text(
        "##fileformat=VCFv4.2\n##alphagenome_terms=research use only\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        + "".join(
            f"chr20\t{5_000 + 3_000 * i}\t.\tA\tG\t50\tPASS\tAG_AVI={10 + i % 7};AG_RNA_LFC={1.0 if i < 30 else 0.01}"
            f";AG_GENE=ENSG{i:05d};AG_SOURCE=atlas\n"
            for i in range(300)
        ),
        encoding="utf-8",
    )
    out = tmp_path / "v.json"
    assert (
        ev.main(["--annotated", str(vcf), "--eqtl", synthetic["eqtl"][0], "--bootstrap", "50", "--out-json", str(out)])
        == 0
    )
    result = json.loads(out.read_text("utf-8"))
    assert [s["score"] for s in result["scores"]] == ["AG_RNA_LFC:abs", "AG_AVI"]
    assert result["overlap"]["flag"] == "AG_AVI>=20" and result["counts"]["positives"] == 30
    assert result["scores"][0]["auroc"] == 1.0


def test_tss_distance_and_bins():
    tss = {"chr20": [1_000, 50_000]}
    assert ev.tss_distance(tss, ("chr20", 1_500, "A", "G")) == 500
    assert ev.tss_distance(tss, ("chr20", 49_000, "A", "G")) == 1_000
    assert ev.tss_distance(tss, ("chr21", 10, "A", "G")) is None
    assert ev.bin_of(500, ev.TSS_BINS) == 0 and ev.bin_of(600_000, ev.TSS_BINS) == len(ev.TSS_BINS)
    assert ev.bin_of(None, ev.AF_BINS) is None


# --- Matching ------------------------------------------------------------------------------------------------------


def test_matching_draws_within_stratum_without_replacement():
    def v(pos, s):
        return {"key": ("chr20", pos, "A", "G"), "s": s}

    positives = [v(1, "a"), v(2, "a"), v(3, "b")]
    candidates = [v(10 + i, "a") for i in range(7)] + [v(30, "b")]
    controls, short = ev.match_controls(positives, candidates, lambda x: x["s"], per_case=3, seed=1)
    assert sum(c["s"] == "a" for c in controls) == 6 and sum(c["s"] == "b" for c in controls) == 1
    assert len({c["key"] for c in controls}) == len(controls)  # no control used twice
    assert short == 1  # the 'b' positive got only one
    again, _ = ev.match_controls(positives, candidates, lambda x: x["s"], per_case=3, seed=1)
    assert [c["key"] for c in again] == [c["key"] for c in controls]


# --- End to end on a synthetic sample ------------------------------------------------------------------------------


HEADER = [
    "rank",
    "chrom",
    "pos",
    "ref",
    "alt",
    "kind",
    "qual",
    "avi",
    "avi_raw",
    "top_track",
    "gene",
    "rna_lfc",
    "csq",
    "source",
]  # the annotate TSV's columns (#207)


def phred_of(raw):
    """A monotone stand-in for AVI's PHRED scale (higher raw, higher PHRED)."""
    return round(max(0.0, (raw + 1.3) * 8), 2)


@pytest.fixture
def synthetic(tmp_path):
    """60 fine-mapped eQTL calls with high |LFC| (and common AF), 600 non-coding non-eQTL calls, 20 missense calls,
    written as the annotate stage's TSV (#207), terms-notice line first. AVI is made to track rarity (low for common
    variants), as the real score does, so it should NOT separate the eQTLs once AF is matched."""
    rng = random.Random(3)
    tss = [["chr20", 10_000 * i, f"G{i}"] for i in range(1, 200)]
    ann, eqtl_a, eqtl_b, af = [], [], [], []
    noncoding_flagged = 0
    for i in range(660):
        pos = 5_000 + 3_000 * i
        is_eqtl = i < 60
        freq = rng.uniform(0.05, 0.5) if is_eqtl or i % 2 == 0 else rng.uniform(0.0005, 0.04)
        lfc = rng.gauss(0.8, 0.2) * rng.choice([-1, 1]) if is_eqtl else rng.gauss(0, 0.2)
        raw = round(-1.0 + 2.0 * (0.5 - freq) + rng.gauss(0, 0.3), 4)
        noncoding_flagged += phred_of(raw) >= 20
        gene = f"ENSG{i:05d}" if is_eqtl else ""
        ann.append(
            [
                0,
                "chr20",
                pos,
                "A",
                "G",
                "snv",
                50,
                phred_of(raw),
                raw,
                "RNA_SEQ:liver",
                gene,
                round(lfc, 4),
                "",
                "atlas",
            ]
        )
        af.append(["chr20", pos, "A", "G", round(freq, 4)])
        if is_eqtl:
            (eqtl_a if i < 30 else eqtl_b).append([f"ENSG{i:05d}.1", f"chr20_{pos}_A_G", 0.95])
    for j in range(20):
        raw = 4.0 if j < 5 else 0.0
        ann.append(
            [
                0,
                "chr20",
                3_000_000 + 1_000 * j,
                "C",
                "T",
                "snv",
                50,
                phred_of(raw),
                raw,
                "",
                "",
                0.0,
                "missense,intron",
                "atlas",
            ]
        )
    eqtl_b.append(["ENSG99999", "chr20_6000000_T_C", 0.1])  # in a credible set, not a positive, not called
    annotated = tmp_path / "alphagenome.tsv"
    write_tsv(annotated, HEADER, ann)
    annotated.write_text(
        "# AlphaGenome outputs: research use only, not for clinical decisions\n" + annotated.read_text("utf-8"), "utf-8"
    )
    paths = {
        "annotated": str(annotated),
        "eqtl": [
            write_tsv(tmp_path / "a.cs.tsv.gz", ["molecular_trait_id", "variant", "pip"], eqtl_a),
            write_tsv(tmp_path / "b.cs.tsv", ["molecular_trait_id", "variant", "pip"], eqtl_b),
        ],
        "tss": write_tsv(tmp_path / "tss.tsv", ["chrom", "pos", "gene"], tss),
        "af": write_tsv(tmp_path / "af.tsv", ["chrom", "pos", "ref", "alt", "af"], af),
        "noncoding_flagged": noncoding_flagged,
    }
    rr = tmp_path / "rr.json"
    rr.write_text(json.dumps({"stage": "Annotate", "source": "atlas", "api_calls": 12, "seconds": 41.5}), "utf-8")
    paths["run_record"] = str(rr)
    return paths


def run(paths, tmp_path, *extra):
    out_json, out_md = tmp_path / "e.json", tmp_path / "e.md"
    argv = [
        "--annotated",
        paths["annotated"],
        "--eqtl",
        paths["eqtl"][0],
        "--eqtl",
        paths["eqtl"][1],
        "--out-json",
        str(out_json),
        "--out-md",
        str(out_md),
        "--bootstrap",
        "200",
        *extra,
    ]
    assert ev.main(argv) == 0
    return json.loads(out_json.read_text("utf-8")), out_md.read_text("utf-8")


def test_end_to_end_matched_on_tss_and_af(synthetic, tmp_path):
    result, md = run(
        synthetic, tmp_path, "--tss", synthetic["tss"], "--af", synthetic["af"], "--run-record", synthetic["run_record"]
    )
    assert result["matched_on"] == ["distance to nearest TSS", "allele frequency"] and result["af_matched"]
    assert result["counts"]["positives"] == 60
    primary, avi = result["scores"]
    assert primary["score"] == "rna_lfc:abs" and primary["role"] == "primary"
    assert primary["auroc"] > 0.95 and primary["ci95"][0] > 0.9
    assert avi["score"] == "avi" and 0.25 < avi["auroc"] < 0.75  # AF matched: no frequency signal left
    assert "matched on allele frequency" in avi["caveat"]
    assert result["gene_agreement"] == {
        "positives_with_ag_gene": 60,
        "ag_gene_resolved_to_ensembl": 60,
        "ag_gene_is_eqtl_gene": 60,
    }
    o = result["overlap"]
    assert o["flag"] == "avi>=20" and (o["csq_and_ag"], o["csq_only"]) == (5, 15)
    assert o["ag_only"] == synthetic["noncoding_flagged"] and o["ag_only"] + o["neither"] == 660
    assert result["coverage"]["by_source"] == {"atlas": 680}
    assert result["run_record"] == {"source": "atlas", "api_calls": 12, "seconds": 41.5}
    assert "| `rna_lfc:abs` | primary |" in md and "research use only" in md


def test_without_af_the_report_says_so_and_avi_shows_the_confound(synthetic, tmp_path):
    result, md = run(synthetic, tmp_path, "--tss", synthetic["tss"])
    assert result["matched_on"] == ["distance to nearest TSS"] and not result["af_matched"]
    assert "**Not matched on allele frequency.**" in md
    avi = result["scores"][1]
    assert avi["auroc"] < 0.3  # common eQTLs vs mostly-rare controls: AVI ranks them low, the confound
    assert avi["caveat"] == ev.AVI_CONFOUND and "Caveat:" in md


def test_nothing_to_match_on_and_custom_scores(synthetic, tmp_path):
    result, _ = run(synthetic, tmp_path, "--score", "avi_raw", "--controls-per-case", "2")
    assert result["matched_on"] == ["nothing (no TSS table, no allele frequencies)"]
    assert [s["score"] for s in result["scores"]] == ["avi_raw"] and result["scores"][0]["role"] == "primary"
    assert "caveat" in result["scores"][0]  # any AVI score carries the confound note
    assert result["counts"]["controls"] == 120


def test_bad_column_mapping_fails_cleanly(synthetic, tmp_path, capsys):
    assert ev.main(["--annotated", synthetic["annotated"], "--eqtl", synthetic["eqtl"][0], "--col", "nope=X"]) == 1
    assert "--col needs ROLE=NAME" in capsys.readouterr().err


def test_gene_names_resolve_to_ensembl_ids(tmp_path):
    names = write_tsv(tmp_path / "meta.tsv.gz", ["gene_id", "gene_name"], [["ENSG01.3", "PARAL1"], ["ENSG02", "ABC"]])
    table = ev.read_gene_names(names)
    assert table == {"PARAL1": {"ENSG01"}, "ABC": {"ENSG02"}}
    assert ev.gene_ids("PARAL1", table) == {"ENSG01"}
    assert ev.gene_ids("ENSG07.2", table) == {"ENSG07"}
    assert ev.gene_ids("UNKNOWN", table) == set()
