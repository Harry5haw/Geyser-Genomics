"""scripts/cohort_qc.py: Hail cohort QC on tiny synthetic VCFs (never real data).

The pure-Python helpers run everywhere. The Hail tests need requirements-cohort.txt and a JVM (Linux/macOS: Hail has
no Windows build); without them they skip, so the main unit job is unaffected. CI runs them in the "cohort" job, and
locally: docker build -f app-cohort/Dockerfile -t geyser-cohort:local . and docker run --rm -v "$PWD:/work"
geyser-cohort:local python -m pytest tests/test_cohort_qc.py tests/test_cohort_runs.py.

Sample S1 has all three callers, S2 only bcftools. Expected, PASS calls only (by hand):

    pos  variant    bcftools  deepvariant      pangenome
    100  A>G  ti    0/1       0/1              0/1
    200  C>A  tv    1/1       1/1              -
    300  G>GA indel 0/1       -                -
    400  T>C,TA     1/2       -                -          split: T>C (ti) and T>TA (indel), each het
    500  A>T        -         0/0 RefCall      -          not PASS: dropped
    600  G>C        -         ./. NoCall       -          not PASS: dropped
    700  C>T  ti    -         0/1              0/1
    800  A>C  tv    -         -                1/1
"""

import argparse
import os
import shutil

import pytest

import cohort_qc

HEADER = """##fileformat=VCFv4.2
##contig=<ID=chr20,length=64444167>
##FILTER=<ID=PASS,Description="All filters passed">
##FILTER=<ID=RefCall,Description="Genotyping model thinks this site is reference.">
##FILTER=<ID=NoCall,Description="Site has depth=0 resulting in no call.">
##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">
##FORMAT=<ID=GQ,Number=1,Type=Integer,Description="Genotype quality">
##FORMAT=<ID=DP,Number=1,Type=Integer,Description="Read depth">
##FORMAT=<ID=AD,Number=R,Type=Integer,Description="Allelic depths">
##FORMAT=<ID=PL,Number=G,Type=Integer,Description="Phred-scaled genotype likelihoods">
"""


def vcf_text(sample_name, records, dv=False):
    """records: (pos, ref, alt, filter, gt). bcftools-style entries carry GT:PL:AD (no DP), DeepVariant-style
    GT:GQ:DP:AD:PL, like the pipeline's real outputs."""
    lines = [HEADER.rstrip("\n"), "\t".join(["#CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER", "INFO", "FORMAT"])]
    lines[-1] += "\t" + sample_name
    for pos, ref, alt, filt, gt in records:
        n_alleles = 1 + len(alt.split(","))
        ad = ",".join(["10"] * n_alleles)
        pl = ",".join(["0"] * (n_alleles * (n_alleles + 1) // 2))
        entry = f"{gt}:30:{10 * n_alleles}:{ad}:{pl}" if dv else f"{gt}:{pl}:{ad}"
        fmt = "GT:GQ:DP:AD:PL" if dv else "GT:PL:AD"
        lines.append("\t".join(["chr20", str(pos), ".", ref, alt, "50", filt, ".", fmt, entry]))
    return "\n".join(lines) + "\n"


S1 = {
    "bcftools": [
        (100, "A", "G", ".", "0/1"),
        (200, "C", "A", ".", "1/1"),
        (300, "G", "GA", ".", "0/1"),
        (400, "T", "C,TA", ".", "1/2"),
    ],
    "deepvariant": [
        (100, "A", "G", "PASS", "0/1"),
        (200, "C", "A", "PASS", "1/1"),
        (500, "A", "T", "RefCall", "0/0"),
        (600, "G", "C", "NoCall", "./."),
        (700, "C", "T", "PASS", "0/1"),
    ],
    "pangenome": [
        (100, "A", "G", "PASS", "0/1"),
        (700, "C", "T", "PASS", "0/1"),
        (800, "A", "C", "PASS", "1/1"),
    ],
}
# S2's 900: a multi-allelic SNP record and a separate indel record at the same position, as bcftools writes them
# (chr20:136,668 in the real HG003 calls). Split, the keys come out of order (G>C, G>T, then G>GA), which Hail
# refuses unless the split may shuffle.
S2 = {
    "bcftools": [
        (100, "A", "G", ".", "0/1"),
        (900, "G", "C,T", ".", "1/2"),
        (900, "G", "GA", ".", "0/1"),
    ]
}


def write_storage(root):
    """The pipeline's layout: variants/{id}.vcf.gz, variants_deepvariant/..., variants_pangenome/... The files are
    plain text (Hail reads them as such when the name doesn't end in .gz), so they're written as .vcf and linked
    through --vcf in the Hail tests; discover() is tested on empty .vcf.gz placeholders."""
    paths = []
    for sample, callers in (("S1", S1), ("S2", S2)):
        for caller, records in callers.items():
            folder = root / cohort_qc.CALLER_PREFIXES[caller]
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{sample}.vcf"
            # Each VCF names its sample differently, as the real ones do (HG003 vs HG003_chr20).
            name = sample if caller == "bcftools" else f"{sample}_chr20"
            path.write_text(vcf_text(name, records, dv=caller != "bcftools"), newline="\n")
            paths.append(cohort_qc.CallSet(sample, caller, str(path)))
    return paths


# --- pure Python: run everywhere -----------------------------------------------------------------------------------


def test_parse_vcf_arg_keeps_colons_in_the_path():
    cs = cohort_qc.parse_vcf_arg("HG003:deepvariant:s3://bucket/variants_deepvariant/HG003.vcf.gz")
    assert cs == cohort_qc.CallSet("HG003", "deepvariant", "s3://bucket/variants_deepvariant/HG003.vcf.gz")
    assert cs.column == "HG003/deepvariant"
    with pytest.raises(argparse.ArgumentTypeError):
        cohort_qc.parse_vcf_arg("HG003:/data/x.vcf.gz")


def test_discover_uses_the_pipeline_keys_and_skips_missing(tmp_path, capsys):
    for prefix in ("variants", "variants_pangenome"):
        (tmp_path / prefix).mkdir()
        (tmp_path / prefix / "S1.vcf.gz").write_bytes(b"")
    found = cohort_qc.discover(str(tmp_path), ["S1"])
    assert [(c.sample, c.caller) for c in found] == [("S1", "bcftools"), ("S1", "pangenome")]
    assert found[0].path == os.path.join(str(tmp_path), "variants", "S1.vcf.gz")
    assert "no deepvariant calls for S1" in capsys.readouterr().err


def test_caller_prefixes_match_the_pipeline():
    """The keys the pipeline writes each caller's VCF to (app/provenance.py STAGE_VCF)."""
    import provenance

    stage_for = {"bcftools": "CallVariants", "deepvariant": "DeepVariant", "pangenome": "PangenomeDeepVariant"}
    for caller, prefix in cohort_qc.CALLER_PREFIXES.items():
        assert provenance.STAGE_VCF[stage_for[caller]] == prefix + "/{id}.vcf.gz"


def test_markdown_table_leaves_missing_values_blank():
    table = cohort_qc.markdown_table([{"a": 1, "b": None}], ["a", "b"])
    assert table.splitlines() == ["| a | b |", "|---|---|", "| 1 |  |"]


def test_comparison_rows_counts_unique_and_shared():
    pd = pytest.importorskip("pandas")
    counts = {"pass_calls": 2, "n_het": 1, "n_hom_var": 1, "snps": 2, "indels": 0, "transitions": 2}
    per_call = pd.DataFrame(
        [{"sample": "S1", "caller": c, **counts, "transversions": 0} for c in ("bcftools", "deepvariant")]
    )
    overlap = pd.DataFrame(
        [
            {"sample": "S1", "callers": "bcftools+deepvariant", "variant_type": "SNP", "n": 1},
            {"sample": "S1", "callers": "bcftools", "variant_type": "SNP", "n": 1},
            {"sample": "S1", "callers": "deepvariant", "variant_type": "indel", "n": 1},
        ]
    )
    rows = {r["caller"]: r for r in cohort_qc.comparison_rows(per_call, overlap)}
    assert rows["bcftools"]["unique_to_caller"] == 1
    assert rows["deepvariant"]["unique_to_caller"] == 1
    assert rows["bcftools"]["shared_by_all_callers"] == 1
    assert rows["bcftools"]["ti_tv"] is None  # no transversions: a blank, not a division by zero
    assert rows["bcftools"]["het_hom"] == 1.0


# --- Hail: skip without it -----------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def hail(tmp_path_factory):
    hl = pytest.importorskip("hail")
    if not (shutil.which("java") or os.environ.get("JAVA_HOME")):
        pytest.skip("Hail needs a JVM (Java 11)")
    if os.name == "nt":
        pytest.skip("Hail has no Windows build: run in app-cohort/Dockerfile")
    tmp = str(tmp_path_factory.mktemp("hail"))
    from pyspark import SparkContext

    # Share a SparkContext with test_cohort_runs.py if it started one first (one per process).
    hl.init(
        sc=SparkContext._active_spark_context,
        master=None if SparkContext._active_spark_context else "local[2]",
        quiet=True,
        tmp_dir=tmp,
        local_tmpdir=tmp,
        log=os.path.join(tmp, "hail.log"),
        idempotent=True,
    )
    return hl


@pytest.fixture(scope="module")
def result(hail, tmp_path_factory):
    root = tmp_path_factory.mktemp("storage")
    out = tmp_path_factory.mktemp("out")
    call_sets = write_storage(root)
    rows = cohort_qc.run(call_sets, str(out), "GRCh38")
    return out, {(r["sample"], r["caller"]): r for r in rows}


def test_pass_only_and_split_multiallelics(result):
    _, rows = result
    bcf, dv, pg = rows["S1", "bcftools"], rows["S1", "deepvariant"], rows["S1", "pangenome"]
    # bcftools: 100, 200, 300, and 400 split into two het calls.
    assert (bcf["pass_calls"], bcf["snps"], bcf["indels"]) == (5, 3, 2)
    # DeepVariant: the RefCall (500) and NoCall (600) candidates don't count.
    assert (dv["pass_calls"], dv["snps"], dv["indels"]) == (3, 3, 0)
    assert (pg["pass_calls"], pg["snps"]) == (3, 3)


def test_titv_and_het_hom_count_sites(result):
    """Per site, like bcftools stats: the hom-var C>A at 200 is one transversion, not two (hl.sample_qc's count)."""
    _, rows = result
    assert rows["S1", "bcftools"]["ti_tv"] == 2.0  # A>G, T>C vs C>A
    assert rows["S1", "bcftools"]["het_hom"] == 4.0
    assert rows["S1", "deepvariant"]["ti_tv"] == 2.0  # A>G, C>T vs C>A
    assert rows["S1", "pangenome"]["het_hom"] == 2.0


def test_unique_and_shared_calls(result):
    _, rows = result
    assert rows["S1", "bcftools"]["unique_to_caller"] == 3  # 300, 400 T>C, 400 T>TA
    assert rows["S1", "deepvariant"]["unique_to_caller"] == 0
    assert rows["S1", "pangenome"]["unique_to_caller"] == 1  # 800
    assert rows["S1", "bcftools"]["shared_by_all_callers"] == 1  # 100
    # One caller only: "shared by all" means nothing, so it's blank; all its calls are unique.
    assert rows["S2", "bcftools"]["shared_by_all_callers"] is None
    assert rows["S2", "bcftools"]["unique_to_caller"] == 4  # 100, and 900's G>C, G>T, G>GA


def test_outputs_are_written(result):
    import csv

    out, _ = result
    for name in ("sample_qc.parquet", "caller_overlap.parquet", "variants.parquet"):
        assert any(f.endswith(".parquet") for f in os.listdir(out / name)), name
    with open(out / "caller_comparison.csv") as handle:
        assert len(list(csv.DictReader(handle))) == 4  # S1 x 3 callers + S2 x 1
    summary = (out / "summary.md").read_text()
    assert "2 samples, 4 call sets, PASS calls only." in summary
    assert "| S1 | SNP | bcftools+deepvariant+pangenome | 1 |" in summary


def test_variant_table_counts_samples_per_caller(hail, result):
    from pyspark.sql import SparkSession

    out, _ = result
    spark = SparkSession.builder.getOrCreate()
    variants = spark.read.parquet(str(out / "variants.parquet"))
    site = variants.where("`locus.position` = 100").collect()
    assert len(site) == 1
    row = site[0].asDict()
    assert (row["n_samples_bcftools"], row["n_samples_deepvariant"], row["n_samples_pangenome"]) == (2, 1, 1)
    assert row["variant_type"] == "SNP"
    # The RefCall and NoCall candidates never become rows.
    assert variants.where("`locus.position` IN (500, 600)").count() == 0
    # 100, 200, 300, 400 x2, 700, 800, and S2's 900 x3 (multi-allelic split + a duplicate locus)
    assert variants.count() == 10


def test_duplicate_call_sets_are_refused(hail, tmp_path):
    cs = cohort_qc.CallSet("S1", "bcftools", str(tmp_path / "x.vcf"))
    with pytest.raises(SystemExit, match="duplicate"):
        cohort_qc.run([cs, cs], str(tmp_path))
