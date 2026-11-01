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
