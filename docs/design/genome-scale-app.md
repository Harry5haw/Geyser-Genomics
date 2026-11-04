# v1.4 "Big inputs": the app half (pre-flight, chunked alignment, streaming merge, region-streamed calling)

The interface contract between `app/tasks.py` (this half) and the state machine (the infra half). The plan, with its
sizes, costs and stop rules, is the v1.4-v2.0 genome-scale plan. Everything here works offline against local
storage, as every stage does. **A change to this contract is announced to the coordinator before it is pushed.**

## Commands

All are subcommands of `python tasks.py` in the pipeline image, like `regions` and `finalize`:

| Command | Does | Exit |
| :--- | :--- | :--- |
| `preflight <srr_id> [--chunks N] [--manifest-key KEY]` | One streaming pass over the raw reads (both mates for paired-end): gzip integrity, read count, mates in step, cut into N chunks of equal read count. Writes the chunks, the manifest and the pre-flight report. N defaults to 24 (1-999). `--manifest-key` defaults to the key below | 0 ok; 1 the input failed the check (`PreflightFailed: …` in the log; the report is still written); 2 usage |
| `align_chunk <srr_id> <reference_name> --chunk K` | Chunk K of this run's manifest (K is the manifest's `index`, 0-based): `<aligner> mem` with the sample's one read group, `samtools fixmate -m` (paired only), `samtools sort`. **No markdup** (duplicates can sit in different chunks). Writes the chunk BAM | 0 / 1 failure / 2 usage |
| `align_merge <srr_id> <reference_name> [--threads N]` | `samtools merge` of every chunk BAM in the manifest, read in place (S3: presigned HTTPS URLs; local: the files), then `samtools markdup -s`, then CRAM + `.crai`, flagstat and mosdepth, with N samtools threads (default: the job's CPUs). **The outputs and the run-record part are today's `align`'s**, and the `.crai` records the CRAM's ETag | 0 / 1 failure (a missing chunk BAM is named) / 2 usage |
| `variants <srr_id> <reference_name> --region R` (unchanged CLI) | Now **reads only its region** of the CRAM when the stored `.crai` carries the CRAM's current ETag (htslib over a presigned URL, or the local file). Otherwise, or if streaming fails, it downloads and re-indexes as today. `GEYSER_STREAM_REGIONS=0` turns streaming off | unchanged |

**`RUN_NAME`** (the execution name, which the state machine already sets on every job) names the run folder.
`preflight`, `align_chunk` and `align_merge` of one run must share it.

## Storage keys

`{run}` is `RUN_NAME` with anything outside `[A-Za-z0-9._-]` replaced by `_` (execution names are already safe),
or `unnamed`.

| Key | Written by | Kept |
| :--- | :--- | :--- |
| `intermediate/chunks/{id}/{run}/manifest.json` | preflight | 3 days (intermediate lifecycle) |
| `intermediate/chunks/{id}/{run}/chunk-NNN.fastq.gz` (single-end), or `chunk-NNN_R1.fastq.gz` + `chunk-NNN_R2.fastq.gz` (paired-end) | preflight | 3 days |
| `intermediate/aligned/{id}/{run}/chunk-NNN.bam` | align_chunk | 3 days |
| `qc_reports/{id}.preflight.json` | preflight (also when the check fails) | kept |
| `alignments/{id}.cram`, `alignments/{id}.cram.crai` (+ `stats/{id}/flagstat.txt`, `mosdepth.*`) | align_merge: **identical to today's align** | kept |

- `NNN` is the chunk index, zero-padded to 3 digits (`chunk-000`).
- The `.crai` carries the CRAM's ETag as object metadata `cram-etag` (S3 user metadata `x-amz-meta-cram-etag`).
  Today's single-job align writes it too, so old CRAMs without it simply aren't streamed.

## The manifest (`manifest.json`, schema 1)

```json
{
  "schema": 1,
  "sample_id": "HG003-WGS30x",
  "run_name": "HG003-WGS30x-20261110T090000Z-1a2b3c4d",
  "layout": "paired",
  "reads": 401234567,
  "chunk_count": 24,
  "input": {"keys": ["raw_reads/HG003-WGS30x_R1.fastq.gz", "raw_reads/HG003-WGS30x_R2.fastq.gz"], "bytes": 52990000000},
  "aligned_prefix": "intermediate/aligned/HG003-WGS30x/HG003-WGS30x-20261110T090000Z-1a2b3c4d/",
  "chunks": [
    {"index": 0, "name": "chunk-000", "reads": 16718524, "bytes": 1234567890,
     "keys": {"raw_R1": "intermediate/chunks/HG003-WGS30x/<run>/chunk-000_R1.fastq.gz",
              "raw_R2": "intermediate/chunks/HG003-WGS30x/<run>/chunk-000_R2.fastq.gz"},
     "aligned": "intermediate/aligned/HG003-WGS30x/<run>/chunk-000.bam"}
  ],
  "created": "2026-11-10T09:45:00Z"
}
```

- `reads` counts fragments: reads per mate for paired-end, reads for single-end. Chunk sizes differ by at most 1.
- `keys` uses `keys_for`'s names: `raw` for single-end, `raw_R1` / `raw_R2` for paired-end. `bytes` is the
  chunk's compressed size (both mates).
- The state machine reads the manifest at its documented key, `intermediate/chunks/{srr_id}/{execution name}/manifest.json`
  (execution names are already in the safe set, so `{run}` is the execution name), with a `getObject` +
  `States.StringToJson`, as `Read_Regions` does. Then a **Map over `$.chunks`** runs one `align_chunk … --chunk <index>`
  per item. `index` is an integer: pass it as `States.Format('{}', …)`. Every other key comes from the manifest.

## The pre-flight report (`qc_reports/{id}.preflight.json`, schema 1)

```json
{"schema": 1, "sample_id": "…", "run_name": "…", "layout": "paired", "ok": true, "error": null,
 "input": {"keys": ["…"], "bytes": 52990000000}, "reads": 401234567, "chunk_count": 24,
 "manifest": "intermediate/chunks/…/manifest.json", "seconds": 2712.4}
```

On a failed check, `ok` is false, `error` names the problem (truncated or corrupt gzip, an empty input, mates
with different read counts, a malformed FASTQ record), and there is no manifest.

## Job names, metrics, run-record parts

| Stage | Job name (state machine) | `JOB_NAME_STAGES` | Metric | Run-record part |
| :--- | :--- | :--- | :--- | :--- |
| preflight | `Preflight-<srr_id>-<execution>` | `("Preflight-", "Preflight")` | `Preflight` | `Preflight` (reads, chunks, bytes) |
| align_chunk | `AlignChunk-<index>-<execution>` | `("AlignChunk-", "AlignChunk")` | `AlignChunk` | none (like the calling shards) |
| align_merge | `AlignMerge-<srr_id>-<execution>` | `("AlignMerge-", "Align")` | `Align` (as today) | `Align` (as today, plus `chunks`) |

A failed `AlignChunk-…` or `AlignMerge-…` job closes the run as failed in Align. A failed `Preflight-…` closes it
as failed in Preflight.

## Per-run parallelism (the #192 overrides pattern)

The keys go in the run input's nested `overrides` object. `Run_Setting_Rules` validates them (JSON integers, whole
numbers), and `Set_Run_Settings` puts them in `$.run_settings`. The bounds are derived in Terraform from the
**Fargate Spot** cap, so a large run stays on Spot. A faster run needs the user to raise the cap first.

| Key | Default | Bounds | Used by |
| :--- | ---: | :--- | :--- |
| `chunk_count` | 24 | 2-96 | `preflight --chunks` (the app accepts 1-999) |
| `align_concurrency` | 5 | 1 … floor((fargate_spot_max_vcpus − QC_Large's 8) / the chunk's 8 vCPU) = 5 at 48 | the Align_Chunks Map's `MaxConcurrencyPath` |
| `calling_concurrency` | `variants_max_concurrency` (4) | 1 … floor(fargate_spot_max_vcpus / the shard's vCPU) = 48 | the calling Map's `MaxConcurrencyPath` |
| `merge_threads` | 8 | 1 … the merge job's vCPU (8) | `align_merge --threads` |

QC_Large runs alongside the chunk Map, so the chunks get 40 of the 48 vCPU: 5 at a time, not the plan's 6.

## What the jobs need (for the infra half)

- **Reads:** `raw_reads/*`, `intermediate/chunks/*`, `intermediate/aligned/*`, `alignments/*`, `reference/*`.
- **Writes:** `intermediate/chunks/*`, `intermediate/aligned/*`, `alignments/*` (with object metadata),
  `stats/*`, `qc_reports/*`, `runs/*`. The job role already covers these if it has `s3:GetObject`/`s3:PutObject`
  on the data lake.
- **Presigned URLs** are signed with the job role's own credentials, so they need no new permission. The URLs
  live 6 hours (longer than the merge's timeout).
- **Sizes** (from the plan): preflight 8 vCPU / 16 GB / 200 GB disk; align_chunk 8 vCPU / 32 GB / 100 GB;
  align_merge 8 vCPU / 16 GB / 200 GB; shards unchanged.
