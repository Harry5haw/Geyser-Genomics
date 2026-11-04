# AlphaGenome-aware variant annotation (Geyser 3.0 "Project Alpha Genome")

**Status:** design. Draft PRs only until v2.0 is tagged.

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

## Design

**App (`app/alphagenome.py`, `tasks.py annotate <id> <ref> --source avi-file|atlas|api|fake`)**
- No AWS imports; storage goes through the existing backends. The annotate stage's interface contract (#207) is authoritative for names, columns and the run-record part; the summary here follows it.
- **Step 1, AVI (all PASS SNVs, local, $0):**
  - tabix lookup in `reference/alphagenome/avi_chr20.tsv.gz` (+ `.tbi`);
  - normalise `#CHROM` (`chr20` or `20`), and check that REF equals our GRCh38 base, counting mismatches;
  - this fills `AG_AVI` (PHRED) and `AG_AVI_RAW`.
- **Step 2, Atlas (selected SNVs, needs the key, capped):**
  - per-gene `RNA_SEQ` log fold change and the top track, which fill `AG_RNA_LFC`, `AG_GENE` and `AG_TOP_TRACK`;
  - selection: PASS calls; non-coding or near-gene by `bcftools csq` (UTR, splice region, upstream or downstream, intronic near exons), plus AVI PHRED ≥ 20; ranked; at most `ALPHAGENOME_MAX_VARIANTS` (default 500).
- **Step 3, model API (indels, capped):** `score_variant` with a 1 Mb interval centred on the variant and the 19 recommended scorers. That's one request per variant, from the same cap.
- **Output:**
  - `variants/{id}.alphagenome.vcf.gz` (+ `.tbi`) with INFO `AG_*`, and `AG_SOURCE=avi-file|atlas|api|fake`;
  - `stats/{id}/alphagenome.tsv`, a ranked "variants of interest" table;
  - a run-record part `Annotate`: counts per source, requests used, seconds, the client version, and the AVI file's name, byte size, SHA-256 and download date;
  - metrics `Annotate` and `Annotate.ApiCalls`.
- **Offline first:** a `fake` backend replays recorded responses for the unit tests and the Docker E2E, and `run_local.py --annotate alphagenome` runs it locally.
- **Key handling:** the key lives in SSM Parameter Store as a SecureString. The job definition's `secrets` puts it in the environment, and the task role gets `ssm:GetParameter` on that one parameter. Locally it comes from `~/geyser-secrets/alphagenome.env`. It never goes in tfvars, tracked files, logs or PR text.
- **Pin `alphagenome==0.10.0`** (released 8 Oct 2026). The client went from 0.8 to 0.10 in seven weeks.

**Data:** there's no per-chromosome download. Getting chr20 is a one-off, and it's the maintainer's call:
1. download the 88.5 GB ZIP (with ~264 GB free locally);
2. `tabix -h … chr20 | bgzip`, then index it: an estimated **~2 GB** and ~190 M rows **(unverified)**;
3. upload it to `reference/alphagenome/`, then delete the ZIP.

The unofficial Hugging Face mirror could serve remote tabix queries, but it isn't the source of record.

**State machine (infra PR, plan written first, applied with the maintainer):**
- An optional `Annotate` step after `Quality_Gate`, chosen by `annotate: "alphagenome"` in the input or by upload metadata `annotate=alphagenome`, validated like `gate-profile`.
- A Catch that only notes the failure.
- Fargate at 0.25-0.5 vCPU for a few minutes, under $0.005 per run. No GPU and no NAT: the API is reached over the task's public IP.

**Web UI:** a "Variants of interest" table: gene, AVI PHRED, |RNA LFC| and top track, the csq consequence, and the research-only note.

## Evaluation (the dissertation's 3.0 chapter)

The harness is `scripts/eval_alphagenome.py` (tests in `tests/test_eval_alphagenome.py`, synthetic fixtures only).

1. **Do AlphaGenome's scores find fine-mapped eQTLs?**
   - **Positives:** the sample's calls (HG003 and HG005 chr20) that are fine-mapped eQTLs with PIP ≥ 0.9. The source is eQTL Catalogue's SuSiE credible sets for the GTEx v8 tissues (CC BY 4.0, GRCh38).
   - **Controls:** the sample's non-coding calls in no credible set. Each positive gets up to 5 controls from the same stratum of distance-to-nearest-TSS bin × allele-frequency bin, drawn without replacement with a fixed seed.
   - **AF for matching:** a population allele frequency, e.g. eQTL Catalogue's summary-statistic `maf` for the tissue, because the pipeline has no gnomAD. The report states what was matched on, and **says when AF wasn't**.
   - **Primary score:** `|AG_RNA_LFC|`, AlphaGenome's predicted expression change, which is what an eQTL is. The harness also reports how often AlphaGenome's top gene is the eQTL's gene.
   - **Secondary score:** `AG_AVI`, reported with the **frequency confound** stated plainly. Fine-mapped eQTLs are mostly common, and AVI is trained to score common variants low. Unmatched on AF, AVI's AUROC measures frequency, not regulation; on synthetic data built that way the harness shows exactly this, with AUROC < 0.3 unmatched and ≈ 0.5 matched.
   - **Statistic:** the AUROC (Mann-Whitney, ties counted half) with a percentile bootstrap 95% CI (1,000 resamples, positives and controls resampled independently).
   - **Comparisons with baselines** (`--delong`): the primary score against each other score (e.g. CADD, phyloP, AVI, or the built-in `neg_tss_distance`), on the same positives and controls, by DeLong's paired test (fast midrank form; Sun & Xu 2014). The p-values are Holm-adjusted across the comparisons. For a distance-to-TSS baseline to compete, the controls are matched on allele frequency only (`--match af`). No combined model is fitted on AlphaGenome scores, because the terms forbid it.
2. **Coverage and cost:** the share of called SNVs found in the AVI file; Atlas and API requests per run; time and $, from the run record.
3. **Agreement:** a 2×2 table of csq protein-level consequence × AlphaGenome flag (default AVI PHRED ≥ 20), plus the variants only one of them flags.
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
