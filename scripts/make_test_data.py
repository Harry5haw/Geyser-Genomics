#!/usr/bin/env python3
"""Generate a tiny synthetic dataset with known variants, for offline end-to-end runs.

Writes into a storage root using the same key layout as the pipeline:

    <root>/reference/<ref>.fa          random reference genome
    <root>/raw_reads/<sample>.fastq.gz reads simulated from a mutated copy of it
    <root>/truth/<sample>.json         the SNPs that were planted (the expected VCF calls)
                                       and the number of planted duplicate reads (--duplicate-rate)

Deterministic for a given --seed, needs only the standard library, and runs in about a second.

    python scripts/make_test_data.py --root local_bucket
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
        if error_rate:
            seq = "".join(
                rng.choice([b for b in BASES if b != base]) if rng.random() < error_rate else base
                for base in seq
            )
        yield f"read{i}", seq, "I" * read_length


def add_duplicates(reads, rng, rate):
    """After a fraction `rate` of reads, emit an exact copy (same sequence and strand), like a PCR duplicate.

    Copies are named <original>_dup so tests can count them. Uses its own rng so that rate 0 leaves the
    default dataset byte-for-byte unchanged.
    """
    for name, seq, qual in reads:
        yield name, seq, qual
        if rate and rng.random() < rate:
            yield f"{name}_dup", seq, qual


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
                   help="fraction of reads followed by an exact copy (planted PCR duplicates); default 0")
    a = p.parse_args(argv)

    rng = random.Random(a.seed)
    reference = make_reference(rng, a.length)
    sample_genome, snps = plant_snps(rng, reference, a.snps, a.read_length)

    write_fasta(os.path.join(a.root, "reference", a.reference), a.contig, reference)

    fastq_path = os.path.join(a.root, "raw_reads", f"{a.sample}.fastq.gz")
    os.makedirs(os.path.dirname(fastq_path), exist_ok=True)
    n = n_dup = 0
    reads = simulate_reads(rng, sample_genome, a.coverage, a.read_length, a.error_rate)
    reads = add_duplicates(reads, random.Random(a.seed + 1), a.duplicate_rate)
    with gzip.open(fastq_path, "wt", encoding="ascii", newline="\n") as fh:
        for name, seq, qual in reads:
            fh.write(f"@{name}\n{seq}\n+\n{qual}\n")
            n += 1
            n_dup += name.endswith("_dup")

    truth_path = os.path.join(a.root, "truth", f"{a.sample}.json")
    os.makedirs(os.path.dirname(truth_path), exist_ok=True)
    with open(truth_path, "w", encoding="utf-8") as fh:
        json.dump({"sample": a.sample, "reference": a.reference, "contig": a.contig, "snps": snps,
                   "planted_duplicates": n_dup}, fh, indent=2)

    print(f"reference : {os.path.join(a.root, 'reference', a.reference)} ({a.length:,} bp)")
    print(f"reads     : {fastq_path} ({n:,} x {a.read_length} bp, {n_dup:,} planted duplicates)")
    print(f"truth     : {truth_path} ({len(snps)} SNPs)")


if __name__ == "__main__":
    main()
