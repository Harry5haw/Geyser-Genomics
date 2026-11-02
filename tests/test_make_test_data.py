import gzip
import json

import pytest

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
        assert by_name[name[: -len("_dup")]] == seq  # same sequence AND strand: a true duplicate


def revcomp(seq):
    return seq.translate(str.maketrans("ACGT", "TGCA"))[::-1]


def test_paired_writes_matching_mates_from_one_fragment(tmp_path):
    make_test_data.main(
        ["--root", str(tmp_path), "--length", "5000", "--snps", "2", "--paired", "--error-rate", "0", "--sample", "P1"]
    )
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
    make_test_data.main(
        [
            "--root",
            str(tmp_path),
            "--length",
            "5000",
            "--snps",
            "2",
            "--paired",
            "--duplicate-rate",
            "0.2",
            "--sample",
            "P1",
        ]
    )
    r1 = dict(read_fastq(tmp_path / "raw_reads" / "P1_R1.fastq.gz"))
    r2 = dict(read_fastq(tmp_path / "raw_reads" / "P1_R2.fastq.gz"))
    dups = [n for n in r1 if n.endswith("_dup")]
    assert json.loads((tmp_path / "truth" / "P1.json").read_text())["planted_duplicates"] == len(dups) > 0
    for name in dups:
        original = name[: -len("_dup")]
        assert r1[name] == r1[original] and r2[name] == r2[original]


def test_gff_adds_gene_models_around_planted_snps_and_changes_nothing_else(tmp_path):
    plain, annotated = tmp_path / "plain", tmp_path / "gff"
    make_test_data.main(["--root", str(plain)])
    make_test_data.main(["--root", str(annotated), "--gff"])
    assert (plain / "reference/synthetic_ref.fa").read_bytes() == (
        annotated / "reference/synthetic_ref.fa"
    ).read_bytes()
    reads = "raw_reads/SYNTH001.fastq.gz"  # compare contents: a gzip header holds its write time
    assert gzip.open(plain / reads).read() == gzip.open(annotated / reads).read()
    assert not (plain / "reference" / "synthetic_ref.fa.gff3.gz").exists()
    assert "genes" not in json.loads((plain / "truth" / "SYNTH001.json").read_text())

    truth = json.loads((annotated / "truth" / "SYNTH001.json").read_text())
    genes, snps = truth["genes"], [s["pos"] for s in truth["snps"]]
    assert [g["strand"] for g in genes] == ["+", "-"]
    for g in genes:
        assert (g["end"] - g["start"] + 1) % 3 == 0  # whole codons
    inside = [p for p in snps if any(g["start"] <= p <= g["end"] for g in genes)]
    assert inside == snps[1:5]  # SNPs 2-5 are genic, the rest intergenic

    lines = gzip.open(annotated / "reference" / "synthetic_ref.fa.gff3.gz", "rt").read().splitlines()
    assert lines[0] == "##gff-version 3"
    features = [line.split("\t") for line in lines[1:]]
    assert [f[2] for f in features] == ["gene", "mRNA", "CDS"] * 2
    assert all(f[0] == "synth1" for f in features) and "biotype=protein_coding" in features[0][8]


def test_gff_coordinates_follow_the_leading_gap(tmp_path):
    make_test_data.main(["--root", str(tmp_path), "--gff", "--leading-gap", "1000"])
    truth = json.loads((tmp_path / "truth" / "SYNTH001.json").read_text())
    for g in truth["genes"]:
        assert g["start"] > 1000
    assert truth["genes"][0]["start"] <= truth["snps"][1]["pos"] <= truth["genes"][0]["end"]


def test_a_run_without_gff_removes_gene_models_left_by_an_earlier_one(tmp_path):
    make_test_data.main(["--root", str(tmp_path), "--gff"])
    assert (tmp_path / "reference" / "synthetic_ref.fa.gff3.gz").exists()
    make_test_data.main(["--root", str(tmp_path), "--seed", "2"])
    assert not (tmp_path / "reference" / "synthetic_ref.fa.gff3.gz").exists()


# ---- --pangenome: a small graph of the synthetic reference ----


def _graph(seed=42):
    import random

    rng = random.Random(seed)
    reference = make_test_data.make_reference(rng, 20_000)
    _, snps = make_test_data.plant_snps(rng, reference, 4, 150)
    decoys = make_test_data.decoy_snps(random.Random(seed + 2), reference, snps, 3)
    return reference, snps, decoys, make_test_data.pangenome_gfa("synth1", reference, snps, decoys)


def test_reference_walk_spells_the_reference():
    reference, _, _, gfa = _graph()
    assert walk_sequence(gfa, make_test_data.GRAPH_REFERENCE, 0) == reference
    walks = [line for line in gfa.splitlines() if line.startswith("W\t")]
    assert walks[0].split("\t")[1:6] == [make_test_data.GRAPH_REFERENCE, "0", "synth1", "0", str(len(reference))]
    assert len(walks) == 1 + len(make_test_data.PANEL_HAPLOTYPES)


def test_panel_haplotypes_carry_the_planted_snps_mostly_and_decoys_half():
    reference, snps, decoys, gfa = _graph()
    for hap, (sample, number) in enumerate(make_test_data.PANEL_HAPLOTYPES):
        seq = walk_sequence(gfa, sample, number)
        assert len(seq) == len(reference)
        for i, snp in enumerate(snps):
            base = seq[snp["pos"] - 1]
            assert base == (snp["alt"] if make_test_data.panel_carries(i, hap, decoy=False) else snp["ref"])
        for d in decoys:
            assert seq[d["pos"] - 1] == (d["alt"] if hap < len(make_test_data.PANEL_HAPLOTYPES) // 2 else d["ref"])
        variant_positions = {v["pos"] - 1 for v in snps + decoys}
        assert all(seq[i] == reference[i] for i in range(len(reference)) if i not in variant_positions)
    # every planted SNP is on all but one panel haplotype
    for i in range(len(snps)):
        panel = len(make_test_data.PANEL_HAPLOTYPES)
        assert sum(make_test_data.panel_carries(i, h, decoy=False) for h in range(panel)) == panel - 1


def test_decoys_keep_clear_of_planted_snps():
    _, snps, decoys, _ = _graph()
    assert len(decoys) == 3
    for d in decoys:
        assert all(abs(d["pos"] - s["pos"]) >= 1000 for s in snps)
        assert d["alt"] != d["ref"]


def test_gfa_rejects_a_variant_that_does_not_match_the_reference():
    reference, snps, _, _ = _graph()
    wrong = [{**snps[0], "ref": next(b for b in "ACGT" if b != snps[0]["ref"])}]
    with pytest.raises(ValueError, match="reference base"):
        make_test_data.pangenome_gfa("synth1", reference, wrong, [])


def test_gbz_command_keeps_the_reference_sample(tmp_path):
    cmd = make_test_data.gbz_command(str(tmp_path), "pangenome/x.gfa", "pangenome/x.gbz")
    assert make_test_data.VG_IMAGE in cmd
    script = cmd[-1]
    assert "vg gbwt -G /data/pangenome/x.gfa --gbz-format" in script and "--set-reference GRCh38" in script
    assert script.endswith("mv /data/pangenome/x.gbz.part /data/pangenome/x.gbz")


def test_vg_image_matches_the_hprc_graph_builder():
    import fetch_dv_testdata

    assert make_test_data.VG_IMAGE == fetch_dv_testdata.VG_IMAGE


def test_pangenome_flag_writes_gfa_and_runs_vg(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(make_test_data.subprocess, "run", lambda cmd, check: ran.append(cmd))
    make_test_data.main(["--root", str(tmp_path), "--pangenome", "--leading-gap", "100"])
    gfa = (tmp_path / "pangenome" / "synthetic_ref.gfa").read_text()
    fasta = "".join((tmp_path / "reference" / "synthetic_ref.fa").read_text().splitlines()[1:])
    assert walk_sequence(gfa, make_test_data.GRAPH_REFERENCE, 0) == fasta  # leading gap included
    assert ran and ran[0][-1].endswith("/data/pangenome/synthetic_ref.gbz")


def walk_sequence(gfa, sample, hap):
    """The sequence a GFA W-line spells (one pass over the GFA)."""
    segments, walk = {}, None
    for line in gfa.splitlines():
        cols = line.split("\t")
        if cols[0] == "S":
            segments[cols[1]] = cols[2]
        elif cols[0] == "W" and cols[1] == sample and cols[2] == str(hap):
            walk = cols[6]
    if walk is None:
        raise KeyError((sample, hap))
    return "".join(segments[n] for n in walk.split(">")[1:])


def test_adjacent_and_end_variants_make_no_empty_segments():
    reference = "ACGTACGTAC"
    planted = [{"pos": 1, "ref": "A", "alt": "G"}, {"pos": 2, "ref": "C", "alt": "T"}]
    decoys = [{"pos": 10, "ref": "C", "alt": "A"}]
    gfa = make_test_data.pangenome_gfa("c", reference, planted, decoys)
    assert all(line.split("\t")[2] for line in gfa.splitlines() if line.startswith("S\t"))
    assert walk_sequence(gfa, make_test_data.GRAPH_REFERENCE, 0) == reference
    first = walk_sequence(gfa, *make_test_data.PANEL_HAPLOTYPES[0])
    assert first[-1] == "A" and len(first) == len(reference)  # the first half of the panel carries decoys


def test_decoys_that_do_not_fit_fail_clearly():
    import random

    reference = make_test_data.make_reference(random.Random(1), 50_000)
    dense = [{"pos": p, "ref": reference[p - 1], "alt": "N"} for p in range(500, 50_000, 900)]
    with pytest.raises(ValueError, match="no room"):
        make_test_data.decoy_snps(random.Random(2), reference, dense, 5)
    with pytest.raises(ValueError, match="too short"):
        make_test_data.decoy_snps(random.Random(2), reference[:1500], [], 1)


def test_pangenome_that_cannot_be_made_stops_before_writing_reads(tmp_path):
    with pytest.raises(SystemExit):
        make_test_data.main(["--root", str(tmp_path), "--pangenome", "--snps", "60"])
    assert not (tmp_path / "raw_reads").exists()


def test_a_run_without_pangenome_removes_an_older_graph(tmp_path, monkeypatch):
    monkeypatch.setattr(make_test_data.subprocess, "run", lambda cmd, check: None)
    make_test_data.main(["--root", str(tmp_path), "--pangenome"])
    (tmp_path / "pangenome" / "synthetic_ref.gbz").write_bytes(b"graph")
    make_test_data.main(["--root", str(tmp_path), "--seed", "7"])
    assert not (tmp_path / "pangenome" / "synthetic_ref.gbz").exists()
    assert not (tmp_path / "pangenome" / "synthetic_ref.gfa").exists()


def test_gbz_command_cleans_up_and_caps_memory(tmp_path):
    cmd = make_test_data.gbz_command(str(tmp_path), "pangenome/x.gfa", "pangenome/x.gbz")
    assert cmd[3:5] == ["--memory", "4g"]
    assert cmd[-1].startswith("set -e; trap 'rm -f /data/pangenome/x.gbz.part' ERR;")
