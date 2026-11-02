#!/usr/bin/env python3
"""Generate a tiny synthetic dataset with known variants, for offline end-to-end runs.

Writes into a storage root using the same key layout as the pipeline:

    <root>/reference/<ref>.fa          random reference genome
    <root>/raw_reads/<sample>.fastq.gz reads simulated from a mutated copy of it
                                       (--paired: <sample>_R1.fastq.gz + <sample>_R2.fastq.gz)
    <root>/truth/<sample>.json         the SNPs that were planted (the expected VCF calls)
                                       and the number of planted duplicate reads/pairs (--duplicate-rate)

Deterministic for a given --seed, needs only the standard library, and runs in about a second.

    python scripts/make_test_data.py --root local_bucket
    python scripts/make_test_data.py --root local_bucket --sample PAIRED001 --paired
"""

import argparse
import gzip
import json
import os
import random

BASES = "ACGT"
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
        seq = genome[start:start + read_length]
        if rng.random() < 0.5:
            seq = seq.translate(COMPLEMENT)[::-1]
        yield f"read{i}", add_errors(rng, seq, error_rate), "I" * read_length


def add_errors(rng, seq, error_rate):
    if not error_rate:
        return seq
    return "".join(rng.choice([b for b in BASES if b != base]) if rng.random() < error_rate else base
                   for base in seq)


def simulate_pairs(rng, genome, coverage, read_length, error_rate, insert_mean=400, insert_sd=50):
    """Illumina-style pairs: R1 reads the fragment forward from one end, R2 the reverse complement of the other.

    Yields (name, r1, r2, qual); both mates share the name, as bwa mem expects. Half the fragments come
    from the reverse strand. Fragment length ~ N(insert_mean, insert_sd), never shorter than one read.
    """
    n_pairs = len(genome) * coverage // (2 * read_length)
    for i in range(n_pairs):
        frag_len = min(len(genome), max(read_length, int(rng.gauss(insert_mean, insert_sd))))
        start = rng.randrange(0, len(genome) - frag_len + 1)
        fragment = genome[start:start + frag_len]
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


def write_fasta(path, name, seq, width=60):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="ascii", newline="\n") as fh:
        fh.write(f">{name}\n")
        for i in range(0, len(seq), width):
            fh.write(seq[i:i + width] + "\n")


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
    p.add_argument("--duplicate-rate", type=float, default=0.0,
                   help="fraction of reads (pairs with --paired) followed by an exact copy "
                        "(planted PCR duplicates); default 0")
    p.add_argument("--paired", action="store_true",
                   help="write paired-end reads as <sample>_R1.fastq.gz and <sample>_R2.fastq.gz "
                        "(same total coverage)")
    p.add_argument("--insert-size", type=int, default=400, help="mean fragment length for --paired")
    p.add_argument("--leading-gap", type=int, default=0, metavar="N",
                   help="start the reference with N Ns, like the gap at the start of a human chromosome "
                        "(the align stage only uses bwa-mem2 on such references; see BWA_MEM2_SAFE_N_PREFIX "
                        "in app/tasks.py). Reads and SNPs are the same, the SNPs N positions later; default 0")
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
        json.dump({"sample": a.sample, "reference": a.reference, "contig": a.contig, "snps": snps,
                   "planted_duplicates": n_dup}, fh, indent=2)

    unit = "pairs" if a.paired else "reads"
    gap = f", starting with {a.leading_gap:,} Ns" if a.leading_gap else ""
    print(f"reference : {os.path.join(a.root, 'reference', a.reference)} ({a.length + a.leading_gap:,} bp{gap})")
    print(f"reads     : {', '.join(fastq_paths)} ({n:,} {unit} x {a.read_length} bp, "
          f"{n_dup:,} planted duplicates)")
    print(f"truth     : {truth_path} ({len(snps)} SNPs)")


if __name__ == "__main__":
    main()
