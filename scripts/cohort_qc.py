"""Cohort QC over the pipeline's own VCFs with Hail (genomics on Spark): sample_qc, variant_qc, caller comparison.

Each sample can have up to three call sets, at the pipeline's keys (app/provenance.py STAGE_VCF):

    variants/{id}.vcf.gz               bcftools (the default caller)
    variants_deepvariant/{id}.vcf.gz   DeepVariant
    variants_pangenome/{id}.vcf.gz     pangenome-aware DeepVariant

All of them go into one Hail MatrixTable: rows are variants (locus, alleles; multi-allelic sites split), columns are
(sample, caller) call sets. Only PASS calls count: DeepVariant writes RefCall and NoCall candidates into its VCF
(about half its records), and over all records HG003's Ti/Tv is 0.86 instead of 1.96 (CLAUDE.md, G4). bcftools
writes FILTER "." (no filters applied), which counts as PASS.

    python scripts/cohort_qc.py --root <storage dir> --samples HG003_chr20 HG005_chr20 --out cohort/qc
    python scripts/cohort_qc.py --vcf HG003:bcftools:/data/a.vcf.gz --vcf HG003:deepvariant:/data/b.vcf.gz --out ...

Writes, under --out:
    sample_qc.parquet/        one row per (sample, caller): hl.sample_qc (call rate, depth, allele counts ...)
    caller_overlap.parquet/   per sample: how many PASS calls each combination of callers made, SNPs and indels
    variants.parquet/         one row per variant: hl.variant_qc over all call sets + how many samples each caller
                              called it in
    caller_comparison.csv     per (sample, caller): calls, unique to that caller, shared by all, Ti/Tv, het/hom
                              (each call once, like bcftools stats; hl.sample_qc counts allele copies)
    summary.md                the same as a markdown table, plus the overlap counts

Runs with requirements-cohort.txt (Hail 0.2.139 on Spark 3.5) and Java 11, on Linux or macOS (Hail has no
Windows build): app-cohort/Dockerfile, or the CI job. docs/cohort-qc.md has the details.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass

# caller name -> the pipeline's key prefix for that caller's VCF (app/provenance.py STAGE_VCF).
CALLER_PREFIXES = {
    "bcftools": "variants",
    "deepvariant": "variants_deepvariant",
    "pangenome": "variants_pangenome",
}
CALLERS = tuple(CALLER_PREFIXES)


@dataclass(frozen=True)
class CallSet:
    sample: str
    caller: str
    path: str

    @property
    def column(self) -> str:
        return f"{self.sample}/{self.caller}"


def discover(root: str, samples, callers=CALLERS) -> list[CallSet]:
    """The call sets that exist under a storage root, for the given samples and callers (missing ones are skipped)."""
    found = []
    for sample in samples:
        for caller in callers:
            path = os.path.join(root, CALLER_PREFIXES[caller], f"{sample}.vcf.gz")
            if os.path.exists(path):
                found.append(CallSet(sample, caller, path))
            else:
                print(f"cohort_qc: no {caller} calls for {sample} ({path})", file=sys.stderr)
    return found


def parse_vcf_arg(text: str) -> CallSet:
    """--vcf SAMPLE:CALLER:PATH (PATH may itself contain ':', e.g. a Windows drive or s3://)."""
    parts = text.split(":", 2)
    if len(parts) != 3 or not all(parts):
        raise argparse.ArgumentTypeError(f"expected SAMPLE:CALLER:PATH, got {text!r}")
    if parts[1] not in CALLERS:
        # A typo would otherwise be a fourth caller, and "unique"/"shared by all" would be computed over it.
        raise argparse.ArgumentTypeError(f"caller {parts[1]!r} is not one of {', '.join(CALLERS)}")
    return CallSet(*parts)


def is_pass(filters):
    """PASS in Hail terms: FILTER PASS is an empty set, FILTER '.' is missing. Anything else (RefCall, NoCall,
    LowQual ...) is filtered out."""
    import hail as hl

    return hl.is_missing(filters) | (hl.len(filters) == 0)


def load_call_set(cs: CallSet, reference_genome: str):
    """One VCF as a single-column MatrixTable with a common schema: PASS rows only, multi-allelics split, entries
    reduced to GT and DP (FORMAT/DP where the caller writes it, else the sum of AD: bcftools' calls here carry
    GT:PL:AD), so call sets from different callers can be joined."""
    import hail as hl

    # No skip_invalid_loci: a contig the reference genome doesn't have (the synthetic genome's, b37's "20") fails the
    # import loudly instead of silently dropping every call on it.
    mt = hl.import_vcf(cs.path, reference_genome=reference_genome, force_bgz=cs.path.endswith(".gz"))
    mt = mt.filter_rows(is_pass(mt.filters))
    # Rows down to the key before splitting: no INFO field is used, and per-allele INFO can't be split anyway.
    mt = mt.select_rows().select_globals()
    fields = mt.entry.dtype
    if "DP" in fields:
        dp = hl.int32(mt.DP)
    elif "AD" in fields:
        dp = hl.int32(hl.sum(mt.AD))
    else:
        dp = hl.missing(hl.tint32)
    mt = mt.select_entries(GT=mt.GT, DP=dp)
    # permit_shuffle: bcftools can write a multi-allelic record and another record at the same position (HG003's
    # chr20:136,668: G>T,... and G>GCAGG...). Split, those keys are out of order, which Hail only accepts with a
    # shuffle. It costs a sort, nothing more, on a single-sample VCF.
    mt = hl.split_multi_hts(mt, permit_shuffle=True)
    mt = mt.select_rows()
    # Every VCF names its sample its own way (HG003, HG003_chr20, ...): the column key is the call set instead.
    mt = mt.key_cols_by(s=hl.literal(cs.column))
    mt = mt.annotate_cols(sample=cs.sample, caller=cs.caller)
    return mt.select_cols("sample", "caller")


def combine(call_sets: list[CallSet], reference_genome: str):
    """All call sets as one MatrixTable (outer join on variants: a call set with no call there has a missing GT)."""
    mts = [load_call_set(cs, reference_genome) for cs in call_sets]
    mt = mts[0]
    for other in mts[1:]:
        mt = mt.union_cols(other, row_join_type="outer")
    return mt


def variant_type(alleles):
    import hail as hl

    ref, alt = alleles[0], alleles[1]
    return hl.case().when(hl.is_snp(ref, alt), "SNP").when(hl.is_indel(ref, alt), "indel").default("other")


def analyse(mt, callers):
    """sample_qc per call set, variant_qc per variant, per-variant counts per call set, and the caller overlap.

    hl.sample_qc counts alternate allele *copies* (a hom-var SNP is two SNP alleles, two transitions or
    transversions). The per_call table counts each called variant once, as bcftools stats does: a hom-var is one call.
    Variants are the split ones, so a multi-allelic site counts once per alternate allele (a 1/2 genotype is two het
    calls); about 1% of the real chr20 records are multi-allelic. sample_qc is kept in its own output for everything
    else (call rate, depth, singletons)."""
    import hail as hl

    mt = hl.sample_qc(mt)
    mt = hl.variant_qc(mt)
    called = hl.is_defined(mt.GT) & mt.GT.is_non_ref()
    # How many samples each caller called the variant in (PASS, non-reference).
    mt = mt.annotate_rows(
        variant_type=variant_type(mt.alleles),
        **{f"n_samples_{c}": hl.agg.count_where(called & (mt.caller == c)) for c in callers},
    )

    sample_qc = mt.cols()
    variants = mt.rows()

    entries = mt.entries()
    entries = entries.filter(hl.is_defined(entries.GT) & entries.GT.is_non_ref())
    ref, alt = entries.alleles[0], entries.alleles[1]
    per_call = entries.group_by(entries["sample"], entries.caller).aggregate(
        pass_calls=hl.agg.count(),
        n_het=hl.agg.count_where(entries.GT.is_het()),
        n_hom_var=hl.agg.count_where(entries.GT.is_hom_var()),
        snps=hl.agg.count_where(hl.is_snp(ref, alt)),
        indels=hl.agg.count_where(hl.is_indel(ref, alt)),
        transitions=hl.agg.count_where(hl.is_transition(ref, alt)),
        transversions=hl.agg.count_where(hl.is_transversion(ref, alt)),
    )
    per_site = entries.group_by(entries["sample"], entries.locus, entries.alleles).aggregate(
        callers=hl.sorted(hl.array(hl.agg.collect_as_set(entries.caller)))
    )
    overlap = per_site.group_by(
        per_site["sample"],
        callers=hl.delimit(per_site.callers, "+"),
        variant_type=variant_type(per_site.alleles),
    ).aggregate(n=hl.agg.count())
    return sample_qc, variants, per_call, overlap


COUNTS = ("pass_calls", "n_het", "n_hom_var", "snps", "indels", "transitions", "transversions")


def comparison_rows(call_sets, per_call_df, overlap_df):
    """Per input call set: PASS calls, SNPs, indels, Ti/Tv and het/hom (per variant), plus the calls unique to that
    caller and the calls every caller of that sample made. The callers of a sample are the call sets it was given, so a
    call set with no PASS calls at all still has a row (zeros) and still counts in "shared by all"."""
    counts = {(r["sample"], r["caller"]): r for r in per_call_df.to_dict("records")}
    rows = []
    for cs in sorted(call_sets, key=lambda c: (c.sample, c.caller)):
        col = counts.get((cs.sample, cs.caller), dict.fromkeys(COUNTS, 0))
        if (cs.sample, cs.caller) not in counts:
            print(f"cohort_qc: WARNING: {cs.column} has no PASS calls", file=sys.stderr)
        mine = overlap_df[overlap_df["sample"] == cs.sample]
        n_callers = sum(1 for c in call_sets if c.sample == cs.sample)
        combos = mine["callers"].str.split("+")
        unique = int(mine[combos.apply(lambda c, caller=cs.caller: c == [caller])]["n"].sum())
        shared_all = int(mine[combos.apply(len) == n_callers]["n"].sum()) if n_callers > 1 else None
        rows.append(
            {
                "sample": cs.sample,
                "caller": cs.caller,
                "pass_calls": int(col["pass_calls"]),
                "snps": int(col["snps"]),
                "indels": int(col["indels"]),
                "ti_tv": _ratio(col["transitions"], col["transversions"]),
                "het_hom": _ratio(col["n_het"], col["n_hom_var"]),
                "unique_to_caller": unique,
                "shared_by_all_callers": shared_all,
            }
        )
    return rows


def _ratio(numerator, denominator, digits=3):
    """numerator/denominator rounded, or None (a blank) when the denominator is 0."""
    return round(int(numerator) / int(denominator), digits) if int(denominator) else None


def markdown_table(rows, columns) -> str:
    def cell(v):
        return "" if v is None else str(v)

    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    lines += ["| " + " | ".join(cell(r[c]) for c in columns) + " |" for r in rows]
    return "\n".join(lines)


def write_outputs(out: str, sample_qc, variants, per_call, overlap, call_sets) -> list[dict]:
    """Parquet through Spark (Hail Table -> Spark DataFrame), and the small CSV and markdown summaries."""
    import csv

    os.makedirs(out, exist_ok=True)
    sample_qc.to_spark(flatten=True).write.mode("overwrite").parquet(os.path.join(out, "sample_qc.parquet"))
    overlap.to_spark(flatten=True).write.mode("overwrite").parquet(os.path.join(out, "caller_overlap.parquet"))
    variants.to_spark(flatten=True).write.mode("overwrite").parquet(os.path.join(out, "variants.parquet"))

    overlap_df = overlap.to_pandas()
    rows = comparison_rows(call_sets, per_call.to_pandas(), overlap_df)
    columns = list(rows[0]) if rows else []
    with open(os.path.join(out, "caller_comparison.csv"), "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)

    overlap_rows = overlap_df.sort_values(["sample", "variant_type", "n"], ascending=[True, True, False])
    overlap_rows = overlap_rows.to_dict("records")
    with open(os.path.join(out, "summary.md"), "w", newline="\n") as handle:
        handle.write("# Cohort QC (Hail)\n\n")
        handle.write(f"{len({c.sample for c in call_sets})} samples, {len(call_sets)} call sets, PASS calls only.\n\n")
        handle.write("## Callers per sample\n\n")
        handle.write(markdown_table(rows, columns) + "\n\n")
        handle.write("## Which callers made each call\n\n")
        handle.write(markdown_table(overlap_rows, ["sample", "variant_type", "callers", "n"]) + "\n")
    return rows


def init_hail(cores: int, driver_memory: str, tmp_dir: str | None):
    import hail as hl

    hl.init(
        master=f"local[{cores}]",
        quiet=True,
        tmp_dir=tmp_dir,
        local_tmpdir=tmp_dir,
        log=os.path.join(tmp_dir or ".", "hail.log"),
        spark_conf={"spark.driver.memory": driver_memory, "spark.ui.enabled": "false"},
    )


def run(call_sets: list[CallSet], out: str, reference_genome: str = "GRCh38") -> list[dict]:
    """The whole job on an initialised Hail. Returns the caller-comparison rows."""
    import hail as hl

    if not call_sets:
        raise SystemExit("cohort_qc: no call sets to analyse")
    columns = [c.column for c in call_sets]
    if len(set(columns)) != len(columns):
        raise SystemExit(f"cohort_qc: duplicate sample/caller pairs: {sorted(columns)}")
    callers = sorted({c.caller for c in call_sets})
    # Checkpointed once: the outputs are several Spark actions, and without it each would re-import, split, sort and
    # join every VCF again.
    mt = combine(call_sets, reference_genome).checkpoint(hl.utils.new_temp_file("cohort", "mt"))
    return write_outputs(out, *analyse(mt, callers), call_sets)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", help="storage root with variants*/ (the pipeline's local bucket or a copy)")
    parser.add_argument("--samples", nargs="+", default=[], help="sample IDs to look up under --root")
    parser.add_argument("--callers", nargs="+", default=list(CALLERS), choices=CALLERS)
    parser.add_argument("--vcf", action="append", type=parse_vcf_arg, default=[], help="SAMPLE:CALLER:PATH")
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument(
        "--reference-genome", default="GRCh38", help="Hail reference genome: GRCh38 (chr20), GRCh37 (20) ..."
    )
    parser.add_argument("--cores", type=int, default=4)
    parser.add_argument("--driver-memory", default="4g")
    parser.add_argument("--tmp-dir", default=None, help="Hail/Spark scratch (default: <out>/_tmp)")
    args = parser.parse_args(argv)

    call_sets = list(args.vcf)
    if args.root:
        call_sets += discover(args.root, args.samples, args.callers)
    elif args.samples:
        parser.error("--samples needs --root")
    reference = args.reference_genome

    tmp_dir = args.tmp_dir or os.path.join(args.out, "_tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    init_hail(args.cores, args.driver_memory, tmp_dir)
    rows = run(call_sets, args.out, reference)
    for row in rows:
        print(row)
    print(f"cohort_qc: {len(call_sets)} call sets -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
