import gzip
import json

import make_test_data


def read_fasta(path):
    with open(path) as fh:
        return "".join(line.strip() for line in fh if not line.startswith(">"))


def test_generates_consistent_dataset(tmp_path):
    make_test_data.main(["--root", str(tmp_path), "--length", "5000", "--snps", "4", "--coverage", "10"])
    ref = read_fasta(tmp_path / "reference" / "synthetic_ref.fa")
    truth = json.loads((tmp_path / "truth" / "SYNTH001.json").read_text())
    assert len(ref) == 5000
    assert len(truth["snps"]) == 4
    for snp in truth["snps"]:
        assert ref[snp["pos"] - 1] == snp["ref"] != snp["alt"]
    with gzip.open(tmp_path / "raw_reads" / "SYNTH001.fastq.gz", "rt") as fh:
        assert sum(1 for _ in fh) == 4 * (5000 * 10 // 150)


def test_is_deterministic(tmp_path):
    make_test_data.main(["--root", str(tmp_path / "a"), "--length", "2000", "--snps", "2"])
    make_test_data.main(["--root", str(tmp_path / "b"), "--length", "2000", "--snps", "2"])
    a = (tmp_path / "a" / "truth" / "SYNTH001.json").read_text()
    b = (tmp_path / "b" / "truth" / "SYNTH001.json").read_text()
    assert a == b


def test_leading_gap_pads_the_reference_and_shifts_the_truth_only(tmp_path):
    args = ["--length", "3000", "--snps", "2", "--coverage", "5"]
    make_test_data.main(["--root", str(tmp_path / "a")] + args)
    make_test_data.main(["--root", str(tmp_path / "b"), "--leading-gap", "1000"] + args)
    ref_a = read_fasta(tmp_path / "a" / "reference" / "synthetic_ref.fa")
    ref_b = read_fasta(tmp_path / "b" / "reference" / "synthetic_ref.fa")
    assert ref_b == "N" * 1000 + ref_a
    snps_a = json.loads((tmp_path / "a" / "truth" / "SYNTH001.json").read_text())["snps"]
    snps_b = json.loads((tmp_path / "b" / "truth" / "SYNTH001.json").read_text())["snps"]
    assert [s["pos"] + 1000 for s in snps_a] == [s["pos"] for s in snps_b]
    for snp in snps_b:
        assert ref_b[snp["pos"] - 1] == snp["ref"]
    reads = [(tmp_path / r / "raw_reads" / "SYNTH001.fastq.gz").read_bytes() for r in ("a", "b")]
    assert gzip.decompress(reads[0]) == gzip.decompress(reads[1])  # same reads, only the reference moved


def read_fastq(path):
    with gzip.open(path, "rt") as fh:
        lines = fh.read().splitlines()
    return [(lines[i][1:], lines[i + 1]) for i in range(0, len(lines), 4)]


def test_duplicate_rate_zero_leaves_default_reads_unchanged(tmp_path):
    make_test_data.main(["--root", str(tmp_path / "a"), "--length", "3000", "--snps", "2"])
    make_test_data.main(["--root", str(tmp_path / "b"), "--length", "3000", "--snps", "2", "--duplicate-rate", "0"])
    fq = ("raw_reads", "SYNTH001.fastq.gz")
    assert read_fastq(tmp_path.joinpath("a", *fq)) == read_fastq(tmp_path.joinpath("b", *fq))
    assert json.loads((tmp_path / "a" / "truth" / "SYNTH001.json").read_text())["planted_duplicates"] == 0


def test_planted_duplicates_are_exact_copies_of_their_original(tmp_path):
    make_test_data.main(["--root", str(tmp_path), "--length", "5000", "--snps", "2", "--duplicate-rate", "0.2"])
    reads = read_fastq(tmp_path / "raw_reads" / "SYNTH001.fastq.gz")
    truth = json.loads((tmp_path / "truth" / "SYNTH001.json").read_text())
    by_name = dict(reads)
    dups = [(name, seq) for name, seq in reads if name.endswith("_dup")]

    assert truth["planted_duplicates"] == len(dups)
    n_original = 5000 * 30 // 150
    assert len(reads) == n_original + len(dups)
    assert 0.1 * n_original < len(dups) < 0.3 * n_original
    for name, seq in dups:
        assert by_name[name[:-len("_dup")]] == seq  # same sequence AND strand: a true duplicate


def revcomp(seq):
    return seq.translate(str.maketrans("ACGT", "TGCA"))[::-1]


def test_paired_writes_matching_mates_from_one_fragment(tmp_path):
    make_test_data.main(["--root", str(tmp_path), "--length", "5000", "--snps", "2", "--paired",
                         "--error-rate", "0", "--sample", "P1"])
    r1 = read_fastq(tmp_path / "raw_reads" / "P1_R1.fastq.gz")
    r2 = read_fastq(tmp_path / "raw_reads" / "P1_R2.fastq.gz")
    assert not (tmp_path / "raw_reads" / "P1.fastq.gz").exists()

    assert len(r1) == len(r2) == 5000 * 30 // (2 * 150)  # same total coverage as single-end
    assert [n for n, _ in r1] == [n for n, _ in r2]  # bwa mem pairs records by position and name
    # Mates face each other across the fragment (Illumina FR orientation). On the forward strand
    # the reference has R1 at the left end and revcomp(R2) at the right; for a reverse-strand
    # fragment, R2 on the left and revcomp(R1) on the right. With no sequencing errors only a
    # planted SNP inside a read stops it matching the reference exactly.
    genome = read_fasta(tmp_path / "reference" / "synthetic_ref.fa")
    strands, inserts = [], []
    for (_, a), (_, b) in zip(r1, r2, strict=True):
        for strand, left, right in (("+", a, revcomp(b)), ("-", b, revcomp(a))):
            p_left, p_right = genome.find(left), genome.find(right)
            if p_left >= 0 and p_right >= 0:
                strands.append(strand)
                inserts.append(p_right + 150 - p_left)
                break
    assert len(inserts) > 0.8 * len(r1)
    assert all(150 <= size <= 700 for size in inserts)
    assert 350 < sum(inserts) / len(inserts) < 450  # --insert-size default 400
    assert 0.35 < strands.count("+") / len(strands) < 0.65


def test_paired_duplicates_copy_both_mates(tmp_path):
    make_test_data.main(["--root", str(tmp_path), "--length", "5000", "--snps", "2", "--paired",
                         "--duplicate-rate", "0.2", "--sample", "P1"])
    r1 = dict(read_fastq(tmp_path / "raw_reads" / "P1_R1.fastq.gz"))
    r2 = dict(read_fastq(tmp_path / "raw_reads" / "P1_R2.fastq.gz"))
    dups = [n for n in r1 if n.endswith("_dup")]
    assert json.loads((tmp_path / "truth" / "P1.json").read_text())["planted_duplicates"] == len(dups) > 0
    for name in dups:
        original = name[:-len("_dup")]
        assert r1[name] == r1[original] and r2[name] == r2[original]
