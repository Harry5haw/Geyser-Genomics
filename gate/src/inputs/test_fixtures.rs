//! The tool outputs the tests read, shared by every parser's tests.

// Real tool output on the synthetic samples (samtools 1.21, mosdepth 0.3.15, bcftools 1.21), shared
// with the Python parsers' tests.
pub(super) const SINGLE_FLAGSTAT: &str = include_str!("../../../tests/fixtures/stats/single.flagstat.txt");
pub(super) const PAIRED_FLAGSTAT: &str = include_str!("../../../tests/fixtures/stats/paired.flagstat.txt");
pub(super) const SINGLE_MOSDEPTH: &str = include_str!("../../../tests/fixtures/stats/single.mosdepth.summary.txt");
pub(super) const PAIRED_MOSDEPTH: &str = include_str!("../../../tests/fixtures/stats/paired.mosdepth.summary.txt");
pub(super) const PAIRED_BCFTOOLS: &str = include_str!("../../../tests/fixtures/stats/paired.bcftools.stats.txt");
pub(super) const SINGLE_BCFTOOLS: &str = include_str!("../../../tests/fixtures/stats/single.bcftools.stats.txt");
pub(super) const SINGLE_DIST: &str = include_str!("../../../tests/fixtures/stats/single.mosdepth.global.dist.txt");
// Everything one run_local.py --check run wrote for SYNTH001 (default synthetic data: 50 kb, no N
// gap). The shared fixtures above come from another run (--leading-gap 1000: 51 kb), so the load
// tests use only these, never a mix of the two.
pub(super) const RUN_FLAGSTAT: &str = include_str!("../../tests/fixtures/SYNTH001/flagstat.txt");
pub(super) const RUN_MOSDEPTH: &str = include_str!("../../tests/fixtures/SYNTH001/mosdepth.summary.txt");
pub(super) const RUN_DIST: &str = include_str!("../../tests/fixtures/SYNTH001/mosdepth.global.dist.txt");
pub(super) const RUN_BCFTOOLS: &str = include_str!("../../tests/fixtures/SYNTH001/bcftools.stats.txt");
pub(super) const SINGLE_FASTP: &str = include_str!("../../tests/fixtures/SYNTH001/fastp.json");
// fastp with the QC stage's flags on the HG003 chr20 benchmark reads.
pub(super) const PAIRED_FASTP: &str = include_str!("../../tests/fixtures/PAIRED001/fastp.json");
pub(super) const HG003_FASTP: &str = include_str!("../../tests/fixtures/HG003_chr20/fastp.json");
pub(super) const HG003_DIST: &str = include_str!("../../tests/fixtures/HG003_chr20/mosdepth.global.dist.txt");
