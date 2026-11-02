# geyser-gate: the quality gate

A run can "succeed" and still be useless: 60% of reads mapped, 5x depth, or a VCF full of errors all
end with a green Step Functions run. `geyser-gate` reads the QC statistics and the run record a run
already leaves behind and gives a verdict: **pass**, or **fail with a named reason**, e.g.
`mapped_pct 80 is below the minimum 95`.

It doesn't re-run any tool. It only reads the tools' own output files and applies thresholds from a
small TOML file. It is written in Rust because it is meant to ship as one static binary
(1 MB, `x86_64-unknown-linux-musl`) that can be copied into the pipeline image without adding a runtime.

> **Status:** a standalone tool, tested offline. It is **not yet part of the pipeline**: `app/tasks.py`,
> the state machine and the image are unchanged. Running it as a stage after variant calling is a
> follow-up (see [Not yet](#not-yet)).

## Inputs

The same keys the pipeline writes, under a folder laid out like the bucket (`--root`, e.g. `local_bucket/`):

| Input | Key | Written by | Gives |
| :--- | :--- | :--- | :--- |
| `flagstat` | `stats/<id>/flagstat.txt` | align (`samtools flagstat`) | `reads`, `mapped_pct`, `duplicate_pct`, `properly_paired_pct` |
| `mosdepth_summary` | `stats/<id>/mosdepth.summary.txt` | align (`mosdepth -n`) | `mean_depth` |
| `bcftools_stats` | `stats/<id>/bcftools.stats.txt` | variants (`bcftools stats`) | `variants`, `snps`, `indels`, `tstv` |
| `run_record` | `runs/<id>.provenance.json` | every stage ([`app/provenance.py`](../app/provenance.py), schema 1) | `run_status` (`succeeded` / `failed` / `incomplete`), run name |

The run record is `runs/<id>.provenance.json`, not `runs/<id>.json` (that file is the local web worker's state).

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
config, bad sample ID, `--root` not a folder). A missing or unparsable *input* is not an error: it is
reported in the verdict (see `on_missing`).

The verdict JSON goes to stdout (or `--output`); a one-line-per-check summary goes to stderr:

```text
  pass  run_status           succeeded
  pass  mapped_pct           100.0
  pass  duplicate_pct        0.04
  pass  properly_paired_pct  100.0
  pass  mean_depth           29.9413
  pass  snps                 10
  FAIL  tstv                 1.5  (tstv 1.5 is below the minimum 1.7)
geyser-gate: PAIRED001: FAIL (1 of 7 checks)
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

**`on_missing`**: what a check counts as when its input file is missing (the stage didn't run, or its
best-effort stats tool failed) or its value is undefined (Ti/Tv with no transversions). `fail` (default)
or `skip`. An input that exists but can't be parsed always fails: the gate can't vouch for it.

**Not applicable:** `properly_paired_pct` doesn't apply to single-end reads. It is reported as
`not_applicable` and never fails. The layout comes from flagstat (from the run record if flagstat is unusable).

### The built-in thresholds ([`thresholds/default.toml`](thresholds/default.toml))

For human germline short-read WGS at ~30x, called by the pipeline's bcftools stage. They were checked
against the stats of the two GIAB samples used in the benchmarks (`geyser-data`): HG003 and HG005, chr20,
2 x 150 bp, aligned with bwa-mem2 and called with bcftools as the pipeline does. Both pass every check
with a margin; their stats are test fixtures (`tests/fixtures/HG00{3,5}_chr20/`).

| Check | Bound | HG003 chr20 | HG005 chr20 | Why this bound |
| :--- | :--- | ---: | ---: | :--- |
| `run_status` | `succeeded` | | | A failed or unfinished run has nothing to vouch for |
| `mapped_pct` | ≥ 95 | 99.77 | 99.82 | Below 95: wrong reference, contamination or a broken alignment |
| `duplicate_pct` | ≤ 20 | 10.99 | 10.24 | Above 20 the library is low-complexity or over-amplified, and much of the depth is copies |
| `properly_paired_pct` | ≥ 90 | 99.05 | 96.97 | Paired-end only. Low values mean bad insert sizes, chimeras or the wrong reference |
| `mean_depth` | ≥ 20 | 36.78 | 32.64 | Below 20x a 30x sample loses sensitivity, mostly for heterozygous calls |
| `snps` | ≥ 1 | 118,410 | 109,870 | A complete run with an empty VCF is broken whatever the other numbers say |
| `tstv` | 1.7 – 2.4 | 1.81 | 1.85 | See below |

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
the same bounds the Docker end-to-end test asserts. No Ti/Tv check: 10 SNPs are too few for the ratio
to mean anything. The default thresholds fail synthetic data, by design (Ti/Tv 1.5).

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
  "inputs": {                        // each input: key, status ok | missing | invalid (+ error)
    "flagstat": { "key": "stats/PAIRED001/flagstat.txt", "status": "ok" }, ...
  },
  "metrics": {                       // every metric, checked or not (null if unavailable)
    "reads": 10000, "mapped_pct": 100.0, "duplicate_pct": 0.04, "properly_paired_pct": 100.0,
    "mean_depth": 29.9413, "variants": 10, "snps": 10, "indels": 0, "tstv": 1.5
  },
  "checks": [                        // the configured checks, run_status first
    { "name": "mean_depth", "status": "pass", "value": 29.9413, "min": 25.0, "max": 31.0,
      "source": "stats/PAIRED001/mosdepth.summary.txt", "reason": null }, ...
  ]
}
```

Check `status`: `pass`, `fail`, `skipped` (missing input with `on_missing = "skip"`) or `not_applicable`.
Counts are integers; percentages and ratios are rounded to 4 decimals, and the bounds apply to the
rounded value shown.

### Metrics

| Metric | From | Definition |
| :--- | :--- | :--- |
| `reads` | flagstat | Primary records, QC-passed (each mate counts). Supplementary and secondary alignments excluded |
| `mapped_pct` | flagstat | primary mapped / primary × 100 (samtools < 1.13 has no primary lines: mapped / in total) |
| `duplicate_pct` | flagstat | primary duplicates / primary × 100 (marked by `samtools markdup`, not removed) |
| `properly_paired_pct` | flagstat | properly paired / paired in sequencing × 100. Not applicable to single-end reads |
| `mean_depth` | mosdepth | bases / length of the `total` row (mosdepth's `mean`, unrounded). Duplicates excluded |
| `variants`, `snps`, `indels` | bcftools stats | `SN` counts of the first file (id 0) |
| `tstv` | bcftools stats | transitions / transversions from the `TSTV` row. Undefined (not 0.00) with no transversions |

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
(`../tests/fixtures/stats/`), run records from `run_local.py --check` (`tests/fixtures/*.provenance.json`)
and the HG003/HG005 chr20 stats.

## Not yet

- **Wiring it into the pipeline:** a fourth state after variant calling (or the end of the variants
  stage), the static binary copied into the image, and the verdict stored as `runs/<id>.gate.json`.
  A failed gate would end the run through `Notify_Failure` with its reasons. That touches `app/`, the
  Dockerfile and `infrastructure/`, so it is its own issue.
- **fastp's JSON** (`qc_reports/<id>.fastp.json`): adapter content, read quality and duplication before
  alignment.
- **Breadth of coverage** (`mosdepth.global.dist.txt`, e.g. % of the reference at ≥ 10x).
- Stats for the DeepVariant and pangenome VCFs (they have no `bcftools stats` yet), with their own Ti/Tv range.
