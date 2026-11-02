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
