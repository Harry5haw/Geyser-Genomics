"""fetch_dv_testdata.py helpers (no network)."""

import gzip

import pytest

import fetch_dv_testdata as f


def test_fasta_contig_range_from_fai():
    # chr2: 25 bases, 10 per line + newline -> 3 lines "10\n10\n5\n" = 11 + 11 + 6 = 28 bytes
    fai = "chr1\t7\t6\t10\t11\nchr2\t25\t20\t10\t11\n"
    assert f.fasta_contig_range(fai, "chr2") == (20, 47, 25)
    # exact multiple of the line length: no partial last line
    assert f.fasta_contig_range("c\t20\t3\t10\t11\n", "c") == (3, 24, 20)
    with pytest.raises(ValueError):
        f.fasta_contig_range(fai, "chrX")


def test_fasta_contig_range_matches_real_layout(tmp_path):
    # Build a FASTA, index it the way samtools does, and check the range slices out exactly one contig.
    seqs = {"a": "ACGT" * 7, "b": "TTGCA" * 9}
    fasta, fai, offset = b"", "", 0
    for name, seq in seqs.items():
        header = f">{name}\n".encode()
        lines = [seq[i : i + 10] for i in range(0, len(seq), 10)]
        body = "".join(line + "\n" for line in lines).encode()
        fai += f"{name}\t{len(seq)}\t{offset + len(header)}\t10\t11\n"
        fasta += header + body
        offset += len(header) + len(body)
    first, last, length = f.fasta_contig_range(fai, "b")
    assert fasta[first : last + 1].decode().replace("\n", "") == seqs["b"]
    assert fasta[last : last + 1] == b"\n"


def test_keep_contig_filters_vcf_and_bed(tmp_path):
    src = tmp_path / "in.vcf.gz"
    src.write_bytes(gzip.compress(b"##fileformat=VCFv4.2\n#CHROM\tPOS\nchr1\t5\nchr20\t7\nchr20\t9\nchr21\t1\n"))
    kept = f.keep_contig(str(src), str(tmp_path / "out.vcf.gz"), "chr20")
    assert kept == 2
    assert (
        gzip.decompress((tmp_path / "out.vcf.gz").read_bytes())
        == b"##fileformat=VCFv4.2\n#CHROM\tPOS\nchr20\t7\nchr20\t9\n"
    )

    bed = tmp_path / "in.bed"
    bed.write_text("chr2\t1\t5\nchr20\t10\t20\nchr200\t1\t2\n")
    assert f.keep_contig(str(bed), str(tmp_path / "out.bed"), "chr20") == 1
    assert (tmp_path / "out.bed").read_text() == "chr20\t10\t20\n"


def test_ensembl_gff_is_cut_to_the_contig_and_renamed(tmp_path):
    src = tmp_path / "ensembl.gff3.gz"
    with gzip.open(src, "wt", encoding="utf-8", newline="") as fh:
        fh.write("##gff-version 3\n##sequence-region   20 1 64444167\n##sequence-region   21 1 46709983\n")
        fh.write("#!genome-build  GRCh38.p14\n")
        fh.write("20\tensembl_havana\tgene\t100\t900\t.\t+\t.\tID=gene:ENSG1;biotype=protein_coding\n")
        fh.write("21\tensembl_havana\tgene\t100\t900\t.\t+\t.\tID=gene:ENSG2;biotype=protein_coding\n")
        fh.write("20\tensembl_havana\tCDS\t200\t400\t.\t+\t0\tParent=transcript:ENST1\n")
    dest = tmp_path / "out.gff3.gz"
    assert f.ensembl_gff_for_contig(str(src), str(dest), "chr20") == 2
    lines = gzip.open(dest, "rt").read().splitlines()
    assert "##sequence-region   chr20 1 64444167" in lines
    assert not any(" 21 " in line or line.startswith("21\t") for line in lines)
    assert [line.split("\t")[0] for line in lines if not line.startswith("#")] == ["chr20", "chr20"]


def test_annotation_needs_a_grch38_dataset_and_lands_next_to_the_reference():
    (entry,) = [e for e in f.dataset_files("hg003-chr20", annotation=True) if e[3].startswith("ensembl:")]
    url, size, key, how = entry
    assert url.endswith(f"Homo_sapiens.GRCh38.{f.ENSEMBL_RELEASE}.chromosome.20.gff3.gz") and size == 2019220
    assert key == "reference/GRCh38_chr20.fa.gff3.gz" and how == "ensembl:chr20"
    with pytest.raises(ValueError, match="--annotation needs a GRCh38 dataset"):
        f.dataset_files("na12878-slice", annotation=True)
