"""Read a VCF (gzipped) into table rows, and compare calls against a demo truth file.

Only calls count: filtered records (DeepVariant's RefCall and NoCall) and placeholder ALTs are skipped,
as in run_local.read_vcf_calls. bcftools writes FILTER '.', DeepVariant 'PASS' for its calls."""

import gzip
import io
import json
from dataclasses import dataclass, field

from geyser_web.pipeline import ObjectNotFound


@dataclass(frozen=True)
class Variant:
    chrom: str
    pos: int
    ref: str
    alt: str
    qual: float | None
    depth: int | None
    genotype: str
    kind: str  # SNP | Indel | MNP
    consequence: str = ""  # from bcftools csq (BCSQ), e.g. "missense SYNTHG1"; "" when not annotated


@dataclass
class VariantTable:
    rows: list = field(default_factory=list)  # at most `limit` Variants
    total: int = 0  # every call in the file (filtered records left out)
    calls: set = field(default_factory=set)  # {(pos, ref, alt)} for all records, as run_local.read_vcf_calls
    annotated: bool = False  # the file carries bcftools csq consequences (a BCSQ header line)

    @property
    def truncated(self) -> bool:
        return self.total > len(self.rows)


def _kind(ref: str, alt: str) -> str:
    if len(ref) == 1 and len(alt) == 1:
        return "SNP"
    if len(ref) == len(alt):
        return "MNP"
    return "Indel"


def _info_field(info: str, name: str):
    for item in info.split(";"):
        if item.startswith(name + "="):
            return item.split("=", 1)[1]
    return None


def _consequence(bcsq, alt: str = None) -> str:
    """'missense|SYNTHG1|SYNTHG1T|protein_coding|+|...|100A>G,...' -> 'missense SYNTHG1' (each once).

    alt: keep only the entries for that allele (their last field, the DNA change, ends '>ALT'), so
    each allele of a multi-allelic site shows its own consequences; if none match, all are shown.
    A leading '*' (bcftools: downstream of an earlier stop) or '@' (see another record) is kept off.
    """
    if not bcsq:
        return ""
    entries = bcsq.split(",")
    if alt:
        mine = [e for e in entries if e.split("|")[-1].endswith(">" + alt)]
        entries = mine or entries
    seen = []
    for entry in entries:
        parts = entry.split("|")
        if len(parts) < 2 or not parts[0].lstrip("*@"):
            continue
        label = f"{parts[0].lstrip('*@')} {parts[1]}".strip()
        if label not in seen:
            seen.append(label)
    return ", ".join(seen)


def parse_vcf(lines, limit: int = 500) -> VariantTable:
    table = VariantTable()
    for line in lines:
        if line.startswith("##INFO=<ID=BCSQ,"):
            table.annotated = True
        if not line or line.startswith("#"):
            continue
        cols = line.rstrip("\r\n").split("\t")
        if len(cols) < 8:
            continue
        chrom, pos, ref, alts, qual, info = cols[0], int(cols[1]), cols[3], cols[4], cols[5], cols[7]
        if cols[6] not in ("PASS", "."):
            continue  # a candidate the caller rejected (DeepVariant RefCall / NoCall), not a call
        genotype = ""
        if len(cols) >= 10:
            fmt, sample = cols[8].split(":"), cols[9].split(":")
            if "GT" in fmt and fmt.index("GT") < len(sample):
                genotype = sample[fmt.index("GT")]
        depth = _info_field(info, "DP")
        bcsq = _info_field(info, "BCSQ")
        for alt in alts.split(","):
            if alt in (".", "<*>", "*"):
                continue  # no alternative allele: a placeholder, not a call
            table.total += 1
            table.calls.add((pos, ref, alt))
            if len(table.rows) < limit:
                table.rows.append(
                    Variant(
                        chrom=chrom,
                        pos=pos,
                        ref=ref,
                        alt=alt,
                        qual=None if qual == "." else round(float(qual), 1),
                        depth=int(depth) if depth and depth.isdigit() else None,
                        genotype=genotype,
                        kind=_kind(ref, alt),
                        consequence=_consequence(bcsq, alt),
                    )
                )
    return table


def read_variants(storage, key: str, limit: int = 500):
    """Parse a gzipped VCF from storage; None if it doesn't exist."""
    try:
        source = storage.open_read(key)
    except ObjectNotFound:
        return None
    try:
        with gzip.GzipFile(fileobj=source, mode="rb") as gz:
            return parse_vcf(io.TextIOWrapper(gz, encoding="utf-8", errors="replace"), limit)
    finally:
        source.close()


@dataclass
class TruthCheck:
    total: int
    recovered: int
    missed: list
    extra: list

    @property
    def perfect(self) -> bool:
        return not self.missed and not self.extra


def truth_key(sample_id: str) -> str:
    """Written by scripts/make_test_data.py: the SNPs planted in a synthetic sample."""
    return f"truth/{sample_id}.json"


def read_truth(storage, sample_id: str):
    try:
        with storage.open_read(truth_key(sample_id)) as fh:
            return json.loads(fh.read().decode("utf-8"))
    except (ObjectNotFound, ValueError):
        return None


def check_truth(calls: set, truth: dict) -> TruthCheck:
    """Same comparison as run_local.check_against_truth: planted (pos, ref, alt) vs called."""
    planted = {(s["pos"], s["ref"], s["alt"]) for s in truth.get("snps", [])}
    return TruthCheck(
        total=len(planted),
        recovered=len(planted & calls),
        missed=sorted(planted - calls),
        extra=sorted(calls - planted),
    )


def planted_positions(truth) -> set:
    return {(s["pos"], s["ref"], s["alt"]) for s in (truth or {}).get("snps", [])}
