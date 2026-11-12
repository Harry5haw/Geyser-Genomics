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
  fallback (a bwa-mem2 bug) does not apply.
- **Long reads are single-end.** A paired upload (`_R1`/`_R2`) with `READ_TYPE=hifi|ont` is a `LayoutError`,
  raised before anything is downloaded. A short-read aligner (`bwa-mem2`, `bwa`, `giraffe`) with a long read type
  is refused the same way.
- **No duplicate marking for long reads.** Long-read libraries are PCR-free, and their fragments are sheared at
  random over kilobases. Two reads that start at the same base on the same strand are almost never copies of
  one molecule. `samtools markdup` has only position and strand to go on for single-end reads, so it would still
  flag those coincidences. The align stage sorts and writes the CRAM directly. The run record says
  `duplicates_marked: false`.
- **ONT means R10.4.1, Q20+.** `lr:hq` is minimap2's preset for accurate long reads (2.27+). `ONT_R104` is the
  DeepVariant model for that chemistry. Older, noisier ONT data would want `map-ont` and another caller.
- **DeepVariant follows `READ_TYPE`.** `DEEPVARIANT_MODEL_TYPE` still works on its own. Set together with
  `READ_TYPE`, the two must agree (`READ_TYPE=hifi` with `DEEPVARIANT_MODEL_TYPE=WGS` fails before any download).
  Pangenome-aware DeepVariant is a short-read caller, so it refuses long reads.

## What the run record shows

`runs/<id>.provenance.json` gains `read_type`, `minimap2_preset` (for minimap2) and `duplicates_marked`. It also
lists `minimap2` with its version under `tools`.

## Tested

- **Unit tests** ([`tests/test_long_read.py`](../tests/test_long_read.py)): aligner defaults and refusals, the
  presets, the paired-long-read layout error, the command with no markdup, mpileup's `-X`, DeepVariant's model
  (including the mismatch error), the run record, `run_local --read-type` and `make_test_data --long-reads`.
- **Docker end-to-end** ([`tests/test_e2e_docker.py`](../tests/test_e2e_docker.py), `test_long_reads_*`): the real
  minimap2, samtools and bcftools in the image recover all 10 planted SNPs and call nothing else. No read is
  flagged as a duplicate. Sharded calling gives exactly the single job's records.

## Not yet

- **The cloud route.** The trigger and the state machine don't pass `READ_TYPE` yet, so cloud runs are short-read.
  That needs a small infrastructure change, and the large (chunked) route stays bwa-mem2 only. Locally, everything
  above runs.
- **Real-data accuracy.** The benchmark on GIAB HG002 HiFi reads (minimap2 + DeepVariant `PACBIO`, scored with
  hap.py) is run separately and recorded in the [benchmark history](benchmarks/README.md).

---

[← Back to the README](../README.md)
