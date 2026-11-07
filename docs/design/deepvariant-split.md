# Design: DeepVariant split into CPU `make_examples` parts and one GPU job (v1.5)

Status: **app half built and tested offline** (this note's PR). The infra half (a Fargate job definition for the
CPU image and a `Map` state in front of the GPU job) is a separate infrastructure PR, sized from a cloud canary.

## Why

On the first real-size cloud run (`REAL-HG003-30x-chr20-DV-20261104T085535Z`, g6.xlarge, 4 Nov 2026), the
DeepVariant job ran 1,200 s:
- **`make_examples` took 1,007 s (84%)** and used only the CPU;
- the GPU was busy for 4% of the job, and 32% of `call_variants`.

Every one of those seconds is billed at the GPU instance's rate. Running `make_examples` as parallel CPU jobs on
Fargate Spot, and only `call_variants` + `postprocess_variants` on the GPU, is the "lever" in the genome-scale plan
(§4.2): about $5 instead of about $9.50 for a 30x genome, at the cost of moving the examples through S3.

## The jobs

| Job | Image | Command |
| :--- | :--- | :--- |
| make_examples part `P` of `J` (J in parallel) | `geyser-deepvariant-cpu` (DeepVariant 1.10.0, no GPU) | `deepvariant_task.py <id> <ref> --make-examples-part P --parts J` |
| the GPU job, after every part | `geyser-deepvariant` (1.10.0-gpu) | `deepvariant_task.py <id> <ref> --from-examples` |

The single-job stage (`deepvariant_task.py <id> <ref>`) is unchanged and stays the default. Pangenome-aware
DeepVariant has no split mode.

**Tasks.**
- `DEEPVARIANT_SHARDS` is the number of `make_examples` tasks in all, `N`. Its default is `J` × the part's CPUs.
- Part `P` runs tasks `P·N/J` to `(P+1)·N/J − 1` (integer division): contiguous, and as even as possible.
- It runs **the wrapper's own `make_examples` command** from `run_deepvariant --dry_run=true`, with the
  `seq 0 N-1` narrowed to its range. The flags, the model and the `@N` output pattern are exactly the single job's.
- DeepVariant names every per-task file `…-TTTTT-of-NNNNN…`, so parts never write the same file.

**The GPU job:**
- runs the wrapper's own `call_variants` and `postprocess_variants` commands for `N` tasks, with
  `postprocess_variants --cpus` set to its own CPUs (the wrapper writes `N` there);
- needs the reference, but **not the alignment**: the wrapper's dry run doesn't check that the reads exist, and
  the examples already contain everything.

## Keys (all under `intermediate/`, which the lifecycle rule expires)

```
intermediate/dv/{sample_id}/{RUN_NAME}/shard-{P}/make_examples.tfrecord-TTTTT-of-NNNNN.gz
                                                 make_examples.tfrecord-TTTTT-of-NNNNN.gz.example_info.json
                                                 make_examples_call_variant_outputs.tfrecord-TTTTT-of-NNNNN.gz
                                                 manifest.json          (written last)
```

- **Per run:** `RUN_NAME` (the execution name; `unnamed` without one), like the bcftools shard parts. A re-run
  never sees another run's examples. The job role can't delete objects, so old runs' examples stay until they
  expire.
- **`manifest.json` is written last,** so a manifest means its files are complete. A failed part leaves no
  manifest.

### The manifest (`split_version` 1)

| Field | Meaning |
| :--- | :--- |
| `split_version` | 1 |
| `sample_id`, `run_name` | whose examples these are |
| `deepvariant` | the wrapper's version (`--version`); a part that can't tell fails |
| `shards`, `parts`, `part`, `tasks` | `N`, `J`, `P` and `[first, last]` |
| `model_type`, `extra_args` | `DEEPVARIANT_MODEL_TYPE`, `DEEPVARIANT_EXTRA_ARGS` |
| `reference`, `alignment` | `[name, bytes, sha256 of the first and last 64 MiB]`, as the checkpoint's fingerprint |
| `files` | `[{name, bytes}]` |
| `examples`, `small_model_calls` | `make_examples`' own counts (what goes to the GPU, what the small model already called on the CPU) |
| `make_examples_seconds`, `cpus` | the part's `make_examples` wall time and its CPUs |

## What the GPU job checks, before downloading anything but the reference

1. At least one manifest, all with `split_version` 1.
2. All parts agree on `deepvariant`, `shards`, `parts`, `model_type`, `extra_args`, `reference` and `alignment`.
3. Parts `0` to `J−1` are all there, and their tasks cover `0` to `N−1` exactly once.
4. Its own DeepVariant version, `DEEPVARIANT_MODEL_TYPE` and `DEEPVARIANT_EXTRA_ARGS` equal the parts'. The
   environment must be the same in both jobs, `call_variants`' `batch_size` included.
5. After downloading the reference, it equals the parts' (same fingerprint).

Any failure is a `ValueError` saying which check failed. The GPU never starts.

## Outputs: identical to the single job

| | Split mode |
| :--- | :--- |
| `variants_deepvariant/{id}.vcf.gz` | the same key |
| `stats/{id}/bcftools.stats.deepvariant.txt` (PASS calls; also the sample's `bcftools.stats.txt` when DeepVariant is the run's caller) | the same |
| Run record | one `DeepVariant` part, written by the GPU job, as before, plus `make_examples: {split, parts, shards, examples, small_model_calls, part_seconds, part_cpus, bytes}` and the examples prefix under `inputs`. The parts write **no** run-record part (J jobs would overwrite one file), like the bcftools region shards. |
| Metrics | each part records `DeepVariantExamples`. The GPU job records `DeepVariant.MakeExamples` (the **slowest** part, the critical path), `DeepVariant.CallVariants` and `DeepVariant.PostprocessVariants` with the GPU figures (#186), and `DeepVariant`. |
| GPU sampling (#186) | unchanged: `call_variants` and `postprocess_variants` on the GPU job |

**`batch_size`: DeepVariant's default** (1,024). The cloud L4 (23 GB) held it at 21.1 GB on the chr20 run. The
batch 256 of RP-1's amendment A1 is for that study only.

## What the infra half needs (not in this PR)

- **A Fargate job definition** for `geyser-deepvariant-cpu`, pushed to ECR.
  - The image is ~7.4 GB uncompressed, so expect minutes of pull per part. Measure it on the canary.
  - **Disk per part:** the CRAM (each part downloads it whole today; v1.4's presigned range reads would make
    that a stream), the reference, and its share of the examples. Chr20 30x is about 0.4 GB of CRAM; a 30x
    genome is about 17 GB, so a whole-genome part needs `ephemeralStorage` well above the 20 GB default.
- **The state machine:**
  - `Choose_Caller` → (deepvariant, split) → a `Map` over `parts` (`make_examples` parts, Spot first, like the
    bcftools shards) → the GPU Task with `--from-examples`;
  - the same `DEEPVARIANT_*` model and flags on both (check 4). The part count and task count (`parts`,
    `shards`) come from the manifests, so the GPU job's own `DEEPVARIANT_SHARDS` doesn't matter there;
  - a failed part (the `Map`'s Catch) and a failed GPU Task both fall back to bcftools with
    `$.deepvariant_error`, as a failed single DeepVariant job does today. DeepVariant is an extra caller
    (`provenance.EXTRA_CALLERS`), so its failure never fails the run (#236).
- **The SFN role** needs nothing new: the jobs read and write S3 themselves.

## Tests

- **Unit** (`tests/test_deepvariant_split.py`, 37 tests): the task split, the narrowed command (on the real
  1.10.0 dry run, `tests/fixtures/deepvariant/dry-run-4-shards.txt`), the replayed GPU steps and their `--cpus`,
  a part's uploads with the manifest last, and a part that wrote the wrong tasks. Also the GPU job end to end with
  faked tools (same output keys, metrics, run-record part, no alignment download), every refusal in the list
  above, another run's examples never used, a failed GPU step named, the CLI and `run_local.py`.
- **Docker** (`tests/test_e2e_deepvariant.py::test_split_deepvariant_matches_the_single_job`, opt-in): synthetic
  sample, 2 parts in the CPU image, then the GPU job. 10/10 planted SNPs, a complete run record, gate PASS.
- **Local GPU, real data** (4 Nov 2026): HG003 chr20 35x, 2 parts × 8 tasks (16 in all), then the GPU job at DeepVariant's
  defaults (batch 1,024), on an RTX 3080.
  - **The same examples as the single job:** 39,978 + 39,781 = **79,759**, exactly the count of every 16-shard single
    job in the batch × shards grid (#216).
  - **Calls:** PASS 85,652 SNPs, 19,883 indels, 105,402 records. That's inside the single jobs' run-to-run range
    (85,652-85,653 / 19,883-19,884 / 105,402-105,404), which comes from GPU inference noise.
  - **Parts:** 268 s and 256 s (`make_examples` 241 s and 230 s), peak RSS about 1.1 GB each.
  - **The GPU job:** 167.5 s (`call_variants` 130 s, `postprocess_variants` 21 s), GPU 28.8% busy in `call_variants`,
    9.5 GB of VRAM.
  - **Sizing:** the GPU job peaked at **11.4 GB of host memory** (TensorFlow at batch 1,024), so it needs a memory
    limit of 14 GB or more. The parts fit in a few GB.

## Locally

```bash
docker build -f app-deepvariant/Dockerfile --build-arg DV_TAG=1.10.0 -t geyser-deepvariant-cpu .
DEEPVARIANT_SHARDS=16 python scripts/run_local.py --sample HG003_chr20 --reference GRCh38_chr20.fa \
    --caller deepvariant --skip-to-variants --dv-split 2 --dv-memory 12g
```
