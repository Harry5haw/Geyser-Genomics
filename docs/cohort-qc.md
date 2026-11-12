# Cohort QC with Hail and Spark

The pipeline handles one sample at a time. These two jobs look across samples once they're done. They're analysis
jobs that run on demand and aren't part of the pipeline: no stage calls them and nothing in Terraform creates them.

| Job | Engine | Reads | Writes |
|---|---|---|---|
| `scripts/cohort_qc.py` | [Hail](https://hail.is) 0.2.139 on Spark 3.5 | each sample's VCFs from bcftools, DeepVariant and pangenome DeepVariant | sample QC, variant QC and a caller comparison (Parquet, CSV, markdown) |
| `scripts/cohort_runs.py` | plain PySpark 3.5.4 (the same code runs on AWS Glue 5.0) | `runs/*.provenance.json`, the run records | one row per sample: stage times, CPU, memory and QC stats (Parquet) |

## Setup

Both jobs need `requirements-cohort.txt` and Java 11. These aren't in `requirements-dev.txt` or the pipeline image,
because Hail's wheel alone is about 130 MB. Hail has no Windows build, so on Windows (or anywhere without Java 11)
use the small image in `app-cohort/`. It holds only the dependencies, and the repository is mounted at run time:

```bash
docker build -f app-cohort/Dockerfile -t geyser-cohort:local .
docker run --rm -v "$PWD:/work" geyser-cohort:local python -m pytest tests/test_cohort_qc.py tests/test_cohort_runs.py
```

On Linux or macOS with Java 11: `pip install -r requirements-dev.txt -r requirements-cohort.txt`.

## Caller comparison (`cohort_qc.py`)

```bash
# Look up the samples under a storage root in the pipeline's layout (local_bucket/ or a copy of the bucket)
python scripts/cohort_qc.py --root /data --samples HG003_chr20 HG005_chr20 --out /data/cohort/qc
# Or list the call sets yourself
python scripts/cohort_qc.py --vcf HG003:bcftools:/data/a.vcf.gz --vcf HG003:deepvariant:/data/b.vcf.gz --out out/
```

Each call set is read from the pipeline's own key (`app/provenance.py` `STAGE_VCF`): `variants/{id}.vcf.gz`,
`variants_deepvariant/{id}.vcf.gz` and `variants_pangenome/{id}.vcf.gz`. The job then does four things:

1. **Keeps PASS calls only.** DeepVariant also writes its RefCall and NoCall candidates into the VCF, about half of
   its records. Counting them drops HG003's Ti/Tv from 1.96 to 0.86 (the same issue the quality gate has, CLAUDE.md
   G4). bcftools writes FILTER `.` (no filters applied), which counts as PASS.
2. **Splits multi-allelic sites** (`hl.split_multi_hts`) and gives every call set the same entry schema: GT, plus
   DP (FORMAT/DP where the caller writes it, otherwise the sum of AD; the pipeline's bcftools calls carry only
   GT:PL:AD).
3. **Joins all call sets into one MatrixTable**: rows are variants, and there's one column per (sample, caller)
   pair. It's an outer join, so a call set with nothing at a site has a missing genotype there.
4. **Runs `hl.sample_qc` and `hl.variant_qc`** and counts, for each sample, how many calls each combination of
   callers made.

Outputs under `--out`:

| File | Contents |
|---|---|
| `caller_comparison.csv` | One row per (sample, caller): PASS calls, SNPs, indels, Ti/Tv, het/hom ratio, calls unique to that caller, calls all of the sample's callers made |
| `caller_overlap.parquet/` | Per sample: the number of calls for each caller combination (`bcftools+deepvariant`, ...), by SNP or indel |
| `sample_qc.parquet/` | `hl.sample_qc` for each call set: call rate, depth and GQ stats, allele counts, singletons |
| `variants.parquet/` | One row per variant: `hl.variant_qc` over all the call sets, plus `n_samples_<caller>` (how many samples each caller called it in) |
| `summary.md` | The comparison and overlap tables in markdown |

**Sites vs alleles.** `hl.sample_qc` counts alternate *alleles*: a hom-var SNP counts as two SNP alleles and two
transitions (or transversions). `bcftools stats`, and so the quality gate, counts *sites*. The caller comparison
counts sites, so its Ti/Tv can be compared with the gate's; the sample_qc Parquet keeps Hail's own definitions.

## Cohort table from the run records (`cohort_runs.py`, Glue-ready)

```bash
python scripts/cohort_runs.py --runs local_bucket/runs --out cohort/runs --csv
```

This reads every `runs/{id}.provenance.json`. It skips the per-stage parts in `runs/{id}.provenance/` and the web
worker's `runs/{id}.json`. Each record becomes one flat row: sample, run name, status, layout, aligner, reference
MD5, app version, and the gate's numbers (mapped %, properly paired %, duplicate %, mean depth, SNPs, indels, Ti/Tv).
Every stage that can appear in a record (Preflight, QualityControl, Align, CallVariants, DeepVariant,
PangenomeDeepVariant, QualityGate, Annotate) gets status, seconds, CPU seconds and peak memory columns, plus totals.
A stage a run never had is a null column, so tables from different cohorts line up. `summary/` holds the cohort's
min, mean and max for each stat.

### On AWS Glue (not deployed)

Glue 5.0 runs Spark 3.5.4 on Python 3.11, the same versions as `requirements-cohort.txt`.
`scripts/glue/cohort_runs_job.py` is a thin entry point: it reads the job arguments and calls
`cohort_runs.build_cohort()` with Glue's Spark session. Nothing creates the job, and the project has no Glue IAM
role. Running it once would take about 2 DPU for a minute, at a few cents. The steps would be (not run):

```bash
aws s3 cp scripts/cohort_runs.py          s3://<bucket>/glue/cohort_runs.py
aws s3 cp scripts/glue/cohort_runs_job.py s3://<bucket>/glue/cohort_runs_job.py
aws glue create-job --name geyser-cohort-runs --role <glue role> --glue-version 5.0 \
  --worker-type G.1X --number-of-workers 2 \
  --command Name=glueetl,ScriptLocation=s3://<bucket>/glue/cohort_runs_job.py,PythonVersion=3 \
  --default-arguments '{"--extra-py-files":"s3://<bucket>/glue/cohort_runs.py"}'
aws glue start-job-run --job-name geyser-cohort-runs \
  --arguments '{"--RUNS":"s3://<bucket>/runs","--OUT":"s3://<bucket>/cohort/runs","--CSV":"true"}'
```

The Glue role needs `s3:GetObject` on `runs/*`, `s3:ListBucket`, and `s3:PutObject`/`s3:DeleteObject` on
`cohort/*`. A later step would be a Glue crawler (or an Athena table) over `cohort/runs/samples/` to query it with SQL.

## Tests

`tests/test_cohort_qc.py` and `tests/test_cohort_runs.py` use small synthetic VCFs and records with hand-computed
answers: RefCall/NoCall excluded, a multi-allelic site split, unique and shared calls, per-site Ti/Tv, record
discovery, null stages. Without Hail, pyspark or Java they skip, so the main unit job doesn't need them. The
**Cohort QC (Hail, PySpark)** CI job installs them (Java 11, no Docker) and runs on pull requests that touch the
cohort files, and on every push to main.

## Results

Filled in from the run over the project's chr20 call sets (HG003 at two coverages and HG005, three callers each).
