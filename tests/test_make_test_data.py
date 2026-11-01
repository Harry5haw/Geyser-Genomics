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
