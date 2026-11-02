#!/usr/bin/env python3
"""Generate a tiny synthetic dataset with known variants, for offline end-to-end runs.

Writes into a storage root using the same key layout as the pipeline:

    <root>/reference/<ref>.fa          random reference genome
    <root>/raw_reads/<sample>.fastq.gz reads simulated from a mutated copy of it
                                       (--paired: <sample>_R1.fastq.gz + <sample>_R2.fastq.gz)
    <root>/truth/<sample>.json         the SNPs that were planted (the expected VCF calls)
                                       and the number of planted duplicate reads/pairs (--duplicate-rate)
    <root>/pangenome/<ref stem>.gbz    --pangenome: a small pangenome graph of the reference, for
                                       run_local.py --caller pangenome --pangenome <ref stem>.gbz

Deterministic for a given --seed, needs only the standard library, and runs in about a second.

    python scripts/make_test_data.py --root local_bucket
    python scripts/make_test_data.py --root local_bucket --sample PAIRED001 --paired
    python scripts/make_test_data.py --root local_bucket --pangenome    # + graph (vg in Docker)

--pangenome writes the graph as GFA (standard library) and converts it to GBZ with vg in Docker, as
fetch_dv_testdata.py does for the HPRC chr20 graph. The reference is the GRCh38 path (the sample name
pangenome-aware DeepVariant looks for by default) and PANEL_HAPLOTYPES panel haplotypes walk it. Most of them
carry each planted SNP, and half of them carry DECOY_SNPS extra SNPs the sample does not have, so a correct
pangenome-aware call set is still exactly the planted SNPs.
"""

import argparse
import gzip
import json
import os
import random
import subprocess

BASES = "ACGT"
# --pangenome: the panel (diploid samples, two haplotypes each), the graph's reference sample name, extra SNPs only
# the panel carries, and the vg release used for the HPRC graph in fetch_dv_testdata.py.
PANEL_SAMPLES = ("PANEL1", "PANEL2", "PANEL3")
PANEL_HAPLOTYPES = [(sample, hap) for sample in PANEL_SAMPLES for hap in (1, 2)]
GRAPH_REFERENCE = "GRCh38"
DECOY_SNPS = 5
VG_IMAGE = "quay.io/vgteam/vg:v1.55.0"
COMPLEMENT = str.maketrans("ACGT", "TGCA")


def make_reference(rng, length):
    return "".join(rng.choice(BASES) for _ in range(length))


def plant_snps(rng, reference, count, read_length):
    """Homozygous SNPs, spaced well apart and away from the contig ends so every one gets full coverage."""
    margin = read_length * 2
    spacing = (len(reference) - 2 * margin) // count
    sample = list(reference)
    snps = []
    for i in range(count):
        pos0 = margin + i * spacing + rng.randrange(spacing // 4, 3 * spacing // 4)
        ref_base = reference[pos0]
        alt_base = rng.choice([b for b in BASES if b != ref_base])
        sample[pos0] = alt_base
        snps.append({"pos": pos0 + 1, "ref": ref_base, "alt": alt_base})  # VCF positions are 1-based
    return "".join(sample), snps


def simulate_reads(rng, genome, coverage, read_length, error_rate):
    n_reads = len(genome) * coverage // read_length
    for i in range(n_reads):
        start = rng.randrange(0, len(genome) - read_length + 1)
        seq = genome[start : start + read_length]
        if rng.random() < 0.5:
            seq = seq.translate(COMPLEMENT)[::-1]
        yield f"read{i}", add_errors(rng, seq, error_rate), "I" * read_length


def add_errors(rng, seq, error_rate):
    if not error_rate:
        return seq
    return "".join(rng.choice([b for b in BASES if b != base]) if rng.random() < error_rate else base for base in seq)


def simulate_pairs(rng, genome, coverage, read_length, error_rate, insert_mean=400, insert_sd=50):
    """Illumina-style pairs: R1 reads the fragment forward from one end, R2 the reverse complement of the other.

    Yields (name, r1, r2, qual); both mates share the name, as bwa mem expects. Half the fragments come
    from the reverse strand. Fragment length ~ N(insert_mean, insert_sd), never shorter than one read.
    """
    n_pairs = len(genome) * coverage // (2 * read_length)
    for i in range(n_pairs):
        frag_len = min(len(genome), max(read_length, int(rng.gauss(insert_mean, insert_sd))))
        start = rng.randrange(0, len(genome) - frag_len + 1)
        fragment = genome[start : start + frag_len]
        if rng.random() < 0.5:
            fragment = fragment.translate(COMPLEMENT)[::-1]
        r1 = fragment[:read_length]
        r2 = fragment[-read_length:].translate(COMPLEMENT)[::-1]
        yield f"pair{i}", add_errors(rng, r1, error_rate), add_errors(rng, r2, error_rate), "I" * read_length


def add_duplicates(records, rng, rate):
    """After a fraction `rate` of reads (or pairs), emit an exact copy, like a PCR duplicate.

    A copy has the same sequence(s) and strand, and is named <original>_dup so tests can count them.
    Uses its own rng so that rate 0 leaves the default dataset byte-for-byte unchanged.
    """
    for record in records:
        yield record
        if rate and rng.random() < rate:
            yield (f"{record[0]}_dup",) + tuple(record[1:])


def gene_models(snps, length, offset=0, flank=300):
    """Two single-exon protein-coding genes for bcftools csq, around planted SNPs 2-3 (+ strand)
    and 4-5 (- strand); the other SNPs stay intergenic. No randomness: the reads and SNPs are the
    same with or without --gff. Each CDS is codon-aligned (a multiple of 3 bases). Coordinates are
    1-based and include `offset` (the leading gap). Returns [] if there are too few SNPs.
    """
    genes = []
    for name, strand, (a, b) in (("SYNTHG1", "+", (1, 2)), ("SYNTHG2", "-", (3, 4))):
        if len(snps) <= b:
            continue
        start = max(1, snps[a]["pos"] - offset - flank)
        end = min(length, snps[b]["pos"] - offset + flank)
        end = start + (end - start + 1) // 3 * 3 - 1
        genes.append({"name": name, "strand": strand, "start": start + offset, "end": end + offset})
    return genes


def write_gff(path, contig, genes):
    """Ensembl-style GFF3, the layout bcftools csq expects (gene -> mRNA -> CDS, with biotype)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wt", encoding="ascii", newline="\n") as fh:
        fh.write("##gff-version 3\n")
        for g in genes:
            n, s, e, strand = g["name"], g["start"], g["end"], g["strand"]
            fh.write(f"{contig}\tgeyser\tgene\t{s}\t{e}\t.\t{strand}\t.\tID=gene:{n};Name={n};biotype=protein_coding\n")
            fh.write(
                f"{contig}\tgeyser\tmRNA\t{s}\t{e}\t.\t{strand}\t.\t"
                f"ID=transcript:{n}T;Parent=gene:{n};Name={n}-201;biotype=protein_coding\n"
            )
            fh.write(f"{contig}\tgeyser\tCDS\t{s}\t{e}\t.\t{strand}\t0\tID=CDS:{n}P;Parent=transcript:{n}T\n")


def decoy_snps(rng, reference, planted, count, offset=0, gap=1000, attempts=10_000):
    """SNPs only the graph's panel carries: at least `gap` bp from the planted ones and each other, and from the
    contig ends. Positions are 1-based and include offset, like the planted SNPs after the leading gap is added.
    ValueError if they do not fit (a short reference, or planted SNPs closer together than 2 x gap)."""
    if len(reference) <= 2 * gap:
        raise ValueError(f"a {len(reference):,} bp reference is too short for decoy SNPs (needs > {2 * gap:,} bp)")
    taken = [snp["pos"] for snp in planted]
    decoys = []
    for _ in range(attempts):
        if len(decoys) == count:
            break
        pos0 = rng.randrange(gap, len(reference) - gap)
        pos = pos0 + 1 + offset
        if all(abs(pos - t) >= gap for t in taken):
            ref_base = reference[pos0]
            decoys.append({"pos": pos, "ref": ref_base, "alt": rng.choice([b for b in BASES if b != ref_base])})
            taken.append(pos)
    if len(decoys) < count:
        raise ValueError(
            f"no room for {count} decoy SNPs {gap:,} bp from the planted ones: use fewer --snps or a longer --length"
        )
    return sorted(decoys, key=lambda d: d["pos"])


def panel_carries(index, hap, decoy):
    """Which panel haplotypes carry which variant: planted SNP i is on every haplotype but one (rotating); a decoy
    is on the first half of the panel only."""
    if decoy:
        return hap < len(PANEL_HAPLOTYPES) // 2
    return hap != index % len(PANEL_HAPLOTYPES)


def pangenome_gfa(contig, sequence, planted, decoys):
    """GFA 1.1 of the reference with one SNP bubble per variant, the reference as a GRCh38 walk and the panel
    haplotypes as walks. sequence is the whole FASTA sequence (leading gap included); positions are 1-based.
    Variants may be adjacent or at either end: no empty segment is written."""
    variants = sorted([(v, False) for v in planted] + [(v, True) for v in decoys], key=lambda x: x[0]["pos"])
    segments, links = [], []
    ref_walk = []
    hap_walks = [[] for _ in PANEL_HAPLOTYPES]
    tails = []  # segments the walks are currently on (nothing before the first segment)

    def add_segment(seq):
        segments.append(seq)
        return len(segments)

    def append_shared(seq):
        """A stretch every walk shares; skipped if empty."""
        nonlocal tails
        if not seq:
            return
        seg = add_segment(seq)
        links.extend((t, seg) for t in tails)
        ref_walk.append(seg)
        for walk in hap_walks:
            walk.append(seg)
        tails = [seg]

    start, planted_index = 0, 0
    for variant, decoy in variants:
        pos0 = variant["pos"] - 1
        if pos0 < start:
            raise ValueError(f"variants overlap at position {variant['pos']}")
        if sequence[pos0] != variant["ref"]:
            raise ValueError(f"reference base at {variant['pos']} is {sequence[pos0]}, not {variant['ref']}")
        append_shared(sequence[start:pos0])
        ref_seg, alt_seg = add_segment(variant["ref"]), add_segment(variant["alt"])
        links.extend((t, seg) for t in tails for seg in (ref_seg, alt_seg))
        ref_walk.append(ref_seg)
        for hap, walk in enumerate(hap_walks):
            walk.append(alt_seg if panel_carries(planted_index, hap, decoy) else ref_seg)
        tails = [ref_seg, alt_seg]
        planted_index += not decoy
        start = pos0 + 1
    append_shared(sequence[start:])

    lines = ["H\tVN:Z:1.1"]
    lines += [f"S\t{i}\t{seq}" for i, seq in enumerate(segments, start=1)]
    lines += [f"L\t{a}\t+\t{b}\t+\t0M" for a, b in links]

    def walk_line(sample, hap, walk):
        length = sum(len(segments[i - 1]) for i in walk)
        return f"W\t{sample}\t{hap}\t{contig}\t0\t{length}\t" + "".join(f">{i}" for i in walk)

    lines.append(walk_line(GRAPH_REFERENCE, 0, ref_walk))
    lines += [walk_line(sample, hap, walk) for (sample, hap), walk in zip(PANEL_HAPLOTYPES, hap_walks, strict=True)]
    return "\n".join(lines) + "\n"


def gbz_command(root, gfa_key, gbz_key, image=VG_IMAGE, memory="4g"):
    """docker run converting the GFA to GBZ with the reference sample kept as the reference (vg gbwt -G). A failed
    conversion removes its partial file. The GFA is kept next to the GBZ (about 50 KB, readable when debugging)."""
    gfa, gbz = f"/data/{gfa_key}", f"/data/{gbz_key}"
    script = (
        f"set -e; trap 'rm -f {gbz}.part' ERR; "
        f"vg gbwt -G {gfa} --gbz-format -g {gbz}.part --set-reference {GRAPH_REFERENCE}; mv {gbz}.part {gbz}"
    )
    return [
        "docker",
        "run",
        "--rm",
        "--memory",
        memory,
        "--mount",
        f"type=bind,source={os.path.abspath(root)},target=/data",
        image,
        "bash",
        "-c",
        script,
    ]


def write_fasta(path, name, seq, width=60):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="ascii", newline="\n") as fh:
        fh.write(f">{name}\n")
        for i in range(0, len(seq), width):
            fh.write(seq[i : i + width] + "\n")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default="local_bucket", help="storage root (LOCAL_STORAGE_DIR)")
    p.add_argument("--sample", default="SYNTH001")
    p.add_argument("--reference", default="synthetic_ref.fa")
    p.add_argument("--contig", default="synth1")
    p.add_argument("--length", type=int, default=50_000)
    p.add_argument("--snps", type=int, default=10)
    p.add_argument("--coverage", type=int, default=30)
    p.add_argument("--read-length", type=int, default=150)
    p.add_argument("--error-rate", type=float, default=0.001)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--duplicate-rate",
        type=float,
        default=0.0,
        help="fraction of reads (pairs with --paired) followed by an exact copy (planted PCR duplicates); default 0",
    )
    p.add_argument(
        "--paired",
        action="store_true",
        help="write paired-end reads as <sample>_R1.fastq.gz and <sample>_R2.fastq.gz (same total coverage)",
    )
    p.add_argument("--insert-size", type=int, default=400, help="mean fragment length for --paired")
    p.add_argument(
        "--gff",
        action="store_true",
        help="also write gene models, reference/<reference>.gff3.gz: two genes around planted SNPs 2-5, "
        "so the variants stage annotates them with bcftools csq (reads and SNPs unchanged)",
    )
    p.add_argument(
        "--leading-gap",
        type=int,
        default=0,
        metavar="N",
        help="start the reference with N Ns, like the gap at the start of a human chromosome "
        "(the align stage only uses bwa-mem2 on such references; see BWA_MEM2_SAFE_N_PREFIX "
        "in app/tasks.py). Reads and SNPs are the same, the SNPs N positions later; default 0",
    )
    p.add_argument(
        "--pangenome",
        action="store_true",
        help="also write a small pangenome graph, pangenome/<reference stem>.gbz (GFA built here, converted with vg "
        "in Docker), for run_local.py --caller pangenome --pangenome <reference stem>.gbz",
    )
    a = p.parse_args(argv)
    if a.leading_gap < 0:
        p.error("--leading-gap must be >= 0")

    rng = random.Random(a.seed)
    reference = make_reference(rng, a.length)
    sample_genome, snps = plant_snps(rng, reference, a.snps, a.read_length)
    # The gap goes into the FASTA only, after simulation, so the reads (and the rng) are unchanged.
    for snp in snps:
        snp["pos"] += a.leading_gap

    write_fasta(os.path.join(a.root, "reference", a.reference), a.contig, "N" * a.leading_gap + reference)
    genes = gene_models(snps, a.length, a.leading_gap) if a.gff else []
    gff_path = os.path.join(a.root, "reference", a.reference + ".gff3.gz")
    if genes:
        write_gff(gff_path, a.contig, genes)
    elif os.path.exists(gff_path):
        os.remove(gff_path)  # an earlier --gff run's models describe a different genome now

    stem = os.path.splitext(a.reference)[0]
    graph_keys = (f"pangenome/{stem}.gfa", f"pangenome/{stem}.gbz")
    for key in graph_keys:  # an earlier --pangenome run's graph describes another genome now
        if os.path.exists(os.path.join(a.root, *key.split("/"))):
            os.remove(os.path.join(a.root, *key.split("/")))
    gfa = None
    if a.pangenome:  # before any reads are written: a graph that cannot be made stops the run here
        try:
            decoys = decoy_snps(random.Random(a.seed + 2), reference, snps, DECOY_SNPS, a.leading_gap)
            gfa = pangenome_gfa(a.contig, "N" * a.leading_gap + reference, snps, decoys)
        except ValueError as err:
            p.error(f"--pangenome: {err}")

    raw_dir = os.path.join(a.root, "raw_reads")
    os.makedirs(raw_dir, exist_ok=True)
    if a.paired:
        records = simulate_pairs(rng, sample_genome, a.coverage, a.read_length, a.error_rate, a.insert_size)
        fastq_paths = [os.path.join(raw_dir, f"{a.sample}_{mate}.fastq.gz") for mate in ("R1", "R2")]
    else:
        records = simulate_reads(rng, sample_genome, a.coverage, a.read_length, a.error_rate)
        fastq_paths = [os.path.join(raw_dir, f"{a.sample}.fastq.gz")]
    records = add_duplicates(records, random.Random(a.seed + 1), a.duplicate_rate)

    n = n_dup = 0
    handles = [gzip.open(path, "wt", encoding="ascii", newline="\n") for path in fastq_paths]
    try:
        for name, *seqs, qual in records:
            for fh, seq in zip(handles, seqs, strict=True):
                fh.write(f"@{name}\n{seq}\n+\n{qual}\n")
            n += 1
            n_dup += name.endswith("_dup")
    finally:
        for fh in handles:
            fh.close()

    truth_path = os.path.join(a.root, "truth", f"{a.sample}.json")
    os.makedirs(os.path.dirname(truth_path), exist_ok=True)
    with open(truth_path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "sample": a.sample,
                "reference": a.reference,
                "contig": a.contig,
                "snps": snps,
                "planted_duplicates": n_dup,
                **({"genes": genes} if genes else {}),  # --gff only: the default truth file is unchanged
            },
            fh,
            indent=2,
        )

    graph_note = ""
    if gfa is not None:
        gfa_key, gbz_key = graph_keys
        os.makedirs(os.path.join(a.root, "pangenome"), exist_ok=True)
        with open(os.path.join(a.root, *gfa_key.split("/")), "w", encoding="ascii", newline="\n") as fh:
            fh.write(gfa)
        subprocess.run(gbz_command(a.root, gfa_key, gbz_key), check=True)
        graph_note = f"{gbz_key} ({len(PANEL_HAPLOTYPES)} panel haplotypes, {DECOY_SNPS} decoy SNPs)"

    unit = "pairs" if a.paired else "reads"
    gap = f", starting with {a.leading_gap:,} Ns" if a.leading_gap else ""
    print(f"reference : {os.path.join(a.root, 'reference', a.reference)} ({a.length + a.leading_gap:,} bp{gap})")
    print(f"reads     : {', '.join(fastq_paths)} ({n:,} {unit} x {a.read_length} bp, {n_dup:,} planted duplicates)")
    print(f"truth     : {truth_path} ({len(snps)} SNPs)")
    if graph_note:
        print(f"pangenome : {os.path.join(a.root, graph_note)}")


if __name__ == "__main__":
    main()
