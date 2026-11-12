# Run it locally (no AWS)

The whole pipeline runs on a laptop. Each stage runs in its own container from the same image the cloud uses, and a local folder (`local_bucket/`) stands in for the S3 data lake with the same layout.

**Prerequisites:** Docker and Python 3.11.

```bash
# 1. Build the pipeline image (fastp, bwa-mem2 and bwa, samtools, bcftools)
docker build -f Dockerfile -t geyser-app .

# 2. Create a synthetic genome and 30x reads with 10 known mutations planted in them
python scripts/make_test_data.py

# 3. Run all three stages (QC and alignment one after the other here), just as Step Functions would, then check the VCF against the truth
python scripts/run_local.py --sample SYNTH001 --reference synthetic_ref.fa --check

# Paired-end instead: writes PAIRED001_R1.fastq.gz and PAIRED001_R2.fastq.gz
python scripts/make_test_data.py --sample PAIRED001 --paired
python scripts/run_local.py --sample PAIRED001 --reference synthetic_ref.fa --check

# Scatter-gather variant calling: one bcftools job per 7,000 bp region, then a merge into the same VCF
python scripts/run_local.py --sample SYNTH001 --reference synthetic_ref.fa --shard-window 7000 --check
```

> [!TIP]
> Expected result: `Truth check (bcftools): 10/10 planted SNPs called, 0 unexpected calls`
>
> `--duplicate-rate 0.1` plants exact read (or pair) copies, like PCR duplicates. The align stage's `samtools markdup` flags them, and the SNP calls stay the same.
>
> `--gff` also writes gene models (`reference/synthetic_ref.fa.gff3.gz`): one gene on each strand, around planted SNPs 2-5. The variants stage then annotates consequences with bcftools csq (`variants/SYNTH001.csq.vcf.gz`), and the web UI shows a Consequence column. Reads and SNPs are the same as without `--gff`. For real data, `fetch_dv_testdata.py --dataset hg003-chr20 --annotation` adds Ensembl's chr20 gene models, with the contig renamed to match GRCh38's `chr20`.
>
> The align stage uses [bwa-mem2](https://github.com/bwa-mem2/bwa-mem2), which runs the same algorithm as `bwa mem` faster on modern CPUs. The image picks the fastest build for the CPU it runs on. Set `ALIGNER=bwa` (e.g. `ALIGNER=bwa python scripts/run_local.py ...`) to align with classic `bwa mem` instead.
>
> bwa-mem2 has one known bug ([bwa-mem2#262](https://github.com/bwa-mem2/bwa-mem2/issues/262)): it shifts reads that start in the first few bases of the reference's first contig, which shows up as false SNPs. Human chromosomes start with long N gaps, so they are unaffected. For any reference whose first contig doesn't start with at least 1,000 Ns, the align stage uses bwa instead and logs why. The default synthetic genome is such a reference. `make_test_data.py --leading-gap 1000` gives it a human-like gap, and then bwa-mem2 produces the same 10,000 alignments as bwa. Each aligner has its own index next to the reference (`reference/<name>.0123`, `.bwt.2bit.64` ... for bwa-mem2). The align stage builds a missing index itself. That is quick for one chromosome, but a whole-genome bwa-mem2 index needs about 90 GB of RAM, so build it once as its own Batch job with `scripts/build_reference_index.py` (GRCh38 no-alt by default; see "Build a whole-genome reference index" in [docs/operations.md](operations.md)).
>
> **Graph alignment (`ALIGNER=giraffe`, offline so far):** the align stage can also map the reads to a pangenome graph with [vg giraffe](https://github.com/vgteam/vg) 1.55.0. By default it uses haplotype sampling: kmc counts the sample's k-mers, and giraffe maps to a personal graph built from the haplotypes closest to them. It then surjects the alignments onto the graph's GRCh38 paths. The contig names (`GRCh38#0#chr20`) are renamed to the FASTA's (`chr20`) with `samtools reheader`. The CRAM header lists every FASTA contig in the FASTA's order, as with bwa-mem2. Graph paths that aren't FASTA contigs are left out (a whole-genome graph with `chr20.fa` keeps chr20), and reads that map there stay unmapped. A graph with no path on the FASTA, or one of a different length, fails the stage rather than write a wrong CRAM. After that the reads get the same fixmate, sort, markdup and CRAM as with bwa-mem2. vg and kmc live in their own image (`app-giraffe/`, built on geyser-app), so the other stages don't pull them. The graph is `pangenome/<GIRAFFE_GRAPH>`, by default `<reference stem>.gbz`, with its `.hapl` beside it. A missing `.hapl` is built in the job: seconds for the synthetic graph, ~6 min for chr20, hours for a whole genome. `GIRAFFE_SAMPLING=off` maps to the whole graph with its `.dist`/`.min` instead.
>
> ```bash
> docker build -f app-giraffe/Dockerfile -t geyser-giraffe .
> python scripts/make_test_data.py --pangenome
> python scripts/run_local.py --sample SYNTH001 --reference synthetic_ref.fa --aligner giraffe --check
> ```
>
> In the cloud, `reference/chr20.fa` holds both aligners' indexes and the FASTA index: bwa's, bwa-mem2's `.0123` and `.bwt.2bit.64` (338 MB), and `chr20.fa.fai` (all uploaded 2 Nov 2026), so no stage rebuilds an index. The two share `.amb`/`.ann`/`.pac`, and bwa-mem2 writes them byte-identical to bwa's. To add an index for another reference, build it with the pipeline image and upload only the files that are missing:
>
> ```bash
> docker run --rm -v "$PWD:/data" -w /data --entrypoint bwa-mem2 geyser-app index <name>.fa   # ~30 s and ~2 GB RAM for chr20
> aws s3 cp <name>.fa.0123 s3://<data-lake>/reference/ && aws s3 cp <name>.fa.bwt.2bit.64 s3://<data-lake>/reference/
> ```
>
> Effect on the cloud align job (wave-0 sample: 10k single-end reads, chr20; 4 vCPU / 8 GB Fargate):
>
> | Run | Aligner | bwa-mem2 index | Align job run time |
> | :--- | :--- | :--- | ---: |
> | `CLOUD003-20261102T144231Z-08dc9420` | bwa (image `main-69daa5c`) | n/a (stored bwa index) | 28.1 s |
> | `CLOUD005-20261102T151205Z-548b7a1b` | bwa-mem2 | built in the job | 45.7 s |
> | `CLOUD006-20261102T151552Z-7b850912` (paired, 5k pairs) | bwa-mem2 | built in the job | 66.0 s |
> | `CLOUD007-20261102T152740Z-c8af04a5` | bwa-mem2 | **downloaded** | **29.3 s** |
>
> On a sample this small, alignment itself takes a second or two, so bwa-mem2 only matches bwa: it downloads 338 MB of index against bwa's 113 MB. The speed-up shows on real data (table below).
>
> **bwa vs bwa-mem2, measured** (`scripts/benchmark_aligners.py`). HG003 chr20: 19.0 M reads, paired FASTQs recovered from the 35x case-study BAM, aligned to chr20 by the real align stage. Each aligner ran once, with 16 threads, on an AMD Ryzen 7 3700X (bwa-mem2 picked its AVX2 build):
>
> | | bwa 0.7.18 | bwa-mem2 2.3 |
> | :--- | ---: | ---: |
> | Align stage, wall clock | 812 s | **521 s (1.56× faster)** |
> | Aligner alone | 759 s | **460 s (1.65×)** |
> | Peak process memory (whole stage) | **5.2 GiB** | 7.4 GiB |
> | Index build, chr20 | 52 s, 93 MiB | 28 s, 1.5 GiB |
> | hap.py, bcftools and DeepVariant | identical | identical |
>
> - **The calls are equivalent.** hap.py gives the same TP/FN/FP for both aligners with both callers. Comparing the PASS calls directly: bcftools 139,132 identical with none different; DeepVariant 109,228 shared, 2 only with bwa, none only with bwa-mem2. Both BAMs have the same 18,997,779 reads and 2,083,409 marked duplicates.
> - **The price is memory:** about 2.3 GiB more at peak, and a much larger index (1.5 GiB of RAM to build for one chromosome, ~90 GB for a whole genome). bwa-mem2 spent 159 s of its 460 s writing SAM into the pipe, so `sort` and `markdup` downstream partly limit the gain.
> - **Caveats:**
>   - One run each, bwa first. The stage wall clock includes copying the reads into the container, so bwa-mem2 may have read them from a warm page cache. The aligners' own timers start after that copy, so the 1.65× ratio is not affected by run order.
>   - bwa-mem2 2.3 reports itself as 2.2.1, so the CSV's version column says 2.2.1.
>   - Against a chr20-only reference, reads from other chromosomes have nowhere else to go. DeepVariant's SNP false positives rise from 52 on Google's whole-genome BAM to 120 here, for both aligners alike.
>
> Results: `benchmarks/aligners_<base>.csv` and `benchmarks/aligners_<base>/summary.md` under the storage root:
>
> ```bash
> python scripts/fetch_dv_testdata.py --dataset hg003-chr20
> python scripts/benchmark_aligners.py --root <root> --from-bam alignments/HG003_chr20.bam \
>     --base HG003_chr20 --reference GRCh38_chr20.fa --region chr20
> ```
>
> **Scatter-gather variant calling** (`--shard-window BP`) is how the cloud will spread variant calling over many jobs. `tasks.py regions <ref> --window BP` cuts the reference into regions in reference order. `tasks.py variants <id> <ref> --region R` calls one region into `intermediate/variant-parts/<id>/<run>/`; there's one folder per run (`RUN_NAME`), so a merge never picks up an earlier run's parts. Everything under `intermediate/` is disposable and expires after 7 days (S3 lifecycle, #62). `tasks.py variants <id> <ref> --window BP` merges every part into the usual `variants/<id>.vcf.gz` (+ `.tbi` and stats), and fails, naming them, if any parts are missing. bcftools reports each position in exactly one region, so the merged VCF has the same records as a single job; the Docker E2E test checks this. Shards record the `CallVariantsShard` metric and the merge records `CallVariants`. The Step Functions `Map` state that runs the shards in parallel in the cloud comes in a separate infrastructure PR.

**Tests:**

```bash
pip install -r requirements-dev.txt
pytest                                           # unit tests, no Docker needed
GEYSER_E2E=1 pytest tests/test_e2e_docker.py     # full pipeline in Docker
```

The tests also check that the stage list in the Step Functions definition (`infrastructure/state_machine.asl.json`, which `step_functions.tf` renders with `templatefile()`), the stage code (`app/tasks.py`) and the local runner all agree. `tests/test_state_machine_local.py` runs that same definition in [Step Functions Local](https://docs.aws.amazon.com/step-functions/latest/dg/sfn-local.html) (Docker) with Batch and SNS mocked: the happy path (QC and alignment in parallel, then variants), each stage failing, and a failed notification still ending the run as Failed (`GEYSER_SFN_LOCAL=1`; CI runs it on every pull request).

**Quality gate (Rust):** [`gate/`](../gate/README.md) reads a run's QC statistics (fastp's report and `stats/<id>/`) and run record, checks them against thresholds from a TOML file (base quality, GC, reads lost in alignment, mapped %, duplicates, properly paired, mean depth, breadth at 10x, SNP count, Ti/Tv) and exits pass or fail with a JSON verdict that names each failed check. The default thresholds were checked on HG003 and HG005 chr20. It is in the image and runs as `tasks.py gate <id>` after variant calling: locally the last job of `run_local.py`, in the cloud a state after either caller (bcftools or DeepVariant). The verdict is stored in `qc_reports/<id>.gate.json` and the run record. In the cloud it runs in **fail** mode since #157 (Terraform `gate_mode`, [gate/README.md](../gate/README.md#warn-first-then-fail)): a failed verdict fails the run, with the gate's reasons in the failure notification. Locally `run_local.py` still defaults to warn. Develop it with `cd gate && cargo test`.

The storage and metrics backends are chosen by environment variables, so the same code runs locally and in AWS:

| Variable | Values | Default |
| :--- | :--- | :--- |
| `STORAGE_BACKEND` | `s3`, `local` | `s3` |
| `LOCAL_STORAGE_DIR` | path to the local data lake | `local_bucket` |
| `S3_ENDPOINT_URL` | an S3-compatible endpoint (OVHcloud Object Storage, MinIO, LocalStack) instead of AWS; boto3's `AWS_ENDPOINT_URL_S3` and `AWS_ENDPOINT_URL` count too. Set `AWS_REGION` to the service's region (e.g. `gra`) and its keys in `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` | unset (AWS) |
| `S3_ADDRESSING_STYLE` | `path`, `virtual`, `auto` | unset (botocore's `auto`) |
| `METRICS_BACKEND` | `cloudwatch`, `local`, `none` | `cloudwatch` for S3 on AWS, `none` for S3 outside AWS, `local` otherwise |

---

[← Back to the README](../README.md)
