<!--
Public copy of a pre-registration. The original was committed to the author's private dissertation repository
(Harry5haw/geyser-dissertation) as prereg/rp1-alphagenome.md, commit 9ca1bc85a6cb3ee8ebbc55d7495176a4e97d2a3f,
annotated tag prereg-rp1-v1, pushed 2026-11-04T01:26:18Z, before any confirmatory data was examined.
SHA-256 of the original file: b2dbfe07ad50a9d4d76d0e5085698d6e80f7684537d96a49d2815385ffcc7b91
Everything below this comment is byte-identical to that file. Amendments, if any, are added as separate dated files.
-->
# Pre-registration RP-1: AlphaGenome's predicted expression effects and fine-mapped eQTLs in personal genomes

- **Version:** v1, written 4 Nov 2026, before any of the confirmatory data below was downloaded, generated or scored. To be committed to the dissertation repo as `prereg/rp1-alphagenome.md` and tagged `prereg-rp1-v1`.
- **Author:** the maintainer (the Geyser Genomics project). Drafted with Claude (AI assistance disclosed in the dissertation's appendix). Approved by the author on 4 Nov 2026.
- **Analysis code, pinned:**
  - `scripts/eval_alphagenome.py` at commit **c1495bd** (Harry5haw/Geyser-Genomics PR #208);
  - `app/alphagenome_annotation.py` at **d40fb97** (PR #207);
  - `alphagenome` client **0.10.0**.
- **Deviations:** any change after the tag is recorded in a deviations table in the article, with the reason and the date.

## 0. Disclosures: what was seen before this pre-registration

1. **Pilot (exploratory, 4 Nov 2026).**
   - Sample and calls: HG003 chromosome 20, DeepVariant PASS calls on Google's 35x case-study BAM.
   - Comparison: 125 positives against 625 controls matched on distance-to-nearest-TSS bin × MAF bin.
   - Result: AlphaGenome |RNA_SEQ log fold change| AUROC **0.740** (95% CI 0.690-0.790). With whole-blood instead of muscle MAF, 0.737.
   - **The pilot motivated this study and is excluded from every confirmatory test.**
2. **Counts only, not scores:** unique variants with PIP ≥ 0.9 per chromosome in the eQTL Catalogue GTEx v8 credible sets. chr19 825, chr20 296, chr21 258, chr22 447 (11,623 genome-wide). These were used to size the study.
3. **Nothing else.** No AlphaGenome, CADD, phyloP or AVI value has been seen for any variant outside the pilot. No calls exist yet for HG001, or for any sample on chr19, chr21 or chr22, from the data below.

## 1. Data

- **Samples:** GIAB HG001 (NA12878, European ancestry), HG003 (Ashkenazi) and HG005 (Han Chinese), which are mutually unrelated.
  - Source: Google's public 30x NovaSeq PCR-free whole-genome BAMs, GRCh38 (hs38DH, BWA-MEM), at `gs://brain-genomics-public/research/sequencing/grch38/bam/novaseq/wgs_pcr_free/30x/`.
  - Chromosomes **chr19, chr20, chr21 and chr22**, each cut from the remote BAM with samtools.
- **Calls:**
  - Caller: DeepVariant 1.10.0 (GPU image built from `app-deepvariant/Dockerfile`, `DV_TAG=1.10.0-gpu`), WGS model, default settings, on each sample × chromosome.
  - **All confirmatory calls are made fresh with this one image.** Existing chr20 VCFs aren't reused.
  - Variants used: PASS records only; SNV alleles only (ref and alt one base each, ACGT); multi-allelic records split into one candidate per ALT.
- **Truth set:** eQTL Catalogue SuSiE credible sets for all **49 GTEx v8 gene-expression datasets** (study QTS000015, `*.credible_sets.tsv.gz`, GRCh38, CC BY 4.0).
  - **Positive:** PIP ≥ 0.9 in at least one dataset.
  - **Control candidate:** in no credible set in any dataset.
- **Allele frequency (for matching):** the `maf` column of the eQTL Catalogue GTEx v8 summary statistics for **muscle (QTD000281, n = 702)**. One value per variant (chromosome, position, ref, alt). Variants without a MAF can't be matched on AF and are excluded from AF-matched analyses (counts reported).
- **TSS:** Ensembl 105 gene metadata from eQTL Catalogue (`gene_counts_Ensembl_105_phenotype_metadata.tsv.gz`, Zenodo 10.5281/zenodo.7808390, CC BY 4.0), using `phenotype_pos`, all genes. The distance is to the nearest TSS on the same chromosome.

## 2. Scores

- **AG (primary):** AlphaGenome Atlas, one `AtlasClient.query_variant` per SNV, over the 11 effect scorers (the recommended set without the `*_ACTIVE` scorers and `CONTACT_MAPS`).
  - **AG = |raw signed log fold change| of the RNA_SEQ gene × track cell with the largest |quantile score|**, as computed by `alphagenome_annotation.reduce_atlas` at d40fb97.
  - The full RNA_SEQ gene × track matrix is stored locally (never published) for secondary analysis S1.
- **AG-gene (secondary S1):** |raw LFC| for the positive's own eQTL gene (the credible set's `gene_id`), in the AlphaGenome RNA-seq track whose ontology term matches the GTEx tissue of the positive's highest-PIP dataset (via the dataset's UBERON `tissue_id`).
  - When there's no matching track, use the gene's largest-|quantile| track.
  - Controls take the gene and tissue of the positive they're matched to.
- **Baselines (H2):**
  - (a) `neg_tss_distance`: minus the distance to the nearest TSS;
  - (b) **CADD v1.7** GRCh38 PHRED for the exact SNV (`whole_genome_SNVs.tsv.gz`);
  - (c) **phyloP**, Zoonomia 447-way (UCSC `phyloP447way`), the value at the variant's position. If that track can't be read, phyloP 100-way (`phyloP100way`), stated as a deviation;
  - (d) **AVI** PHRED (AlphaGenome Atlas AVI SNV scores), **only if** the scores can be obtained before the analysis (by remote range reads of the hosted file, or the author's download). Otherwise baseline (d) is dropped, and that's reported.
- **Direction:** higher means more likely regulatory for every score (hence minus the distance).
- **Missing values:** a variant missing a score is excluded from that score's AUROC (counts reported). If more than 10% of positives or controls are missing a score, that's flagged in the results.

## 3. Hypotheses, designs and success criteria

**Common to all:**
- **Statistic:** the AUROC (Mann-Whitney, ties counted half), with a percentile bootstrap 95% CI (2,000 resamples, positives and controls resampled independently).
- **Matching:** controls drawn without replacement from the candidates in the same stratum, as implemented in `eval_alphagenome.match_controls`.
  - TSS-distance bins (bp): ≤1,000 / 1,000-5,000 / 5,000-20,000 / 20,000-100,000 / 100,000-500,000 / > 500,000.
  - MAF bins: ≤0.01 / 0.01-0.05 / 0.05-0.10 / 0.10-0.25 / > 0.25.

### H1 (primary, confirmatory): AG ranks fine-mapped eQTLs above matched controls
- **Data:** HG003, **chr19 + chr21 + chr22**, pooled.
- **Design:** 5 controls per positive, matched on the TSS bin × MAF bin; seed **20261104**.
- **Success:** the AUROC's 95% CI lower bound **> 0.50**, **and** the AUROC point estimate **≥ 0.60** (the smallest effect of interest).

### H2 (confirmatory): AG beats the baselines
- **Data:** the H1 positives, with a **separate control draw matched on the MAF bin only** (5 per positive, seed **20261105**). This lets distance to TSS compete.
- **Test:** for each baseline k in {(a), (b), (c), [(d)]}, the paired **DeLong** test of ΔAUROC = AUROC(AG) − AUROC(baseline k), two-sided, on the variants with both scores. **Holm adjustment across the baselines tested**, at α = 0.05.
- **Success for baseline k:** Holm-adjusted p < 0.05 **and** ΔAUROC > 0.
- **Reporting:** each baseline is reported separately. There's no single pass or fail for H2 as a whole.

### H3 (confirmatory): replication across samples and chromosomes
- **Cells:** {HG001, HG003, HG005} × {chr19, chr20, chr21, chr22}, minus HG003 × chr20 (the pilot): **11 cells**. Each uses H1's design (TSS × MAF matched, 5 per positive, seed 20261104 within each cell).
- **Endpoint:** the random-effects (DerSimonian-Laird) meta-estimate of the AUROC across the 11 cells, with I² reported. Each cell's variance is DeLong's (`eval_alphagenome.delong_variance`).
- **Success:** the meta-estimate's 95% CI lower bound **> 0.50**, **and** at least **9 of 11** cells have an AUROC point estimate > 0.50.
- **Companion analysis** (reported alongside, not part of the criterion; shared common variants make cells overlap):
  - the variant-level union of the three samples' PASS SNVs on **chr19, chr21 and chr22**, each unique variant counted once;
  - H1's design, seed **20261106**.

## 4. Secondary analyses (pre-specified, descriptive, no success criteria)

- **S1:** AG-gene vs AG, paired DeLong, on H1's data.
- **S2:** gene agreement: the share of H1 positives whose AG top gene (names mapped to Ensembl IDs with the Ensembl 105 metadata) is one of the positive's eQTL genes.
- **S3, the in-pipeline arm:** the same as H1, on Geyser's own cloud bcftools calls, compared with DeepVariant calls of the same sample and chromosome:
  - the existing runs REAL-HG003-30x and REAL-HG005-30x (chr20);
  - plus, if run, cloud runs on chr19/21/22.

## 5. Sensitivity analyses (pre-specified, exploratory)

On H1's data unless stated otherwise:
1. PIP thresholds ≥ 0.5, 0.8, 0.95 and 0.99.
2. 3 and 10 controls per positive.
3. Coarser bins (3 TSS × 3 MAF: ≤5 kb / 5-100 kb / > 100 kb; ≤0.05 / 0.05-0.25 / > 0.25) and finer bins (8 TSS × MAF deciles).
4. Additional matching on gene density (Ensembl 105 genes within ±500 kb, in tertiles).
5. MAF from whole blood (QTD000356) instead of muscle, and pooled MAF (the mean over the GTEx datasets that report the variant).
6. The raw |LFC| vs the quantile score of the same cell; the top track vs the tissue-matched track.
7. Strata: the AUROC within each MAF bin and each TSS bin; and excluding variants within 1 kb of a TSS.
8. HG005: the number of positives lost for lack of a GTEx MAF, and the AUROC for HG005 without AF matching.

**Multiple testing:**
- H1 is a single test.
- H2 uses Holm across its baselines.
- H3 uses its pre-specified meta-analytic criterion.
- Sections 4 and 5 report effect sizes with CIs only. They're labelled exploratory, and no claim rests on their p-values.

## 6. Procedure and stopping rules

- **Order:**
  1. tag this file;
  2. download the BAM slices, CADD ranges, phyloP and MAF;
  3. call variants;
  4. build the positive and control sets per cell;
  5. score with the Atlas (each unique variant once);
  6. run the analyses exactly as above, with the pinned code.
- **Atlas stopping rule:** stop on rate limits or repeated service errors, with no aggressive retries. Resume later with the same variant sets. Results aren't analysed until scoring is complete.
- **Exclusions:**
  - non-PASS records;
  - indels and other non-SNV alleles;
  - REF alleles that don't match the GRCh38 reference;
  - variants on no listed chromosome.
  Each is counted and reported.
- **No combined classifier** is fitted on AlphaGenome outputs: AlphaGenome's terms forbid training models on them.

## 7. Negative outcomes and reporting

- **If H1 fails,** report "no evidence that AlphaGenome's predicted expression effects prioritise fine-mapped eQTLs over TSS- and MAF-matched controls at held-out loci", and keep the pilot labelled exploratory.
- **If H2 fails for a baseline,** report that AG adds no measurable value over that baseline (a cheaper or offline score may serve the pipeline as well).
- **If H3 fails,** report the heterogeneity (I², cell estimates) and where it fails: by sample or ancestry, or by chromosome.
- **Equal prominence:** every pre-registered endpoint is reported with equal prominence, whatever the result, in the article and the dissertation. The tag `prereg-rp1-v1` is cited, and the deviations table is included.

## 8. Terms

- **AlphaGenome outputs:** "By using this information, you agree to AlphaGenome Output Terms of Use (https://deepmind.google.com/science/alphagenome/output-terms). Research use only; not for clinical decision-making."
- **Publishing:** only aggregate results (AUROCs, CIs, counts, figures) and code are published. Per-variant AlphaGenome scores aren't redistributed, and readers regenerate them with their own key.
- **Other sources:**
  - CADD: non-commercial terms;
  - UCSC data: research use, with credit to the contributors;
  - eQTL Catalogue: CC BY 4.0;
  - GIAB and Google's BAMs: public.
