# geyser-gate: the quality gate

A run can "succeed" and still be useless: poor base qualities, 60% of reads mapped, 5x depth, or a VCF
full of errors all end with a green Step Functions run. `geyser-gate` reads the QC statistics and the run record a run
already leaves behind and gives a verdict: **pass**, or **fail with a named reason**, e.g.
`mapped_pct 80 is below the minimum 95`.

It doesn't re-run any tool. It only reads the tools' own output files and applies thresholds from a
small TOML file. It is written in Rust because it is meant to ship as one static binary
(1 MB, `x86_64-unknown-linux-musl`) that can be copied into the pipeline image without adding a runtime.

> **Status:** in the pipeline image and run by `tasks.py gate <id>` after variant calling (#144):
> locally by `run_local.py`, in the cloud by a Step Functions state after either caller. **Warn** mode
> for now: the verdict is stored and recorded, and a failed verdict doesn't fail the run. See
> [In the pipeline](#in-the-pipeline) and [Warn first, then fail](#warn-first-then-fail).

## Inputs

The same keys the pipeline writes, under a folder laid out like the bucket (`--root`, e.g. `local_bucket/`):

| Input | Key | Written by | Gives |
| :--- | :--- | :--- | :--- |
| `fastp` | `qc_reports/<id>.fastp.json` | qc (`fastp`, report-only) | `q30_pct`, `gc_pct`, `fastp_duplicate_pct` |
| `flagstat` | `stats/<id>/flagstat.txt` | align (`samtools flagstat`) | `reads`, `mapped_pct`, `duplicate_pct`, `properly_paired_pct`; with fastp: `read_count_difference` |
| `mosdepth_summary` | `stats/<id>/mosdepth.summary.txt` | align (`mosdepth -n`) | `mean_depth` |
| `mosdepth_dist` | `stats/<id>/mosdepth.global.dist.txt` | align (`mosdepth -n`) | `breadth_1x_pct`, `breadth_10x_pct`, `breadth_20x_pct` |
| `bcftools_stats` | `stats/<id>/bcftools.stats.txt` | variants (`bcftools stats`) | `variants`, `snps`, `indels`, `tstv` |
| `run_record` | `runs/<id>.provenance.json` | every stage ([`app/provenance.py`](../app/provenance.py), schema 1) | `run_status` (`succeeded` / `failed` / `incomplete`), run name |

The run record is `runs/<id>.provenance.json`, not `runs/<id>.json` (that file is the local web worker's state).

**Only this run's files count.** Nothing in the bucket is ever deleted (the pipeline has no
`s3:DeleteObject`) and the stats tools are best effort, so a re-run can leave an earlier run's stats
behind. With a run record, the gate therefore uses only what that run produced:

- A stats file that isn't among the record's `outputs` counts as **missing** ("left over from an earlier run").
- The stored record is a convenience copy that `app/provenance.py` rebuilds from the per-stage parts
  (`runs/<id>.provenance/*.json`). If a part finished after the record was assembled (its re-assembly
  failed), the record is **invalid** (stale), so a failed re-run can't pass on an old "succeeded".
- If fastp's or flagstat's layout disagrees with the record's, that input is **invalid**.
- A record for another sample, or with a schema other than 1, is **invalid**.

Without a run record (or one without `outputs`), the stats files are taken as they are.

## Usage

```bash
cd gate
cargo build --release                         # target/release/geyser-gate

# The built-in thresholds (human germline WGS, ~30x)
geyser-gate --root ../local_bucket --sample HG003

# The repo's synthetic test data (make_test_data.py + run_local.py)
geyser-gate --root ../local_bucket --sample SYNTH001 --config thresholds/synthetic.toml

# Verdict to a file, no summary on stderr
geyser-gate --root ../local_bucket --sample SYNTH001 --config thresholds/synthetic.toml -o verdict.json -q

geyser-gate --print-default-config            # start your own thresholds from these
```

**Exit status:** `0` pass, `1` fail, `2` usage or config error (unknown option, unreadable or invalid
config or one that checks nothing, bad sample ID, `--root` not a folder). A missing or unparsable *input* is not an error: it is
reported in the verdict (see `on_missing`).

The verdict JSON goes to stdout (or `--output`); a one-line-per-check summary goes to stderr:

```text
  pass  run_status               succeeded
  pass  q30_pct                  100.0
  pass  gc_pct                   49.4708
  pass  read_count_difference    0
  pass  mapped_pct               100.0
  pass  duplicate_pct            0.04
  pass  properly_paired_pct      100.0
  pass  mean_depth               29.9413
  pass  breadth_10x_pct          100.0
  pass  snps                     10
  FAIL  tstv                     1.5  (tstv 1.5 is below the minimum 1.7)
geyser-gate: PAIRED001: FAIL (1 of 11 checks)
```

Static binary for the image: `rustup target add x86_64-unknown-linux-musl && cargo build --release --target x86_64-unknown-linux-musl`.

## Thresholds

A TOML file. It is the whole set: a check the file doesn't list isn't run. Bounds are inclusive. Unknown
metric names, unknown keys, `min > max` and empty checks are config errors (exit 2), so a typo can't
silently switch a check off.

```toml
on_missing = "fail"            # or "skip"

[run_record]
statuses = ["succeeded"]       # leave the section out to skip the check

[checks]
mapped_pct = { min = 95.0 }
tstv = { min = 1.7, max = 2.4 }
```

**`on_missing`**: what a check counts as when its input file is missing (the stage didn't run, its
best-effort stats tool failed, or the file is from an earlier run) or its value is undefined (Ti/Tv with
no transversions). `fail` (default) or `skip`. An invalid input (unparsable, stale, contradicting the
record) always fails: the gate can't vouch for it. And a verdict where **no check passed** (everything
skipped or not applicable, e.g. a wrong `--root` with `skip`) is a fail.

**Not applicable:** `properly_paired_pct` doesn't apply to single-end reads. It is reported as
`not_applicable` and never fails. The layout comes from flagstat (from the run record if flagstat is unusable).

### The built-in thresholds ([`thresholds/default.toml`](thresholds/default.toml))

For human germline short-read WGS at ~30x, called by the pipeline's bcftools stage. They were checked
against the two GIAB samples used in the benchmarks (`geyser-data`): HG003 and HG005, chr20, 2 x 151 bp,
called with the pipeline's bcftools stage, with fastp run on their reads with the QC stage's own flags.
HG003's alignment is the pipeline's align stage (bwa-mem2) on its reads, from the aligner benchmark.
HG005's is the downloaded 30x BAM, not aligned by this pipeline, and its reads for fastp were extracted
from that BAM. HG003 passes every check with a margin. HG005 passes every check but one,
`read_count_difference`, because its alignment and its "upload" don't match (below). Their stats are
test fixtures (`tests/fixtures/HG00{3,5}_chr20/`).

A third real alignment checks that the defaults aren't tuned to one aligner: HG003's same reads aligned
to the HPRC chr20 **graph** with vg giraffe (the giraffe benchmark, #129) and called with DeepVariant
(`tests/fixtures/HG003_chr20_giraffe/`). It passes every default check: 98.74% mapped, 10.99%
duplicates, 97.66% properly paired, 36.29x, 98% at 10x, read count difference 0, Ti/Tv 1.96 (PASS calls).

| Check | Bound | HG003 chr20 | HG005 chr20 | Why this bound |
| :--- | :--- | ---: | ---: | :--- |
| `run_status` | `succeeded` | | | A failed or unfinished run has nothing to vouch for |
| `q30_pct` | ≥ 80 | 91.39 | 91.76 | Below 80 (a typical Illumina spec for 2 x 150 bp) the sequencing run had a problem |
| `gc_pct` | 35 – 50 | 43.86 | 44.07 | Human genome ~41, chr20 ~44. Outside 35-50 the library is GC-biased or contaminated (bacteria range from ~25 to ~70) |
| `read_count_difference` | = 0 | 0 | +377,078 | Every read in the upload must reach the alignment, exactly. See below |
| `mapped_pct` | ≥ 95 | 99.77 | 99.82 | Below 95: wrong reference, contamination or a broken alignment |
| `duplicate_pct` | ≤ 20 | 10.99 | 10.24 | Above 20 the library is low-complexity or over-amplified, and much of the depth is copies |
| `properly_paired_pct` | ≥ 90 | 99.05 | 96.97 | Paired-end only. Low values mean bad insert sizes, chimeras or the wrong reference |
| `mean_depth` | ≥ 20 | 36.78 | 32.64 | Below 20x a 30x sample loses sensitivity, mostly for heterozygous calls |
| `breadth_10x_pct` | ≥ 90 | 98 | 98 | Below 90 the depth is uneven: a mean of 30x can hide regions too thin to call. chr20 is ~1% N gap, never covered |
| `snps` | ≥ 1 | 118,410 | 109,870 | A complete run with an empty VCF is broken whatever the other numbers say |
| `tstv` | 1.7 – 2.4 | 1.81 | 1.85 | See below |

Reported but not checked by default: `reads` (depends on the sample), `fastp_duplicate_pct` (5.79 / 5.64;
`duplicate_pct` from the alignment is the better measure), `breadth_1x_pct` (99 / 99), `breadth_20x_pct`
(97 / 95), `variants` and `indels`.

**Read count.** bwa-mem2 writes unmapped reads too and `samtools markdup` only marks duplicates, so
the alignment holds exactly the reads fastp counted in the upload: HG003, 18,960,080 = 18,960,080.
`read_count_difference` is that comparison as an exact, signed count (flagstat's primary records,
QC-failed included, minus fastp's total reads), not a percentage: rounded to 4 decimals, 9 reads lost
among 19 million would show as 100.0000%. Below 0 the align stage lost reads (a truncated stream, a
crashed step that still left a file); above 0 the alignment isn't from this upload. The HG005 fixture is
the second case: its "upload" was extracted from the alignment itself, and `samtools fastq` dropped the
377,078 reads whose mates map to other chromosomes, so the alignment has exactly 377,078 reads more.
The check fails on it, as it should for mismatched inputs (a test asserts that this is its only
failure). With samtools < 1.13 (no `primary` line in flagstat) the difference is undefined.

**Ti/Tv.** The textbook value for human WGS is ~2.0-2.1, and a first draft of the defaults used 1.9-2.3.
Measuring first showed that this would have failed every good run: the pipeline's bcftools calls are
**unfiltered**, and their false positives pull the ratio down to 1.81 (HG003) and 1.85 (HG005). For
comparison, on the same chr20 data:

| Call set | Ti/Tv |
| :--- | ---: |
| GIAB v4.2.1 truth (confident regions only) | 2.29 / 2.29 |
| DeepVariant, PASS calls | 2.05-2.07 / 2.05 |
| bcftools, unfiltered (what the pipeline writes) | 1.81 / 1.85 |
| Random errors | ~0.5 |

So the default range is 1.7-2.4: a call set swamped by errors falls below 1.7, and above 2.4 suggests
an exome-like target or over-filtering. Exomes need their own range (~2.6-3.3), and a config for
filtered DeepVariant calls could tighten the lower bound to ~1.95.

### The synthetic profile ([`thresholds/synthetic.toml`](thresholds/synthetic.toml))

For `make_test_data.py` data (10,000 reads, ~30x, 10 planted SNPs, no indels), single- or paired-end:
the alignment and variant bounds the Docker end-to-end test asserts, plus perfect base qualities, ~50% GC
(a random reference), every read in the alignment and ≥ 95% at 10x (100% by default; 98% with
`--leading-gap 1000`). No Ti/Tv check: 10 SNPs are too few for the ratio to mean anything. The default
thresholds fail synthetic data, by design (Ti/Tv 1.5).

### The smoke profile ([`thresholds/smoke.toml`](thresholds/smoke.toml))

For the cloud smoke tests (the CLOUD0xx checkpoints): 10,000 real single-end reads (or 5,000 pairs)
simulated from GRCh38 chr20:10,000,001-10,050,000 with 10 planted SNPs, aligned to the whole of chr20.
Calibrated on every cloud checkpoint with stats, CLOUD005-CLOUD016 (11 runs, all identical). Because 50 kb
of reads are judged over a 64.4 Mb chromosome, the mean depth is about 0.02x and breadth is 0, so there is
no breadth check and the depth bounds (0.015-0.03) only check that the reads landed and were counted once.
GC is real chr20 sequence (35-40%), exactly 10 SNPs and no indels, and the mapping, read-count and
run-record checks are kept. No Ti/Tv check (10 SNPs). `synthetic` fails these runs (GC 37.5 < 45, depth,
breadth), and `smoke` fails real samples and the local synthetic data (depth above 0.03, GC above 40).

## Verdict

```jsonc
{
  "schema": 1,                       // the verdict layout's version
  "gate_version": "0.1.0",
  "sample_id": "PAIRED001",
  "run_name": "PAIRED001-20261102T152430Z-local",   // from the run record (null if none)
  "layout": "paired",
  "verdict": "pass",                 // or "fail"
  "reasons": [],                     // one line per failed check: the named reasons
  "config": "thresholds/synthetic.toml",            // or "built-in default"
  "on_missing": "fail",
  "inputs": {                        // each input: key, status ok | missing | invalid (+ detail: why)
    "flagstat": { "key": "stats/PAIRED001/flagstat.txt", "status": "ok" }, ...
  },
  "metrics": {                       // every metric, checked or not (null if unavailable)
    "q30_pct": 100.0, "gc_pct": 49.4708, "fastp_duplicate_pct": 0.0, "reads": 10000,
    "read_count_difference": 0, "mapped_pct": 100.0, "duplicate_pct": 0.04, "properly_paired_pct": 100.0,
    "mean_depth": 29.9413, "breadth_1x_pct": 100.0, "breadth_10x_pct": 100.0, "breadth_20x_pct": 97.0,
    "variants": 10, "snps": 10, "indels": 0, "tstv": 1.5
  },
  "checks": [                        // the configured checks, run_status first
    { "name": "mean_depth", "status": "pass", "value": 29.9413, "min": 25.0, "max": 31.0,
      "source": "stats/PAIRED001/mosdepth.summary.txt", "reason": null }, ...
    // a metric from two inputs names both: "source": "stats/PAIRED001/flagstat.txt, qc_reports/PAIRED001.fastp.json"
  ]
}
```

Check `status`: `pass`, `fail`, `skipped` (missing input with `on_missing = "skip"`) or `not_applicable`.
Samples whose QC ran FastQC (before fastp, #100) have no fastp report: with the default thresholds their
fastp checks fail as missing. Gate them with a config without those checks, or `on_missing = "skip"`.
Counts are integers; percentages and ratios are rounded to 4 decimals, and the bounds apply to the
rounded value shown.

### Metrics

| Metric | From | Definition |
| :--- | :--- | :--- |
| `q30_pct` | fastp | Bases with quality ≥ 30 before filtering × 100 (the QC stage filters nothing, so this is the upload) |
| `gc_pct` | fastp | GC content of the reads × 100 |
| `fastp_duplicate_pct` | fastp | fastp's duplication estimate from the read sequences, before alignment × 100 |
| `read_count_difference` | flagstat + fastp | flagstat's primary records (QC-passed + QC-failed) − fastp's total reads; both count each mate. An exact integer, never rounded |
| `reads` | flagstat | Primary records, QC-passed (each mate counts). Supplementary and secondary alignments excluded |
| `mapped_pct` | flagstat | primary mapped / primary × 100 (samtools < 1.13 has no primary lines: mapped / in total). This is the run record's `primary_mapped_pct`, not its `mapped_pct` (flagstat's own, over all records including supplementary) |
| `duplicate_pct` | flagstat | primary duplicates / primary × 100 (marked by `samtools markdup`, not removed) |
| `properly_paired_pct` | flagstat | properly paired / paired in sequencing × 100. Not applicable to single-end reads |
| `mean_depth` | mosdepth | bases / length of the `total` row (mosdepth's `mean`, unrounded). Duplicates excluded |
| `breadth_1x_pct`, `breadth_10x_pct`, `breadth_20x_pct` | mosdepth distribution | The `total` row's fraction of the reference at ≥ 1/10/20x × 100 (duplicates excluded). mosdepth prints 2 decimals, so these are whole percentages. mosdepth leaves out depths no base is exactly at; such a depth takes the next listed depth's value. Deeper than the deepest base: 0 |
| `variants`, `snps`, `indels` | bcftools stats | `SN` counts of the first file (id 0) |
| `tstv` | bcftools stats | transitions / transversions from the `TSTV` row. Undefined (not 0.00) with no transversions. The run record rounds it to 2 decimals, the verdict to 4 |

## Development

```bash
cd gate
cargo fmt --check
cargo clippy --all-targets --locked -- -D warnings
cargo test --locked
```

The toolchain is pinned in `rust-toolchain.toml` (rustup installs it on first use), like ruff and
Terraform, because a new Rust release can add clippy lints and CI denies warnings. CI runs the same
three commands plus the static musl build (`tests.yml`, job "Quality gate").
No Rust on the machine? The same commands work in Docker:
`docker run --rm -v "$PWD:/repo" -w /repo/gate rust:1-slim-trixie sh -c "cargo test"`.

The tests read only real tool output: the synthetic samples' stats shared with the Python tests
(`../tests/fixtures/stats/`), everything one `run_local.py --check` run wrote for a single-end and a
paired-end sample (`tests/fixtures/{SYNTH001,PAIRED001}/`, run records included), and the HG003/HG005
chr20 stats and fastp reports.

## In the pipeline

The image builds the gate from source in its own stage (Rust 1.99.1, static musl binary, ~1 MB) and
copies only the binary and `thresholds/` into the final image (`/usr/local/bin/geyser-gate`,
`/opt/geyser-gate/thresholds/`, also in `$GATE_THRESHOLDS_DIR`).

`tasks.py gate <id>` (the `QualityGate` stage, `app/tasks.py`):

1. Fetches the gate's inputs into a temporary root laid out like the bucket: the fastp report and the
   four `stats/<id>/` files that exist, and the run record **assembled fresh from its stage parts**
   (`provenance.read_record`), so a stale stored copy can't mislead it.
2. Runs `geyser-gate` and stores the verdict as **`qc_reports/<id>.gate.json`**. When the gate gives no
   verdict (not installed, timed out, crashed, config error), that key gets a `"verdict": null` file
   with the reason instead, so an earlier run's verdict there can't pass for this run's (the bucket
   never deletes). The verdict also carries the run name it judged.
3. Records it in the run record: `gate {verdict, reasons, mode, config, gate_version, key}`, and a
   `QualityGate` stage entry. The gate doesn't change the record's `status`, which stays the core
   pipeline's.

| Setting | Values | Default |
| :--- | :--- | :--- |
| `GATE_MODE` | `warn`: a failed verdict, or a gate that can't give one (an image without the binary, a timeout, a crash, an unreadable verdict), only warns. `fail`: either fails the `QualityGate` stage, with the gate's reasons | `warn`, until the decision is made (#144) |
| `GATE_CONFIG` | a thresholds file inside the container, e.g. `$GATE_THRESHOLDS_DIR/synthetic.toml` (variables are expanded by the stage, since Batch and `docker run -e` pass values as they are) | the built-in human WGS thresholds |

A mistyped `GATE_MODE` or an unusable `GATE_CONFIG` (exit 2) fails the stage in either mode: they are
operator settings, not quality problems. `run_local.py` runs the gate as the last job, after variant
calling and any DeepVariant job, so a failed gate never keeps those or `--check` from running
(`--no-gate` skips it). For `make_test_data.py` samples (they have `truth/<id>.json`) it uses the
synthetic thresholds unless `GATE_CONFIG` is set; a host `GATE_CONFIG` in the repo's `gate/thresholds/`
or under the storage root is passed as its path in the container. The dashboard shows the stage as its
own group ("Quality gate").

A gate re-run by hand (`tasks.py gate <id>` without `RUN_NAME`) keeps the core stages in the record,
not the extra callers': a run still in progress (gate done, DeepVariant running) would otherwise borrow
an older run's DeepVariant entry.

### In the cloud (Step Functions)

After `Call_Variants` (bcftools, merged) or a successful `DeepVariant`, the state machine runs
`Prepare_Gate_Command` → **`Quality_Gate`** (a Batch job at QC's size: `tasks.py gate <id>`, 900 s
attempt limit) → **`Read_Gate_Verdict`** (reads `qc_reports/<id>.gate.json` straight from S3) →
**`Gate_Decision`**. The job always runs the app's warn mode, so a failed verdict never fails the job;
the state machine decides with Terraform's **`gate_mode`**:

| Verdict | `gate_mode = "warn"` | `gate_mode = "fail"` (default since #157) |
| :--- | :--- | :--- |
| pass | `Pipeline_Succeeded` | `Pipeline_Succeeded` |
| fail | `Gate_Warned` → `Pipeline_Succeeded` (recorded) | `Notify_Failure` → `Pipeline_Failed`; the notification's error is `QualityGateFailed`, its cause lists the gate's reasons |
| none (the job failed, the verdict is missing, unreadable or `null`) | `Gate_Skipped` → `Pipeline_Succeeded` (not judged) | `Notify_Failure` → `Pipeline_Failed`; the notification's error is `QualityGateError` |

The execution itself always fails as `PipelineStageFailed`, like any failed stage. The gate's error and
reasons are in the SNS failure notification (`ErrorDetails`) and in the execution's data (`$.error`,
`$.gate.verdict`); the job pins `GATE_MODE=warn`, so the state machine is the one place the mode is
decided. Before applying, the image in ECR (`dev-local`) must have the gate (main from #148 on):
with an older image every gate job fails, which fail mode turns into a failed run every time.

The gate costs one more Fargate job per run: about a minute of start-up overhead and a fraction of a
cent. Verdicts are per sample (`qc_reports/<id>.gate.json`), like every other key of a sample, so two
runs of the same sample at once can read each other's verdict, as they already overwrite each other's
stats; `scripts/reconcile.py` flags concurrent runs.

A stage that fails before the gate (QC, align, variants) ends the run through `Notify_Failure` as before;
the gate never runs then.

### Threshold profile per run (`gate_profile`)

Which thresholds a cloud run is judged by is per run, not global: the execution input's
**`gate_profile`** names a file in the image's `$GATE_THRESHOLDS_DIR` (`gate/thresholds/`):

| `gate_profile` | Thresholds | For |
| :--- | :--- | :--- |
| `default` (also when unset) | `default.toml` (human germline WGS, ~30x) | real samples |
| `synthetic` | `synthetic.toml` | `make_test_data.py` data (local runs, the Docker E2E) |
| `smoke` | `smoke.toml` | the cloud smoke tests (CLOUD0xx: 10,000 real chr20 reads, 10 planted SNPs) |

- **Uploads:** set it with S3 user metadata `gate-profile` (or `gate_profile`) on the upload, for
  example `aws s3 cp reads.fastq.gz s3://<data lake>/raw_reads/CLOUD016.fastq.gz --metadata gate-profile=smoke`,
  on either mate of a pair. The trigger passes it on exactly as uploaded (one HeadObject for a
  single-end upload). **Manual starts:** `"gate_profile": "smoke"` in the input. Values are
  case-sensitive.
- **One validator, visible failures:** the trigger never drops a run or picks for you. Mates that
  disagree, a metadata key that looks like a misspelling (`gateprofile`) and metadata it couldn't read
  all go on as values `Check_Gate_Profile` rejects, so the run fails with the reason, closed and
  notified. A single-end profile is read when the trigger runs: like the reads themselves, an upload
  overwritten in the meantime is judged as the newer one.
- **Locally:** `run_local.py --gate-profile default|synthetic|smoke`. Without it, a `make_test_data.py` sample
  still gets `synthetic`.
- **Checked first:** `Check_Gate_Profile`, before any job. An unknown profile (`strict`, `Synthetic`,
  a number, the trigger's flagged values) fails the run there, in either `gate_mode`, with
  `UnknownGateProfile`. It never falls back to the default silently. The failure goes through
  `Prepare_Run_Close` like any failed run, so the sample's record shows its latest run as failed.
- **The image:** `GATE_CONFIG` is `$GATE_THRESHOLDS_DIR/<profile>.toml`, which every image with the gate
  ships (main from #148 on); `default.toml` is also the binary's built-in default.
- **Why:** with the human thresholds, a synthetic smoke test fails the gate (CLOUD015: mean depth 0.02x,
  0% at 10x, Ti/Tv 0.25). In warn mode that's only a recorded verdict, but in fail mode every smoke test
  would fail. `synthetic` doesn't fit them either: it is calibrated for the local 50 kb random
  reference at ~30x. With `gate-profile=smoke` the smoke tests are judged by what they are.

## Warn first, then fail

The user's decision (2 Nov 2026): **warn first, then fail** (#157). **Fail since #157's apply** (decided 3 Nov 2026).

- **Criterion for the switch:** at least **5 real-sample cloud runs** (not the simulated CLOUD00x
  checkpoints) that the gate **judged** (`Gate_Decision` reached; a `Gate_Skipped` run doesn't count),
  and **no false fails** against the calibrated thresholds: every failed verdict was a really bad run,
  or the threshold was corrected first. DeepVariant runs needed #158 (done in #166): the run record
  counts a DeepVariant-only run as complete, so the gate's `run_status` check can pass them.
- **The switch, one line:** `gate_mode = "fail"` (the default in `infrastructure/variables.tf` since
  #157; a `gate_mode` in tfvars overrides it), then `terraform plan` (expect only an in-place update of the
  state machine) and, after review, apply. It ships with the `smoke` profile (above) in one apply: both
  change the same resource, the state machine. From then on, upload smoke tests with
  `--metadata gate-profile=smoke`.
- **Back:** `gate_mode = "warn"`, the same in-place update.

## Not yet

- **Adapter content:** the QC stage runs fastp with adapter trimming disabled (report-only), and then
  fastp doesn't detect adapters, so the report has no adapter numbers. Checking them needs a QC-stage
  change (e.g. let fastp detect adapters without writing trimmed reads).
- Stats for the DeepVariant and pangenome VCFs (they have no `bcftools stats` yet). **They must count
  PASS calls only** (`bcftools stats -f PASS`): DeepVariant's VCF also holds its RefCall candidates, and
  over all records the giraffe + DeepVariant VCF has Ti/Tv 0.86 (PASS: 1.96), which would fail every
  good run. A test pins this down.
