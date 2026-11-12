# Long reads (PacBio HiFi, Oxford Nanopore)

The pipeline was built for short Illumina reads. One setting, `READ_TYPE`, adds a long-read route through the
same stages, the same storage layout and the same run record.

| `READ_TYPE` | Reads | Aligner | Duplicates | bcftools mpileup | DeepVariant model |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `short` (default) | Illumina, single- or paired-end | bwa-mem2 (or `ALIGNER=bwa` / `minimap2 -ax sr` / `giraffe`) | marked | defaults | `WGS` |
| `hifi` | PacBio HiFi (CCS), single-end | `minimap2 -ax map-hifi` | not marked | `-X pacbio-ccs` | `PACBIO` |
| `ont` | Oxford Nanopore R10.4.1 (Q20+), single-end | `minimap2 -ax lr:hq` | not marked | `-X ont` | `ONT_R104` |

The table lives in one place, `READ_TYPES` in [`app/tasks.py`](../app/tasks.py). Every stage reads it, so the
aligner preset, the duplicate handling, the caller's error model and DeepVariant's model can't disagree.

## Running it

```bash
# Synthetic HiFi-like reads (~5 kb, ~0.1% errors, a third of them 1 bp indels) with 10 planted SNPs
python scripts/make_test_data.py --sample HIFI001 --long-reads
# All stages in Docker, then the VCF against the truth: 10/10, nothing else
python scripts/run_local.py --sample HIFI001 --reference synthetic_ref.fa --read-type hifi --check
# Long reads called with DeepVariant (the PACBIO model, picked from READ_TYPE; the DeepVariant image's own
# end-to-end test doesn't cover long reads yet: the HG002 benchmark exercises this path)
python scripts/run_local.py --sample HIFI001 --reference synthetic_ref.fa --read-type hifi --caller deepvariant --check
```

Setting `READ_TYPE=hifi` (or `ont`) is enough: the aligner then defaults to minimap2. `ALIGNER=minimap2 READ_TYPE=hifi`
says the same thing explicitly, which is what the long-read benchmark uses.

## Decisions

- **minimap2 2.28, pinned.** It is the upstream static binary, checksum-pinned in the [Dockerfile](../Dockerfile)
  like bwa-mem2 and mosdepth. The version matches the biocontainer the long-read benchmark uses, so the pipeline
  and the benchmark align with the same build. The run record lists both the version the binary reports and the
  pinned release (`minimap2 release`).
- **No stored minimap2 index.** A `.mmi` index is built for one preset's k-mer and window sizes, so storing one
  would mean one file per preset. minimap2 indexes a chromosome in seconds and a whole genome in a few minutes,
  which is small next to aligning the reads. So `reference/` keeps no minimap2 index, and the bwa-mem2#262
  fallback (a bwa-mem2 bug) does not apply. This holds for a chromosome. For a whole genome, building the index
  in the job takes several GB of memory on top of the alignment, which is one reason the cloud route below needs
  its own sizing (or one stored `.mmi` per long-read preset).
- **Long reads are single-end.** A paired upload (`_R1`/`_R2`) with `READ_TYPE=hifi|ont` is a `LayoutError`,
  raised by QC and by align before anything is downloaded. A short-read aligner (`bwa-mem2`, `bwa`, `giraffe`)
  with a long read type is refused the same way.
- **Variant calling follows the alignment.** The align stage records its read type in the run record. Calling
  (bcftools or DeepVariant) uses that value, so a re-run of the calling stages, or a later stage that wasn't given
  `READ_TYPE`, still calls HiFi reads as HiFi. A `READ_TYPE` that contradicts the record fails before any
  download, instead of quietly calling with the wrong error model.
- **No duplicate marking for long reads.** Long-read libraries are PCR-free, and their fragments are sheared at
  random over kilobases. Two reads that start at the same base on the same strand are almost never copies of
  one molecule. `samtools markdup` has only position and strand to go on for single-end reads, so it would still
  flag those coincidences. The align stage sorts and writes the CRAM directly. The run record says
  `duplicates_marked: false`.
- **ONT means R10.4.1, Q20+.** `lr:hq` is minimap2's preset for accurate long reads (2.27+). `ONT_R104` is the
  DeepVariant model for that chemistry. Older, noisier ONT data would want `map-ont` and another caller.
- **DeepVariant follows the read type.** `DEEPVARIANT_MODEL_TYPE` still works on its own, but it must fit the read
  type: `WGS` or `WES` for short reads, `PACBIO` for `hifi`, `ONT_R104` for `ont`. `READ_TYPE=hifi` with
  `DEEPVARIANT_MODEL_TYPE=WGS` fails before any download. Model names are case-insensitive. Pangenome-aware
  DeepVariant is a short-read caller, so it refuses long reads.
- **QC still uses fastp.** It reads multi-kb reads, and the report's totals and base qualities are right. Its
  per-cycle plots and duplication estimate are short-read measures, though, and mean little for long reads.
  fastplong is the long-read equivalent; switching QC to it is future work.

## What the run record shows

`runs/<id>.provenance.json` gains `read_type`, `minimap2_preset` (for minimap2) and `duplicates_marked`. It also
lists `minimap2` with its version under `tools`.

## Tested

- **Unit tests** ([`tests/test_long_read.py`](../tests/test_long_read.py)): aligner defaults and refusals, the
  presets, the paired-long-read layout error, the command with no markdup, mpileup's `-X`, DeepVariant's model
  (including the mismatch error), calling as aligned, QC's layout refusal, the run record, `run_local --read-type`
  and `make_test_data --long-reads`.
- **Docker end-to-end** ([`tests/test_e2e_docker.py`](../tests/test_e2e_docker.py), `test_long_reads_*`): the real
  minimap2, samtools and bcftools in the image recover all 10 planted SNPs and call nothing else. No read is
  flagged as a duplicate. Sharded calling gives exactly the single job's records.

## In the cloud

The state machine takes an optional `read_type` in the execution input (`short`, `hifi` or `ont`; left out means
`short`). Start a long-read run by hand:

```bash
aws stepfunctions start-execution --state-machine-arn <arn> --name <sample>-<UTC time>-<hash> \
  --input '{"srr_id": "HIFI001", "reference_name": "chr20.fa", "read_type": "hifi", "caller": "deepvariant"}'
```

- `hifi` or `ont` sets the aligner to minimap2 (`ALIGNER=minimap2`, `READ_TYPE` on the align job). An explicit
  `aligner` other than `minimap2` (bwa-mem2, giraffe) fails before any job with `AlignerNotForLongReads`, and an
  unknown `read_type` fails with `UnknownReadType`, so a typo never calls long reads with a short-read model.
- `READ_TYPE` also reaches every calling job: DeepVariant (single job, split make_examples parts and the call job)
  picks its model from it, and the bcftools shards and merge pick mpileup's error model.
- **bcftools on long reads is allowed, as a weaker fallback.** `caller` left out (bcftools) or DeepVariant's
  bcftools fallback both call with `-X pacbio-ccs` / `-X ont`. DeepVariant is the caller to use for long reads.
- The large (chunked) route aligns with bwa-mem2 only, so it refuses a long-read input (`AlignerNotOnLargeRoute`).

### Cloud checkpoint (CLOUDLR001, 12 Nov 2026)

4,199 HG002 HiFi reads overlapping chr20:10,000,001-12,000,000 (cut from the 30x data above), started by hand with
`read_type: hifi` and `caller: deepvariant`. Every stage succeeded on its first Batch attempt: QC (fastp), align
(minimap2 `map-hifi`, 33 s on 4 vCPU / 8 GB, peak 660 MiB) and DeepVariant (`--model_type=PACBIO`, 1.3 min on a
g6.4xlarge, NVIDIA L4, after 4.5 min for the instance to boot). Wall time 8.8 min; about $0.006 of Fargate plus the
GPU instance. The run record says `read_type: hifi`, `minimap2_preset: map-hifi`, `duplicates_marked: false`.

| Calls (hap.py, GIAB v4.2.1, in the window) | SNP TP / FN / FP | SNP F1 | Indel TP / FN / FP | Indel F1 |
| :--- | :--- | ---: | :--- | ---: |
| Cloud (CLOUDLR001) | 2,581 / 0 / 0 | 1.000000 | 425 / 1 / 1 | 0.997700 |
| Local 30x DeepVariant, same window | 2,581 / 0 / 0 | 1.000000 | 425 / 1 / 1 | 0.997700 |

The two VCFs hold the same 3,367 PASS records with the same genotypes; 243 differ only in phase orientation
(`0|1` vs `1|0`), which DeepVariant's phasing leaves arbitrary.

**The execution ends FAILED, at the quality gate, by design.** The reads cover 2 Mb of chr20's 64.4 Mb, so the
mean depth over the chromosome is 0.925 (minimum 20) and 3% of it is covered at 10x (minimum 90%). The gate is
right to reject that as too shallow for a chromosome. Every other check passed (Q30 97.2%, 100% mapped, no
duplicates, Ti/Tv 2.15). No gate profile describes a windowed real-data checkpoint (`smoke` expects the simulated
50 kb CLOUD0xx data), and none was added: a possible follow-up, noted on #295.

## Not yet

- **Uploads can't say they are long reads.** The trigger Lambda starts every upload as a short-read run; a
  `read-type` metadata key on the upload is the follow-up (#295). Until then, start long-read runs by hand as
  above. The upload to `raw_reads/` still starts its own short-read run, so give it a `gate-profile` the state
  machine rejects (`--metadata gate-profile=manual-start`): that run fails before any job, at no cost.
- **Real-data accuracy.** The benchmark on GIAB HG002 HiFi reads (minimap2 + DeepVariant `PACBIO`, scored with
  hap.py) is run separately and recorded in the [benchmark history](benchmarks/README.md).

---

[← Back to the README](../README.md)
