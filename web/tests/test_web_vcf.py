from samples.vcf import check_truth, parse_vcf, read_variants

from .web_helpers import put, vcf_gz


def test_parse_vcf_rows_and_fields(storage):
    put(storage, "variants/S1.vcf.gz", vcf_gz([(100, "A", "G", 200.0, 31, "1/1"), (250, "AT", "A", ".", 12, "0/1")]))
    table = read_variants(storage, "variants/S1.vcf.gz")
    assert table.total == 2
    snp, indel = table.rows
    assert (snp.chrom, snp.pos, snp.ref, snp.alt, snp.qual, snp.depth, snp.genotype, snp.kind) == (
        "synth1",
        100,
        "A",
        "G",
        200.0,
        31,
        "1/1",
        "SNP",
    )
    assert (indel.qual, indel.kind, indel.genotype) == (None, "Indel", "0/1")
    assert table.calls == {(100, "A", "G"), (250, "AT", "A")}


def test_multiallelic_records_and_row_cap():
    lines = ["#header\n", "c\t5\t.\tA\tC,T\t50\t.\tDP=9\n"] + [f"c\t{p}\t.\tG\tA\t50\t.\tDP=9\n" for p in range(10, 20)]
    table = parse_vcf(lines, limit=3)
    assert table.total == 12
    assert len(table.rows) == 3 and table.truncated
    assert (5, "A", "C") in table.calls and (5, "A", "T") in table.calls and (19, "G", "A") in table.calls


def test_missing_vcf_is_none(storage):
    assert read_variants(storage, "variants/NOPE.vcf.gz") is None


def test_truth_check_matches_run_local_semantics():
    truth = {"snps": [{"pos": 100, "ref": "A", "alt": "G"}, {"pos": 200, "ref": "C", "alt": "T"}]}
    result = check_truth({(100, "A", "G"), (300, "G", "A")}, truth)
    assert (result.total, result.recovered) == (2, 1)
    assert result.missed == [(200, "C", "T")] and result.extra == [(300, "G", "A")]
    assert not result.perfect
    assert check_truth({(100, "A", "G"), (200, "C", "T")}, truth).perfect


def test_each_allele_shows_its_own_consequence():
    from samples.vcf import parse_vcf

    lines = [
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO",
        "c\t100\t.\tA\tG,T\t50\t.\tBCSQ=missense|G1|T1|protein_coding|+|5N>5S|100A>G,"
        "synonymous|G1|T1|protein_coding|+|5N|100A>T",
    ]
    rows = parse_vcf(lines).rows
    assert [(v.alt, v.consequence) for v in rows] == [("G", "missense G1"), ("T", "synonymous G1")]


def test_filtered_records_and_placeholder_alts_are_not_calls():
    # DeepVariant writes its rejected candidates as FILTER=RefCall, and NoCall rows; bcftools leaves FILTER '.'.
    lines = [
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS1",
        "c\t100\t.\tA\tG\t50\tPASS\t.\tGT\t0/1",
        "c\t200\t.\tC\tT\t3\tRefCall\t.\tGT\t0/0",
        "c\t300\t.\tG\tA\t40\t.\t.\tGT\t1/1",
        "c\t400\t.\tT\t.\t0\tNoCall\t.\tGT\t./.",
        "c\t500\t.\tA\t<*>\t0\t.\t.\tGT\t0/0",
    ]
    table = parse_vcf(lines)
    assert table.calls == {(100, "A", "G"), (300, "G", "A")} and table.total == 2
    assert [r.pos for r in table.rows] == [100, 300]
