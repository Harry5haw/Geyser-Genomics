# AlphaGenome-aware variant annotation (Geyser 3.0 "Project Alpha Genome")

**Status:** live in v3.0 "Alpha Genome" (9 Nov 2026): the Annotate step ([#207](../../../../pull/207) app,
[#209](../../../../pull/209) infrastructure), the variants-of-interest panel ([#263](../../../../pull/263)) and the
dashboard signals ([#264](../../../../pull/264)). This note records the decisions as they now run; the evaluation
harness below is #208. Release notes: [docs/releases/v3.0.md](../releases/v3.0.md).

> By using this information, you agree to AlphaGenome Output Terms of Use found at
> https://deepmind.google.com/science/alphagenome/output-terms

The terms, data and API facts below were checked on 4 Nov 2026 against the primary sources listed at the end. They're marked **(unverified)** where only a secondary source or an inference supports them.

## Goal

A run can optionally report which of its called variants AlphaGenome predicts will change **gene regulation** (expression, splicing, chromatin), next to `bcftools csq`'s protein-level consequences. The feature is:
- opt-in per run, and off by default;
- never fatal, so a failed annotation is noted and the run keeps its status;
- research use only, never clinical.

## What AlphaGenome offers

| Route | What it gives | Terms | Used for |
|---|---|---|---|
| **AVI download** (`avi_scores_snvs_tabix.zip`, 88.5 GB, one file) | One score per SNV: `#CHROM POS REF ALT raw_score PHRED`. GRCh38, chr1-22, X and Y. No chrM, no indels, **no gene, no track**. Raw ≈ −1.3 to +4.0; PHRED 0 to ~80, where 20 means the top 1% genome-wide | **Permissive, commercial and non-commercial** (the ToS' "Permissive Use Downloadable Artifacts") | Local SNV lookup: `AG_AVI` (PHRED), `AG_AVI_RAW` |
| **Atlas API** (`alphagenome.atlas.AtlasClient`: `query_variants`, `query_interval`) | Precomputed per-scorer, per-track and per-gene scores, raw and calibrated, filterable by tissue (ontology term) and gene. Higher query rate than the model | Non-commercial; API key | Gene and track detail ("why") for selected SNVs: `AG_RNA_LFC`, `AG_GENE`, `AG_TOP_TRACK` |
| **Model API** (`dna_client.create(key).score_variant(interval, variant, scorers)`) | One AnnData per scorer (tracks × genes). Interval of 16 kb, 128 kb, 512 kb or 1 Mb; up to 20 scorers per request (the recommended set has 19) | Non-commercial; API key; no published quota ("thousands of predictions", not millions) | Indels, and SNVs not in the Atlas, capped |
| Open weights (Kaggle and Hugging Face, JAX) | Self-hosted inference | Non-commercial model terms | **Not used.** Needs an H100-class 80 GB GPU; Google Cloud won't even run it on a 40 GB A100. The RTX 3080 (10 GB) and L4 (24 GB) are out **(unverified)**, and AWS p4de/p5 would break the cost rules |

### What AVI is
AVI is a neural network over AlphaGenome's predictions, with each of ten modalities reduced to its maximum across tissues. It also takes in AlphaMissense, VEP's protein-truncation flags and two conservation scores. **It's trained to separate variants with gnomAD AF > 0.1% from rarer ones** (Cheng et al. 2026). That makes it a **deleteriousness score**: useful for ranking a sample's variants, but not a measure of regulatory effect. The evaluation (below) treats it that way.

### Terms that bind the design
- **Outputs and Atlas data are non-commercial**, except the AVI download, which is permissive.
- **Outputs must not be used to train other ML models.** So the pipeline reports and ranks raw scores, and fits no model on them.
- **Research purposes only.** Never for clinical decision-making or medical advice: say so in every output that shows a score (the TSV header, the web UI and the run record).
- **Cite** the AlphaGenome paper (Avsec et al. 2026), and the Atlas paper for AVI and Atlas data (Cheng et al. 2026).
- **Data in the repo:** commit no AVI file and no bulk Atlas or API output. Summaries, figures and a few example variants with citations are fine.
- The full Terms of Service page didn't render for the check; the maintainer reads it when registering for the API key.

## Design (as it runs in v3.0)

### Decisions now live
| Decision | What runs | Why |
| :--- | :--- | :--- |
| **Atlas only in the cloud** | On S3 storage the only source is `atlas` (`CLOUD_SOURCES`); `avi-file` and `fake` are refused there | The Atlas fills gene, track and RNA LFC, which is what the panel and the evaluation need. One request per SNV, capped |
| **No AVI in S3** | No AVI extract is kept in the cloud, so cloud rows have empty `avi`/`avi_raw` (`AG_AVI` absent) | The 88.5 GB download, and a ~2 GB chr20 extract, would sit in S3 for a score the cloud doesn't need. `avi-file` stays an offline source |
| **Annotate on both routes** | The chromosome-scale and whole-genome routes end in the same VCF and run record, so `Plan_Annotate` → `Annotate` is one step after the quality gate on either route, with either caller | One place to opt in (`annotate=alphagenome` upload metadata), one place to fail |
| **`--plan` sizing** | `tasks.py annotate --plan` sizes the job from the VCF's size and the cap (`plan_resources`: vCPUs, memory, timeout, disk); the state machine runs `Plan_Annotate` first and sizes `Annotate` from it. Nothing to annotate: the step is skipped | A whole genome and a smoke sample need different jobs; the formula lives in Python, where it's tested |
| **Never fatal** | Every failure (planning, scoring, the VCF) is noted in the run record (`Annotate` part) and the run keeps its status | Annotation is an extra; CLOUD025 showed it: `Plan_Annotate` was refused and the run still succeeded |

**App** (`app/alphagenome_annotation.py`; `tasks.py annotate <id> <ref> --source atlas|api|avi-file|fake [--max-variants N] [--plan]`)
- **Selection:** PASS calls; with bcftools csq consequences, only non-coding variants near a gene (UTR, splice
  region, intron, non-coding); ranked by QUAL with ties spread by a stable hash; at most `ALPHAGENOME_MAX_VARIANTS`
  (default 500, 1-20,000). The VCF is streamed and only the best N are held, so a whole genome selects in bounded
  memory.
- **Sources:** `atlas` (cloud and offline; `AG_GENE`, `AG_TOP_TRACK`, `AG_RNA_LFC` for the top gene), `api`
  (`score_variant`, 1 Mb window: indels, capped), `avi-file` (offline only: `AG_AVI`, `AG_AVI_RAW` from a local
  extract), `fake` (offline tests; `AG_SOURCE=fake`, not predictions).
- **Outputs:** `variants/{id}.alphagenome.vcf.gz` (+ `.tbi`; INFO `AG_*` and `AG_SOURCE`) and
  `stats/{id}/alphagenome.tsv` (columns `rank chrom pos ref alt kind qual avi avi_raw top_track gene rna_lfc csq
  source`; empty cell = no value). Both carry the terms notice: the VCF as a header line, the TSV as its first
  `#` line.
- **Run record:** the `Annotate` part (`runs/{id}.provenance/Annotate.json`): status, `duration_seconds`,
  `resources` (CPU seconds, peak memory), and `alphagenome` (source, model version, cap, selection counts,
  `scored`, `not_scored` by kind, `not_in_service`, `api_calls`, the VCF's outcome, `terms`). The assembled record
  copies `alphagenome` to its top level. Metrics: `Annotate` (duration, failures) and `AnnotateApiCalls`.
- **Key handling:** an SSM SecureString (`/geyser/alphagenome/api-key`) mapped into the job by its job definition;
  read only from the environment, never logged, redacted from client errors. Locally it comes from
  `~/geyser-secrets/alphagenome.env`. It never goes in tfvars, tracked files, logs or PR text.
- **Image:** `geyser-annotate` = `geyser-app` + the AlphaGenome client pinned at 0.10.0 (~301 MB compressed),
  so no other stage pulls the client.

**Web UI:** the sample page's "Variants of interest" panel: gene, AVI (a dash on Atlas rows), top track, RNA LFC,
the csq consequence and the source, with the research-only note and the terms notice. The hosted site shows the
panel only: no AlphaGenome downloads (the web API's allowlist leaves them out).

### First cloud run
`CLOUD026-20261109T204333Z-f7ddf042` (`annotate=alphagenome`, `smoke` gate profile) **SUCCEEDED in 7 min 7 s**:
the Atlas scored **10 of 10 SNVs in 10 API calls**; the Annotate job ran in 2.9 s with a peak of 231 MiB; the panel
is on the hosted page with the terms notice; the key appears 0 times in the logs. CLOUD023-025 found three IAM
gaps on the way (fixed in place, see the release notes).

## Evaluation (the dissertation's 3.0 chapter)

The harness is `scripts/eval_alphagenome.py` (tests in `tests/test_eval_alphagenome.py`, synthetic fixtures only).
It reads the annotate stage's TSV or VCF as #207 writes them, and the run record (`--run-record`: the assembled
record or its `Annotate` part; with `--plan`, a Fargate cost estimate for the stage). On an Atlas-only table (every
cloud run) the default AVI score has no values: the report leaves it out and says why, and the csq-vs-AVI overlap
table is marked not available. Scores from the `fake` backend are marked as test data. Every report starts and ends
with the terms notice. The preregistered study that uses it is [docs/prereg/rp1-alphagenome.md](../prereg/rp1-alphagenome.md).

1. **Do AlphaGenome's scores find fine-mapped eQTLs?**
   - **Positives:** the sample's calls (HG003 and HG005 chr20) that are fine-mapped eQTLs with PIP ≥ 0.9. The source is eQTL Catalogue's SuSiE credible sets for the GTEx v8 tissues (CC BY 4.0, GRCh38).
   - **Controls:** the sample's non-coding calls in no credible set. Each positive gets up to 5 controls from the same stratum of distance-to-nearest-TSS bin × allele-frequency bin, drawn without replacement with a fixed seed.
   - **AF for matching:** a population allele frequency, e.g. eQTL Catalogue's summary-statistic `maf` for the tissue, because the pipeline has no gnomAD. The report states what was matched on, and **says when AF wasn't**.
   - **Primary score:** `|AG_RNA_LFC|`, AlphaGenome's predicted expression change, which is what an eQTL is. The harness also reports how often AlphaGenome's top gene is the eQTL's gene.
   - **Secondary score:** `AG_AVI`, reported with the **frequency confound** stated plainly. Fine-mapped eQTLs are mostly common, and AVI is trained to score common variants low. Unmatched on AF, AVI's AUROC measures frequency, not regulation; on synthetic data built that way the harness shows exactly this, with AUROC < 0.3 unmatched and ≈ 0.5 matched.
   - **Statistic:** the AUROC (Mann-Whitney, ties counted half) with a percentile bootstrap 95% CI (1,000 resamples, positives and controls resampled independently).
   - **Comparisons with baselines** (`--delong`): the primary score against each other score (e.g. CADD, phyloP, AVI, or the built-in `neg_tss_distance`), on the same positives and controls, by DeLong's paired test (fast midrank form; Sun & Xu 2014). The p-values are Holm-adjusted across the comparisons. For a distance-to-TSS baseline to compete, the controls are matched on allele frequency only (`--match af`). No combined model is fitted on AlphaGenome scores, because the terms forbid it.
2. **Coverage and cost:** the share of selected variants scored (by source); Atlas and API requests per run; time, CPU and peak memory from the run record, and $ estimated from the `--plan` size (CLOUD026: 10/10 SNVs in 10 calls, 2.9 s, 231 MiB).
3. **Agreement:** a 2×2 table of csq protein-level consequence × AlphaGenome flag (default AVI PHRED ≥ 20, so offline `avi-file` runs only; on Atlas output pass `--flag rna_lfc:abs>=X`), plus the variants only one of them flags.
4. **The HG003/HG005 picture:** the AVI distribution of their PASS SNVs, and the PHRED ≥ 20 tail. Most calls are common, so most AVI values are low. That's expected, and it's reported as such.

**Fetching the truth set** happens only after the maintainer has confirmed the terms. The command is in the script's docstring, for example:

```bash
curl -fO https://ftp.ebi.ac.uk/pub/databases/spot/eQTL/susie/QTS000015/QTD000116/QTD000116.credible_sets.tsv.gz
```

It's per tissue, with columns `molecular_trait_id, variant, chromosome, position, ref, alt, cs_id, pip, z, …`. Cite Kerimov et al. 2021. The alternative is GTEx v8 DAP-G on Zenodo (CC BY 4.0, 9.7 GB eQTL tar, European samples, MAF > 0.01).

## Limits, stated plainly
- **Research only, not clinical:** AlphaGenome's terms, and this pipeline's.
- **No mtDNA:** the Atlas and AVI have no chrM (relevant to mitochondrial disease questions).
- **No population allele frequency in the pipeline** (no gnomAD). AVI already folds frequency in through its training, but the pipeline can't show AF beside it.
- **SNVs only offline:** indels need the API, which is capped and has no published quota.
- **No AVI in the cloud:** cloud rows carry the Atlas's gene-level scores only; AVI comparisons need an offline `avi-file` run.
- **Scores can change:** AVI's model weights arrive "upon final publication", so the run record pins the AVI file by its SHA-256.
- **Single-sequence, unphased model:** each variant is scored alone, not on the sample's haplotype.

## Sources
- AlphaGenome client: https://github.com/google-deepmind/alphagenome (README terms; `docs/source/faqs.md`, `variant_scoring.md`; `src/alphagenome/models/v1/{variant_scorers,dna_client,dna_model}.py`; `src/alphagenome/atlas/atlas.py`; release v0.10.0)
- Avsec et al. 2026, *Advancing regulatory variant effect prediction with AlphaGenome*, Nature, doi:10.1038/s41586-025-10014-0
- Cheng et al. 2026, *AlphaGenome Atlas*, medRxiv, doi:10.64898/2026.09.16.26363192 (AVI model; Data availability: "static download of AVI (Tabix format) is permissively licensed for commercial and non-commercial use"; SNVs only)
- Ensembl VEP AVI plugin (file columns and score ranges): https://github.com/Ensembl/VEP_plugins/blob/main/AVI.pm
- Weights and hardware: https://github.com/google-deepmind/alphagenome_research; https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/open-models/alphagenome
- eQTL Catalogue: https://www.ebi.ac.uk/eqtl/License/ (CC BY 4.0); `tabix/Columns.md` in https://github.com/eQTL-Catalogue/eQTL-Catalogue-resources; Kerimov et al. 2021, Nat Genet, doi:10.1038/s41588-021-00924-w
- GTEx v8 DAP-G: https://zenodo.org/records/3517189 (CC BY 4.0)
