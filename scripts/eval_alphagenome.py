#!/usr/bin/env python3
"""Evaluate AlphaGenome annotations of a sample's calls against fine-mapped eQTLs (Geyser 3.0, docs/design/alphagenome.md).

Question: do AlphaGenome's scores rank the sample's variants that are fine-mapped eQTLs above comparable variants
that are not? For each score it reports the AUROC (fine-mapped eQTL vs matched non-coding controls) with a
bootstrap 95% CI, plus a csq-vs-AlphaGenome overlap table and coverage/cost figures.

    python scripts/eval_alphagenome.py --annotated stats/S1/alphagenome.tsv \\
        --eqtl QTD000116.credible_sets.tsv.gz --eqtl QTD000121.credible_sets.tsv.gz \\
        --tss tss_chr20.tsv --af af_chr20.tsv --out-json eval.json --out-md eval.md

**Scores** (``--score NAME[:abs]``, repeatable; the first is the primary). Defaults follow the annotate stage's
interface contract (#207): for its TSV ``rna_lfc:abs`` then ``avi``; for its VCF ``AG_RNA_LFC:abs`` then ``AG_AVI``.
- ``rna_lfc`` / ``AG_RNA_LFC`` with ``:abs`` (default primary): |log fold change| of predicted expression
  (AlphaGenome's RNA_SEQ scorer) for the gene of the strongest effect, the score that matches what an eQTL is.
- ``avi`` / ``AG_AVI`` (default secondary): the AlphaGenome Variant Impact score, PHRED-scaled. **AVI is a deleteriousness score trained
  to separate variants with gnomAD AF > 0.1% from rarer ones** (plus conservation and AlphaMissense; Cheng et al.
  2026). Fine-mapped eQTLs are mostly common variants, so AVI's AUROC here measures allele frequency as much as
  regulation unless the controls are matched on AF. The report says so whenever an AVI score is evaluated.

**Inputs**
- ``--annotated``: the annotate stage's output (#207): ``stats/{id}/alphagenome.tsv`` (lines starting with ``#``,
  such as its terms notice, are skipped; columns ``chrom pos ref alt ... avi avi_raw top_track gene rna_lfc csq
  source``, ``csq`` comma-separated) or ``variants/{id}.alphagenome.vcf.gz`` (INFO ``AG_AVI``, ``AG_AVI_RAW``,
  ``AG_TOP_TRACK``, ``AG_GENE``, ``AG_RNA_LFC``, ``AG_SOURCE``, ``BCSQ``). An optional ``af`` column (TSV) or
  ``AF`` INFO key is used for matching. Rename any column with ``--col ROLE=NAME`` (roles: chrom pos ref alt gene
  source af csq).
- ``--eqtl`` (repeatable, e.g. one per GTEx tissue): fine-mapped eQTLs in eQTL Catalogue's SuSiE credible-set format (GRCh38): columns ``variant``
  (chrN_pos_ref_alt) or ``chromosome``/``position``/``ref``/``alt``, plus ``pip`` and ``molecular_trait_id``.
  Positives are the sample's calls with PIP >= ``--min-pip`` (default 0.9); every variant in any credible set is
  excluded from the controls.
- ``--tss`` (optional): TSV ``chrom  pos  gene`` of transcription start sites, for matching on distance to the
  nearest TSS.
- ``--af`` (optional): TSV ``chrom  pos  ref  alt  af`` of population allele frequencies (e.g. the ``maf`` of eQTL
  Catalogue's summary statistics for the same tissue), or an AF column in ``--annotated``. Without either, the
  controls are **not** matched on AF, and the report says so.
- ``--run-record`` (optional): the annotate stage's run-record part (JSON), for API calls, time and cost.

**Comparisons** (``--delong``): the primary score against each other score, by DeLong's paired test for correlated
AUROCs (DeLong, DeLong & Clarke-Pearson 1988, Biometrics 44:837; fast form: Sun & Xu 2014, IEEE Signal Process Lett
21:1389), on the positives and controls that have both scores, with Holm-adjusted p-values across the comparisons.
``neg_tss_distance`` is a built-in baseline score (minus the distance to the nearest TSS; needs ``--tss``). To let it
compete, match on allele frequency only: ``--match af``.

**Controls**: the sample's calls that are in no credible set and are non-coding by bcftools csq (no protein-level
consequence). Each positive gets up to ``--controls-per-case`` (default 5) controls from the same stratum
(``--match``: distance-to-TSS bin x AF bin by default, whichever are available), drawn without replacement with a fixed ``--seed``.
Positives with too few controls in their stratum are counted in the report.

**Fetching the eQTL truth set** (not run by this script; do it only once the licence is confirmed, see the design
note). eQTL Catalogue, SuSiE credible sets for a GTEx v8 tissue (study QTS000015), CC BY 4.0, GRCh38;
cite Kerimov et al. 2021, Nat Genet, doi:10.1038/s41588-021-00924-w:

    curl -fO https://ftp.ebi.ac.uk/pub/databases/spot/eQTL/susie/QTS000015/QTD000116/QTD000116.credible_sets.tsv.gz

(QTD000116 = adipose subcutaneous; the dataset list is tabix/tabix_ftp_paths.tsv in
github.com/eQTL-Catalogue/eQTL-Catalogue-resources.) AlphaGenome outputs are for research use only, never for
clinical decisions, and must not be used to train another model: this script computes AUROCs of the raw scores
and trains nothing.
"""

import argparse
import bisect
import csv
import gzip
import json
import math
import random
import sys

# Defaults per input format, from the annotate stage's interface contract (#207).
DEFAULTS = {
    "tsv": {
        "scores": ["rna_lfc:abs", "avi"],
        "flag": "avi>=20",
        "columns": {
            "chrom": "chrom",
            "pos": "pos",
            "ref": "ref",
            "alt": "alt",
            "gene": "gene",
            "source": "source",
            "af": "af",
            "csq": "csq",
        },
    },
    "vcf": {
        "scores": ["AG_RNA_LFC:abs", "AG_AVI"],
        "flag": "AG_AVI>=20",
        "columns": {
            "chrom": "CHROM",
            "pos": "POS",
            "ref": "REF",
            "alt": "ALT",
            "gene": "AG_GENE",
            "source": "AG_SOURCE",
            "af": "AF",
            "csq": "BCSQ",
        },
    },
}
ROLES = sorted(DEFAULTS["tsv"]["columns"])
# bcftools csq consequences that change the protein (or its splicing at the canonical sites). Anything else, or no
# consequence at all (intronic and intergenic variants get none), counts as non-coding here.
PROTEIN_LEVEL = {
    "missense",
    "stop_gained",
    "stop_lost",
    "start_lost",
    "frameshift",
    "inframe_insertion",
    "inframe_deletion",
    "inframe_altering",
    "splice_acceptor",
    "splice_donor",
    "start_retained",
    "stop_retained",
    "coding_sequence",
}
TSS_SCORE = "neg_tss_distance"  # built-in baseline: closer to a TSS ranks higher
MATCH_CHOICES = ("auto", "tss+af", "tss", "af", "none")
# Distance to the nearest TSS (bp) and allele frequency: bin upper bounds (the last bin is open-ended).
TSS_BINS = [1_000, 5_000, 20_000, 100_000, 500_000]
AF_BINS = [0.01, 0.05, 0.1, 0.25, 0.5]
AVI_CONFOUND = (
    "AVI is a deleteriousness score trained to separate common (gnomAD AF > 0.1%) from rarer variants, and "
    "fine-mapped eQTLs are mostly common: unless the controls are matched on AF, its AUROC here reflects allele "
    "frequency as much as regulatory effect."
)
AVI_CONFOUND_MATCHED = (
    "AVI is a deleteriousness score trained to separate common (gnomAD AF > 0.1%) from rarer variants. The controls "
    "here are matched on allele frequency, which removes the main confound, but AVI still is not a regulatory score."
)
RUN_RECORD_KEYS = (
    "source",
    "model_version",
    "variants_selected",
    "variants_scored",
    "api_calls",
    "seconds",
    "cost_usd",
)


def open_text(path):
    return gzip.open(path, "rt", encoding="utf-8") if path.endswith(".gz") else open(path, encoding="utf-8")


def norm_chrom(chrom):
    """'20', 'chr20', 'CHR20' -> 'chr20' (GRCh38 UCSC-style, as the pipeline's reference)."""
    chrom = str(chrom).strip()
    return "chr" + chrom[3:] if chrom.lower().startswith("chr") else "chr" + chrom


def variant_key(chrom, pos, ref, alt):
    return (norm_chrom(chrom), int(pos), ref.upper(), alt.upper())


def parse_float(value):
    if value is None:
        return None
    value = str(value).strip()
    if value in ("", ".", "NA", "nan", "NaN", "None"):
        return None
    try:
        out = float(value)
    except ValueError:
        return None
    return None if math.isnan(out) else out


def parse_score_spec(spec):
    """'AG_RNA_LFC:abs' -> ('AG_RNA_LFC', True)."""
    name, _, mod = spec.partition(":")
    if mod not in ("", "abs"):
        raise ValueError(f"unknown score modifier in {spec!r} (only ':abs')")
    return name, mod == "abs"


def bcsq_consequence(value):
    """First consequence of a bcftools csq INFO/BCSQ value ('missense|GENE|ENST..|...' -> 'missense'). '@<pos>'
    back-references (a compound consequence recorded at another site) count as missense. Empty -> None."""
    if not value or value == ".":
        return None
    first = value.split(",")[0]
    if first.startswith("@"):
        return "missense"
    csq = first.split("|")[0]
    return csq.lstrip("*") or None


def is_protein_level(consequence):
    """True when any of the consequences (comma- or '&'-separated, as csq writes them) changes the protein."""
    if not consequence:
        return False
    return any(part.strip().lstrip("*") in PROTEIN_LEVEL for part in consequence.replace("&", ",").split(","))


def input_format(path):
    return "vcf" if path.endswith((".vcf", ".vcf.gz")) else "tsv"


def read_annotated(path, columns, score_names):
    """Annotated calls -> list of dicts: key, gene, source, af, csq, scores {name: float|None}."""
    if input_format(path) == "vcf":
        return list(_read_vcf(path, columns, score_names))
    return list(_read_tsv(path, columns, score_names))


def _read_tsv(path, columns, score_names):
    with open_text(path) as fh:
        # The annotate TSV starts with a '#' terms-notice line: skip comment lines before the header.
        reader = csv.DictReader((line for line in fh if not line.startswith("#")), delimiter="\t")
        missing = [columns[r] for r in ("chrom", "pos", "ref", "alt") if columns[r] not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path}: missing column(s) {missing}")
        for row in reader:
            yield {
                "key": variant_key(
                    row[columns["chrom"]], row[columns["pos"]], row[columns["ref"]], row[columns["alt"]]
                ),
                "gene": (row.get(columns["gene"]) or "").strip() or None,
                "source": (row.get(columns["source"]) or "").strip() or None,
                "af": parse_float(row.get(columns["af"])),
                "csq": (row.get(columns["csq"]) or "").strip() or None,
                "scores": {name: parse_float(row.get(name)) for name in score_names},
            }


def _read_vcf(path, columns, score_names):
    with open_text(path) as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            fields = line.rstrip("\n").split("\t")
            chrom, pos, _id, ref, alts, _qual, filt, info = fields[:8]
            if filt not in ("PASS", "."):
                continue
            tags = {}
            for item in info.split(";"):
                k, _, v = item.partition("=")
                tags[k] = v
            for i, alt in enumerate(alts.split(",")):
                yield {
                    "key": variant_key(chrom, pos, ref, alt),
                    "gene": _allele_value(tags, columns["gene"], i),
                    "source": _allele_value(tags, columns["source"], i),
                    "af": parse_float(_allele_value(tags, columns["af"], i)),
                    "csq": bcsq_consequence(tags.get(columns["csq"])),
                    "scores": {name: parse_float(_allele_value(tags, name, i)) for name in score_names},
                }


def _allele_value(tags, name, idx):
    """INFO value for the idx-th ALT allele (Number=A), or the single value when there is only one."""
    v = tags.get(name)
    if v is None:
        return None
    parts = v.split(",")
    return parts[idx] if len(parts) > idx else parts[0]


def read_eqtls(paths):
    """Credible-set tables (one or more, e.g. per tissue) -> {variant key: (max PIP over all, set of genes)}."""
    out = {}
    for path in [paths] if isinstance(paths, str) else paths:
        _read_eqtl_file(path, out)
    return out


def _read_eqtl_file(path, out):
    with open_text(path) as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        for row in reader:
            if row.get("variant"):
                chrom, pos, ref, alt = row["variant"].split("_", 3)
            else:
                chrom, pos, ref, alt = row["chromosome"], row["position"], row["ref"], row["alt"]
            key = variant_key(chrom, pos, ref, alt)
            pip = parse_float(row.get("pip")) or 0.0
            gene = (row.get("molecular_trait_id") or "").split(".")[0] or None
            best, genes = out.get(key, (0.0, set()))
            if gene:
                genes = genes | {gene}
            out[key] = (max(best, pip), genes)


def read_tss(path):
    """TSV chrom pos [gene] -> {chrom: sorted positions}."""
    tss = {}
    with open_text(path) as fh:
        for row in csv.reader(fh, delimiter="\t"):
            if not row or row[0].startswith("#") or row[0].lower() == "chrom":
                continue
            tss.setdefault(norm_chrom(row[0]), []).append(int(row[1]))
    return {c: sorted(p) for c, p in tss.items()}


def read_af(path):
    """TSV chrom pos ref alt af -> {variant key: af}."""
    out = {}
    with open_text(path) as fh:
        for row in csv.reader(fh, delimiter="\t"):
            if not row or row[0].startswith("#") or row[0].lower() == "chrom":
                continue
            af = parse_float(row[4])
            if af is not None:
                out[variant_key(*row[:4])] = af
    return out


def read_gene_names(path):
    """Gene metadata with gene_id and gene_name columns (e.g. eQTL Catalogue's phenotype metadata) ->
    {gene name: set of Ensembl gene IDs}. AlphaGenome reports gene names where they exist; eQTL tables use IDs."""
    out = {}
    with open_text(path) as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            name, gene_id = (row.get("gene_name") or "").strip(), (row.get("gene_id") or "").split(".")[0]
            if name and gene_id:
                out.setdefault(name, set()).add(gene_id)
    return out


def gene_ids(gene, names):
    """AlphaGenome's gene (an Ensembl ID or a name) -> the Ensembl IDs it stands for."""
    gene = gene.split(".")[0] if gene.startswith("ENSG") else gene
    return {gene} if gene.startswith("ENSG") else names.get(gene, set())


def tss_distance(tss, key):
    positions = tss.get(key[0])
    if not positions:
        return None
    i = bisect.bisect_left(positions, key[1])
    near = [abs(positions[j] - key[1]) for j in (i - 1, i) if 0 <= j < len(positions)]
    return min(near)


def bin_of(value, bounds):
    if value is None:
        return None
    return bisect.bisect_left(bounds, value)


def auroc(pos_scores, neg_scores):
    """Mann-Whitney AUROC with average ranks for ties: P(score of a positive > score of a control), ties count half.
    None when either group is empty."""
    if not pos_scores or not neg_scores:
        return None
    labelled = sorted([(s, 1) for s in pos_scores] + [(s, 0) for s in neg_scores])
    rank_sum = 0.0
    i = 0
    while i < len(labelled):
        j = i
        while j + 1 < len(labelled) and labelled[j + 1][0] == labelled[i][0]:
            j += 1
        avg_rank = (i + j) / 2 + 1
        rank_sum += avg_rank * sum(1 for k in range(i, j + 1) if labelled[k][1] == 1)
        i = j + 1
    n_pos, n_neg = len(pos_scores), len(neg_scores)
    return (rank_sum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def bootstrap_ci(pos_scores, neg_scores, n_boot, seed, level=0.95):
    """Percentile CI of the AUROC, resampling positives and controls independently with replacement."""
    if not pos_scores or not neg_scores or n_boot <= 0:
        return None
    rng = random.Random(seed)
    values = sorted(
        auroc(rng.choices(pos_scores, k=len(pos_scores)), rng.choices(neg_scores, k=len(neg_scores)))
        for _ in range(n_boot)
    )
    lo = values[int(math.floor((1 - level) / 2 * (n_boot - 1)))]
    hi = values[int(math.ceil((1 + level) / 2 * (n_boot - 1)))]
    return lo, hi


def midranks(values):
    """1-based ranks with ties given their average rank (the midrank)."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def _delong_components(pos, neg):
    """AUROC and DeLong's structural components (V10 per positive, V01 per control), via midranks (Sun & Xu 2014)."""
    m, n = len(pos), len(neg)
    tz = midranks(list(pos) + list(neg))
    tx, ty = midranks(list(pos)), midranks(list(neg))
    auc = (sum(tz[:m]) / m - (m + 1) / 2) / n
    v10 = [(tz[i] - tx[i]) / n for i in range(m)]  # P(positive i > a control), ties half
    v01 = [1 - (tz[m + j] - ty[j]) / m for j in range(n)]  # P(control j < a positive), ties half
    return auc, v10, v01


def _cov(a, b):
    k = len(a)
    if k < 2:
        return 0.0
    ma, mb = sum(a) / k, sum(b) / k
    return sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True)) / (k - 1)


def delong_paired(pos_a, neg_a, pos_b, neg_b):
    """DeLong's test for two correlated AUROCs: scores A and B on the same positives and the same controls (lists
    aligned item by item). Returns auc_a, auc_b, delta (A - B), its standard error, z and the two-sided p-value."""
    if len(pos_a) != len(pos_b) or len(neg_a) != len(neg_b):
        raise ValueError("DeLong's paired test needs both scores on the same positives and controls")
    if len(pos_a) < 2 or len(neg_a) < 2:
        return None
    auc_a, v10a, v01a = _delong_components(pos_a, neg_a)
    auc_b, v10b, v01b = _delong_components(pos_b, neg_b)
    m, n = len(pos_a), len(neg_a)
    var = (_cov(v10a, v10a) + _cov(v10b, v10b) - 2 * _cov(v10a, v10b)) / m + (
        _cov(v01a, v01a) + _cov(v01b, v01b) - 2 * _cov(v01a, v01b)
    ) / n
    delta = auc_a - auc_b
    se = math.sqrt(var) if var > 0 else 0.0
    if se == 0.0:
        z, p = (0.0, 1.0) if delta == 0 else (math.copysign(math.inf, delta), 0.0)
    else:
        z = delta / se
        p = math.erfc(abs(z) / math.sqrt(2))
    return {"auc_a": auc_a, "auc_b": auc_b, "delta": delta, "se": se, "z": z, "p": p}


def delong_variance(pos, neg):
    """DeLong's variance of a single AUROC (for checks and CIs)."""
    _, v10, v01 = _delong_components(pos, neg)
    return _cov(v10, v10) / len(pos) + _cov(v01, v01) / len(neg)


def holm(pvalues):
    """Holm's step-down adjustment (monotone, capped at 1), in the input order. None entries stay None."""
    idx = sorted((i for i, p in enumerate(pvalues) if p is not None), key=lambda i: pvalues[i])
    k = len(idx)
    out = [None] * len(pvalues)
    running = 0.0
    for rank, i in enumerate(idx):
        running = max(running, min(1.0, (k - rank) * pvalues[i]))
        out[i] = running
    return out


def match_controls(positives, candidates, stratum, per_case, seed):
    """Draw up to per_case controls per positive from candidates in the same stratum, without replacement.
    Returns (controls, positives short of controls)."""
    rng = random.Random(seed)
    pools = {}
    for v in sorted(candidates, key=lambda v: v["key"]):
        pools.setdefault(stratum(v), []).append(v)
    for pool in pools.values():
        rng.shuffle(pool)
    controls, short = [], 0
    for v in sorted(positives, key=lambda v: v["key"]):
        pool = pools.get(stratum(v), [])
        take = pool[:per_case]
        del pool[:per_case]
        controls.extend(take)
        if len(take) < per_case:
            short += 1
    return controls, short


def parse_flag(spec):
    """'AG_AVI_PHRED>=20' -> ('AG_AVI_PHRED', 20.0, abs?)."""
    name, sep, value = spec.partition(">=")
    if not sep:
        raise ValueError(f"flag must look like NAME>=VALUE or NAME:abs>=VALUE, got {spec!r}")
    name, use_abs = parse_score_spec(name)
    return name, float(value), use_abs


def overlap_table(variants, flag):
    """csq protein-level (yes/no) x AlphaGenome flagged (yes/no), over variants that have the flag's score."""
    name, threshold, use_abs = flag
    table = {"csq_and_ag": 0, "csq_only": 0, "ag_only": 0, "neither": 0, "no_score": 0}
    for v in variants:
        score = v["scores"].get(name)
        if score is None:
            table["no_score"] += 1
            continue
        flagged = (abs(score) if use_abs else score) >= threshold
        coding = is_protein_level(v["csq"])
        table[
            {(True, True): "csq_and_ag", (True, False): "csq_only", (False, True): "ag_only"}.get(
                (coding, flagged), "neither"
            )
        ] += 1
    return table


def run_record_summary(path):
    if not path:
        return None
    with open(path, encoding="utf-8") as fh:
        record = json.load(fh)
    found = {}
    for scope in (record, record.get("outputs") or {}, record.get("stats") or {}, record.get("annotate") or {}):
        if isinstance(scope, dict):
            for k in RUN_RECORD_KEYS:
                if k in scope and k not in found:
                    found[k] = scope[k]
    return found


def evaluate(args):
    defaults = DEFAULTS[input_format(args.annotated)]
    score_specs = [parse_score_spec(s) for s in (args.score or defaults["scores"])]
    columns = dict(defaults["columns"])
    for item in args.col or []:
        role, _, name = item.partition("=")
        if role not in columns or not name:
            raise ValueError(f"--col needs ROLE=NAME with ROLE in {ROLES}, got {item!r}")
        columns[role] = name
    args.flag = args.flag or defaults["flag"]
    flag = parse_flag(args.flag)
    names = sorted({n for n, _ in score_specs} | {flag[0]})
    if args.match not in MATCH_CHOICES:
        raise ValueError(f"--match must be one of {MATCH_CHOICES}")
    if (TSS_SCORE in names or args.match in ("tss+af", "tss")) and not args.tss:
        raise ValueError(f"{TSS_SCORE} and --match {args.match} need --tss")

    variants = read_annotated(args.annotated, columns, names)
    eqtls = read_eqtls(args.eqtl)
    tss = read_tss(args.tss) if args.tss else None
    af_table = read_af(args.af) if args.af else {}
    for v in variants:
        if v["af"] is None and v["key"] in af_table:
            v["af"] = af_table[v["key"]]
        dist = tss_distance(tss, v["key"]) if tss else None
        v["tss_bin"] = bin_of(dist, TSS_BINS)
        v["af_bin"] = bin_of(v["af"], AF_BINS)
        if TSS_SCORE in names:
            v["scores"][TSS_SCORE] = None if dist is None else -float(dist)

    positives = [v for v in variants if v["key"] in eqtls and eqtls[v["key"]][0] >= args.min_pip]
    candidates = [v for v in variants if v["key"] not in eqtls and not is_protein_level(v["csq"])]
    matched_on = []
    match_tss = tss is not None and args.match in ("auto", "tss+af", "tss")
    if match_tss:
        matched_on.append("distance to nearest TSS")
    use_af = (
        args.match in ("auto", "tss+af", "af")
        and any(v["af"] is not None for v in positives)
        and any(v["af"] is not None for v in candidates)
    )
    if use_af:
        matched_on.append("allele frequency")
        positives_af = [v for v in positives if v["af"] is not None]
        dropped_no_af = len(positives) - len(positives_af)
        positives = positives_af
        candidates = [v for v in candidates if v["af"] is not None]
    else:
        dropped_no_af = 0

    def stratum(v):
        return (v["tss_bin"] if match_tss else None, v["af_bin"] if use_af else None)

    controls, short = match_controls(positives, candidates, stratum, args.controls_per_case, args.seed)

    scores = []
    for i, (name, use_abs) in enumerate(score_specs):

        def value(v, name=name, use_abs=use_abs):
            s = v["scores"].get(name)
            return None if s is None else (abs(s) if use_abs else s)

        pos = [s for s in map(value, positives) if s is not None]
        neg = [s for s in map(value, controls) if s is not None]
        entry = {
            "score": name + (":abs" if use_abs else ""),
            "role": "primary" if i == 0 else "secondary",
            "positives_scored": len(pos),
            "positives_missing": len(positives) - len(pos),
            "controls_scored": len(neg),
            "controls_missing": len(controls) - len(neg),
            "auroc": auroc(pos, neg),
            "ci95": bootstrap_ci(pos, neg, args.bootstrap, args.seed),
        }
        if "AVI" in name.upper():
            entry["caveat"] = AVI_CONFOUND_MATCHED if use_af else AVI_CONFOUND
        scores.append(entry)

    comparisons = []
    if args.delong and len(score_specs) > 1:

        def val(v, spec):
            raw = v["scores"].get(spec[0])
            return None if raw is None else (abs(raw) if spec[1] else raw)

        primary = score_specs[0]
        for other in score_specs[1:]:
            pos = [(val(v, primary), val(v, other)) for v in positives]
            neg = [(val(v, primary), val(v, other)) for v in controls]
            pos = [x for x in pos if None not in x]
            neg = [x for x in neg if None not in x]
            res = delong_paired([a for a, _ in pos], [a for a, _ in neg], [b for _, b in pos], [b for _, b in neg])
            comparisons.append(
                {
                    "a": scores[0]["score"],
                    "b": other[0] + (":abs" if other[1] else ""),
                    "positives": len(pos),
                    "controls": len(neg),
                    **(res or {"auc_a": None, "auc_b": None, "delta": None, "se": None, "z": None, "p": None}),
                }
            )
        for c, adj in zip(comparisons, holm([c["p"] for c in comparisons]), strict=True):
            c["p_holm"] = adj

    gene_agreement = None
    with_gene = [v for v in positives if v["gene"]]
    if with_gene:
        names = read_gene_names(args.gene_names) if args.gene_names else {}
        resolved = [v for v in with_gene if gene_ids(v["gene"], names)]
        hits = sum(1 for v in resolved if gene_ids(v["gene"], names) & eqtls[v["key"]][1])
        gene_agreement = {
            "positives_with_ag_gene": len(with_gene),
            "ag_gene_resolved_to_ensembl": len(resolved),
            "ag_gene_is_eqtl_gene": hits,
        }

    scored_any = [v for v in variants if any(v["scores"].get(n) is not None for n, _ in score_specs)]
    sources = {}
    for v in variants:
        sources[v["source"] or "none"] = sources.get(v["source"] or "none", 0) + 1
    return {
        "inputs": {
            "annotated": args.annotated,
            "eqtl": args.eqtl,
            "tss": args.tss,
            "af": args.af,
            "gene_names": args.gene_names,
        },
        "min_pip": args.min_pip,
        "match": args.match,
        "matched_on": matched_on or ["nothing (no TSS table, no allele frequencies)"],
        "af_matched": use_af,
        "controls_per_case": args.controls_per_case,
        "seed": args.seed,
        "bootstrap": args.bootstrap,
        "counts": {
            "variants": len(variants),
            "positives": len(positives),
            "positives_dropped_no_af": dropped_no_af,
            "control_candidates": len(candidates),
            "controls": len(controls),
            "positives_short_of_controls": short,
        },
        "scores": scores,
        "comparisons": comparisons,
        "gene_agreement": gene_agreement,
        "overlap": {"flag": args.flag, **overlap_table(variants, flag)},
        "coverage": {
            "variants_with_any_score": len(scored_any),
            "share_with_any_score": round(len(scored_any) / len(variants), 4) if variants else None,
            "per_score": {n: sum(1 for v in variants if v["scores"].get(n) is not None) for n in names},
            "by_source": sources,
        },
        "run_record": run_record_summary(args.run_record),
    }


def fmt(x, digits=3):
    return "–" if x is None else f"{x:.{digits}f}"


def to_markdown(result):
    c = result["counts"]
    lines = [
        "# AlphaGenome evaluation: fine-mapped eQTLs vs matched controls",
        "",
        f"- Positives: {c['positives']} calls with PIP >= {result['min_pip']}; controls: {c['controls']} non-coding "
        f"calls in no credible set ({result['controls_per_case']} per positive, seed {result['seed']}).",
        f"- Matched on: {', '.join(result['matched_on'])}."
        + ("" if result["af_matched"] else " **Not matched on allele frequency.**"),
        f"- Positives short of controls: {c['positives_short_of_controls']}; dropped for no AF: "
        f"{c['positives_dropped_no_af']}.",
        "",
        f"| Score | Role | AUROC | 95% CI ({result['bootstrap']} bootstraps) | Positives scored | Controls scored |",
        "| :--- | :--- | ---: | :--- | ---: | ---: |",
    ]
    for s in result["scores"]:
        ci = s["ci95"]
        lines.append(
            f"| `{s['score']}` | {s['role']} | {fmt(s['auroc'])} | "
            f"{'–' if ci is None else fmt(ci[0]) + ' to ' + fmt(ci[1])} | "
            f"{s['positives_scored']} ({s['positives_missing']} missing) | "
            f"{s['controls_scored']} ({s['controls_missing']} missing) |"
        )
    if result.get("comparisons"):
        lines += [
            "",
            "**Paired comparisons** (DeLong; Holm-adjusted across the comparisons; same positives and controls):",
            "",
            "| Primary | vs | AUROC primary | AUROC other | Δ | SE | z | p | p (Holm) | n pos / ctrl |",
            "| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for cmp in result["comparisons"]:
            lines.append(
                f"| `{cmp['a']}` | `{cmp['b']}` | {fmt(cmp['auc_a'])} | {fmt(cmp['auc_b'])} | {fmt(cmp['delta'])} | "
                f"{fmt(cmp['se'])} | {fmt(cmp['z'], 2)} | {fmt(cmp['p'], 4)} | {fmt(cmp['p_holm'], 4)} | "
                f"{cmp['positives']} / {cmp['controls']} |"
            )
    caveats = [s["caveat"] for s in result["scores"] if s.get("caveat")]
    if caveats:
        lines += ["", f"**Caveat:** {caveats[0]}"]
    g = result["gene_agreement"]
    if g:
        lines += [
            "",
            f"AlphaGenome's top gene is one of the eQTL's genes for {g['ag_gene_is_eqtl_gene']} of "
            f"{g['ag_gene_resolved_to_ensembl']} positives whose gene could be matched to an Ensembl ID "
            f"({g['positives_with_ag_gene']} had a gene).",
        ]
    o = result["overlap"]
    lines += [
        "",
        f"**csq vs AlphaGenome** (AlphaGenome flag: `{o['flag']}`; {o['no_score']} calls without that score):",
        "",
        "| | AlphaGenome flags it | AlphaGenome doesn't |",
        "| :--- | ---: | ---: |",
        f"| csq: protein-level consequence | {o['csq_and_ag']} | {o['csq_only']} |",
        f"| csq: none (non-coding) | {o['ag_only']} | {o['neither']} |",
    ]
    cov = result["coverage"]
    lines += [
        "",
        f"**Coverage:** {cov['variants_with_any_score']} of {c['variants']} calls have a score "
        f"({fmt(cov['share_with_any_score'], 4) if cov['share_with_any_score'] is not None else '–'}); "
        f"per score: {', '.join(f'{k} {v}' for k, v in cov['per_score'].items())}; "
        f"by source: {', '.join(f'{k} {v}' for k, v in sorted(cov['by_source'].items()))}.",
    ]
    rr = result["run_record"]
    if rr:
        lines.append("**Run record:** " + ", ".join(f"{k} {v}" for k, v in rr.items()) + ".")
    lines += ["", "AlphaGenome outputs: research use only, not for clinical decisions."]
    return "\n".join(lines) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--annotated", required=True, help="annotated calls: TSV with header, or VCF/VCF.gz")
    p.add_argument(
        "--eqtl", required=True, action="append", help="fine-mapped eQTLs, eQTL Catalogue credible-set TSV (GRCh38)"
    )
    p.add_argument("--tss", help="TSV chrom pos [gene] of TSSs, to match on distance to the nearest TSS")
    p.add_argument("--af", help="TSV chrom pos ref alt af, to match on allele frequency")
    p.add_argument(
        "--gene-names",
        help="gene metadata TSV with gene_id and gene_name (e.g. eQTL Catalogue phenotype metadata), so AlphaGenome's "
        "gene names can be compared with the eQTL table's Ensembl IDs",
    )
    p.add_argument("--run-record", help="the annotate stage's run-record part (JSON), for API calls, time, cost")
    p.add_argument(
        "--score",
        action="append",
        help="NAME[:abs], repeatable; first = primary (default: TSV rna_lfc:abs, avi; VCF AG_RNA_LFC:abs, AG_AVI)",
    )
    p.add_argument(
        "--col",
        action="append",
        help="ROLE=NAME to rename an input column (roles: " + ", ".join(ROLES) + ")",
    )
    p.add_argument("--flag", help="AlphaGenome flag for the csq overlap table (default: AVI PHRED >= 20)")
    p.add_argument("--min-pip", type=float, default=0.9)
    p.add_argument("--controls-per-case", type=int, default=5)
    p.add_argument("--bootstrap", type=int, default=1000)
    p.add_argument("--seed", type=int, default=20261104)
    p.add_argument(
        "--match",
        default="auto",
        choices=MATCH_CHOICES,
        help="what controls are matched on (auto: TSS distance and AF, whichever are given)",
    )
    p.add_argument(
        "--delong",
        action="store_true",
        help="compare the primary score with each other score (DeLong paired test, Holm-adjusted)",
    )
    p.add_argument("--out-json")
    p.add_argument("--out-md")
    args = p.parse_args(argv)
    try:
        result = evaluate(args)
    except (OSError, ValueError, KeyError) as exc:
        print(f"eval_alphagenome: {exc}", file=sys.stderr)
        return 1
    md = to_markdown(result)
    if args.out_json:
        with open(args.out_json, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
            fh.write("\n")
    if args.out_md:
        with open(args.out_md, "w", encoding="utf-8") as fh:
            fh.write(md)
    sys.stdout.write(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
