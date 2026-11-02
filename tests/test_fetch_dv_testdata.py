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
