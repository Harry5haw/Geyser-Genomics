//! The inputs the gate reads, as the pipeline writes them (app/tasks.py, app/provenance.py):
//!
//! ```text
//! qc_reports/{id}.fastp.json           fastp in report-only mode                 (qc stage)
//! stats/{id}/flagstat.txt              samtools flagstat, default text output    (align stage)
//! stats/{id}/mosdepth.summary.txt      mosdepth -n summary                       (align stage)
//! stats/{id}/mosdepth.global.dist.txt  mosdepth -n cumulative depth distribution (align stage)
//! stats/{id}/bcftools.stats.txt        bcftools stats -F <ref> -s -              (variants stage)
//! runs/{id}.provenance.json            the provenance run record, schema 1       (every stage)
//! ```
//!
//! The parsers take the tools' own output and nothing else: the gate never re-runs a tool. One file
//! per tool; this module loads them for a sample and applies the rules that span inputs (left-over
//! files, a stale record, layouts that disagree).

mod bcftools;
mod fastp;
mod flagstat;
mod mosdepth;
mod run_record;

use std::fmt;
use std::fs;
use std::io::ErrorKind;
use std::path::{Path, PathBuf};

pub use bcftools::BcftoolsStats;
pub use fastp::Fastp;
pub use flagstat::Flagstat;
pub use mosdepth::{MosdepthDist, MosdepthSummary};
use run_record::Part;
pub use run_record::{Output, RUN_RECORD_SCHEMA, RunRecord};

fn pct(n: u64, d: u64) -> Option<f64> {
    (d > 0).then(|| n as f64 * 100.0 / d as f64)
}

/// One input as the gate found it.
#[derive(Debug, Clone, PartialEq)]
pub enum Loaded<T> {
    Ok(T),
    /// No usable file, with why: it doesn't exist (the stage didn't run, or its best-effort stats tool
    /// failed), or it is left over from an earlier run.
    Missing(String),
    /// The file exists but can't be read or parsed, or contradicts the run record.
    Invalid(String),
}

/// What is wrong with an input, if anything.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Problem<'a> {
    Missing(&'a str),
    Invalid(&'a str),
}

impl<T> Loaded<T> {
    pub fn ok(&self) -> Option<&T> {
        match self {
            Loaded::Ok(v) => Some(v),
            _ => None,
        }
    }

    pub fn problem(&self) -> Option<Problem<'_>> {
        match self {
            Loaded::Ok(_) => None,
            Loaded::Missing(why) => Some(Problem::Missing(why)),
            Loaded::Invalid(why) => Some(Problem::Invalid(why)),
        }
    }
}

/// The inputs by name, in verdict order.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Input {
    Fastp,
    Flagstat,
    Mosdepth,
    MosdepthDist,
    Bcftools,
    RunRecord,
}

impl Input {
    pub const ALL: [Input; 6] =
        [Input::Fastp, Input::Flagstat, Input::Mosdepth, Input::MosdepthDist, Input::Bcftools, Input::RunRecord];

    pub fn name(self) -> &'static str {
        match self {
            Input::Fastp => "fastp",
            Input::Flagstat => "flagstat",
            Input::Mosdepth => "mosdepth_summary",
            Input::MosdepthDist => "mosdepth_dist",
            Input::Bcftools => "bcftools_stats",
            Input::RunRecord => "run_record",
        }
    }

    /// The storage key, as in tasks.keys_for / provenance.record_key.
    pub fn key(self, sample_id: &str) -> String {
        match self {
            Input::Fastp => format!("qc_reports/{sample_id}.fastp.json"),
            Input::Flagstat => format!("stats/{sample_id}/flagstat.txt"),
            Input::Mosdepth => format!("stats/{sample_id}/mosdepth.summary.txt"),
            Input::MosdepthDist => format!("stats/{sample_id}/mosdepth.global.dist.txt"),
            Input::Bcftools => format!("stats/{sample_id}/bcftools.stats.txt"),
            Input::RunRecord => format!("runs/{sample_id}.provenance.json"),
        }
    }
}

impl fmt::Display for Input {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.name())
    }
}

/// Everything the gate read for one sample.
#[derive(Debug, Clone, PartialEq)]
pub struct Inputs {
    pub sample_id: String,
    pub fastp: Loaded<Fastp>,
    pub flagstat: Loaded<Flagstat>,
    pub mosdepth: Loaded<MosdepthSummary>,
    pub mosdepth_dist: Loaded<MosdepthDist>,
    pub bcftools: Loaded<BcftoolsStats>,
    pub record: Loaded<RunRecord>,
}

impl Inputs {
    /// Read the sample's inputs from `root`, a folder laid out like the bucket (e.g. local_bucket/).
    ///
    /// Nothing in the bucket is ever deleted (the pipeline has no s3:DeleteObject) and the stats tools
    /// are best effort, so files can be left over from an earlier run. With a run record, the gate only
    /// uses what that run produced:
    /// - the record must be newer than every stage part (runs/{id}.provenance/*.json). The stored record
    ///   is a convenience copy; a stage whose re-assembly failed leaves it behind its parts.
    /// - a stats file must be among the record's outputs, otherwise it is from an earlier run (missing).
    /// - fastp's and flagstat's layouts must agree with the record's.
    pub fn load(root: &Path, sample_id: &str) -> Inputs {
        let mut record = load(root, sample_id, Input::RunRecord, RunRecord::parse);
        if let Loaded::Ok(r) = &record
            && let Some(problem) = record_problem(root, sample_id, r)
        {
            record = Loaded::Invalid(problem);
        }
        let mut inputs = Inputs {
            sample_id: sample_id.to_string(),
            fastp: load(root, sample_id, Input::Fastp, Fastp::parse),
            flagstat: load(root, sample_id, Input::Flagstat, Flagstat::parse),
            mosdepth: load(root, sample_id, Input::Mosdepth, MosdepthSummary::parse),
            mosdepth_dist: load(root, sample_id, Input::MosdepthDist, MosdepthDist::parse),
            bcftools: load(root, sample_id, Input::Bcftools, BcftoolsStats::parse),
            record,
        };
        // fastp and flagstat see the same reads: their layouts must agree, with or without a record.
        if let (Loaded::Ok(fastp), Loaded::Ok(f)) = (&inputs.fastp, &inputs.flagstat)
            && fastp.paired != f.is_paired()
        {
            let (seen, aligned) = (layout_name(fastp.paired), layout_name(f.is_paired()));
            inputs.fastp = Loaded::Invalid(format!("shows {seen}-end reads, the alignment (flagstat) {aligned}-end"));
        }
        if let Loaded::Ok(r) = &inputs.record {
            let run = r.run_name.as_deref().unwrap_or("in the run record");
            let stale = |input: Input| -> Option<String> {
                let key = input.key(sample_id);
                (r.has_output(&key) == Some(false))
                    .then(|| format!("{key} is not an output of run {run}: left over from an earlier run"))
            };
            fn drop_stale<T>(l: &mut Loaded<T>, why: Option<String>) {
                if let (Loaded::Ok(_), Some(why)) = (&*l, why) {
                    *l = Loaded::Missing(why);
                }
            }
            drop_stale(&mut inputs.fastp, stale(Input::Fastp));
            drop_stale(&mut inputs.flagstat, stale(Input::Flagstat));
            drop_stale(&mut inputs.mosdepth, stale(Input::Mosdepth));
            drop_stale(&mut inputs.mosdepth_dist, stale(Input::MosdepthDist));
            drop_stale(&mut inputs.bcftools, stale(Input::Bcftools));

            if let Some(recorded) = r.layout.as_deref() {
                let disagrees = |paired: bool| {
                    let seen = layout_name(paired);
                    (seen != recorded).then(|| format!("shows {seen}-end reads, the run record says {recorded}"))
                };
                if let Some(why) = inputs.flagstat.ok().and_then(|f| disagrees(f.is_paired())) {
                    inputs.flagstat = Loaded::Invalid(why);
                }
                if let Some(why) = inputs.fastp.ok().and_then(|f| disagrees(f.paired)) {
                    inputs.fastp = Loaded::Invalid(why);
                }
            }
        }
        inputs
    }

    pub fn problem(&self, input: Input) -> Option<Problem<'_>> {
        match input {
            Input::Fastp => self.fastp.problem(),
            Input::Flagstat => self.flagstat.problem(),
            Input::Mosdepth => self.mosdepth.problem(),
            Input::MosdepthDist => self.mosdepth_dist.problem(),
            Input::Bcftools => self.bcftools.problem(),
            Input::RunRecord => self.record.problem(),
        }
    }

    /// The layout: the alignment's (flagstat sees the reads), else fastp's, else the run record's.
    pub fn layout(&self) -> Option<String> {
        let seen = self.flagstat.ok().map(|f| f.is_paired()).or(self.fastp.ok().map(|f| f.paired));
        seen.map(|paired| layout_name(paired).to_string()).or_else(|| self.record.ok().and_then(|r| r.layout.clone()))
    }
}

fn layout_name(paired: bool) -> &'static str {
    if paired { "paired" } else { "single" }
}

/// Why a parsed record can't be trusted: another sample's, or older than one of its stage parts.
fn record_problem(root: &Path, sample_id: &str, r: &RunRecord) -> Option<String> {
    if let Some(other) = r.sample_id.as_deref().filter(|other| *other != sample_id) {
        return Some(format!("the record is for sample '{other}'"));
    }
    let updated = r.updated.as_deref()?;
    // No parts folder (or an unreadable part): nothing to compare; the parts are provenance.py's business.
    let entries = fs::read_dir(root.join(format!("runs/{sample_id}.provenance/"))).ok()?;
    let mut newer: Vec<(String, String)> = entries
        .flatten()
        .filter(|e| e.path().extension().is_some_and(|x| x == "json"))
        .filter_map(|e| serde_json::from_str::<Part>(&fs::read_to_string(e.path()).ok()?).ok())
        .filter_map(|p| Some((p.finished.filter(|f| f.as_str() > updated)?, p.task.unwrap_or_default())))
        .collect();
    newer.sort();
    let (finished, task) = newer.pop()?;
    Some(format!(
        "stale: stage {task} finished at {finished}, after the record was assembled at {updated} (its re-assembly failed?)"
    ))
}

fn load<T>(root: &Path, sample_id: &str, input: Input, parse: fn(&str) -> Result<T, String>) -> Loaded<T> {
    let key = input.key(sample_id);
    let path: PathBuf = root.join(&key);
    match fs::read_to_string(&path) {
        Ok(text) => match parse(&text) {
            Ok(v) => Loaded::Ok(v),
            Err(e) => Loaded::Invalid(e),
        },
        Err(e) if e.kind() == ErrorKind::NotFound => Loaded::Missing(format!("{key} not found")),
        Err(e) => Loaded::Invalid(e.to_string()),
    }
}

/// The tool outputs the tests read, shared by every parser's tests.
#[cfg(test)]
mod test_fixtures {
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
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::inputs::test_fixtures::*;

    #[test]
    fn load_tells_missing_from_invalid() {
        let dir = tempfile::tempdir().unwrap();
        let stats = dir.path().join("stats/S1");
        fs::create_dir_all(&stats).unwrap();
        fs::write(stats.join("flagstat.txt"), SINGLE_FLAGSTAT).unwrap();
        fs::write(stats.join("mosdepth.summary.txt"), "garbage").unwrap();
        fs::create_dir_all(dir.path().join("runs")).unwrap();
        fs::write(dir.path().join("runs/S1.provenance.json"), r#"{"schema": 1, "sample_id": "S2"}"#).unwrap();

        let inputs = Inputs::load(dir.path(), "S1");
        assert!(matches!(inputs.flagstat, Loaded::Ok(_)));
        assert!(matches!(inputs.mosdepth, Loaded::Invalid(_)));
        assert_eq!(inputs.bcftools, Loaded::Missing("stats/S1/bcftools.stats.txt not found".into()));
        assert_eq!(inputs.record, Loaded::Invalid("the record is for sample 'S2'".into()));
        assert_eq!(inputs.layout().as_deref(), Some("single"));
    }

    /// A root with the single-end stats and the given record JSON (plus stage parts: (task, finished)).
    fn root_with(record: serde_json::Value, parts: &[(&str, &str)]) -> tempfile::TempDir {
        let dir = tempfile::tempdir().unwrap();
        let stats = dir.path().join("stats/S1");
        fs::create_dir_all(&stats).unwrap();
        fs::write(stats.join("flagstat.txt"), RUN_FLAGSTAT).unwrap();
        fs::write(stats.join("mosdepth.summary.txt"), RUN_MOSDEPTH).unwrap();
        fs::write(stats.join("bcftools.stats.txt"), RUN_BCFTOOLS).unwrap();
        fs::write(stats.join("mosdepth.global.dist.txt"), RUN_DIST).unwrap();
        fs::create_dir_all(dir.path().join("qc_reports")).unwrap();
        fs::write(dir.path().join("qc_reports/S1.fastp.json"), SINGLE_FASTP).unwrap();
        let part_dir = dir.path().join("runs/S1.provenance");
        fs::create_dir_all(&part_dir).unwrap();
        fs::write(dir.path().join("runs/S1.provenance.json"), record.to_string()).unwrap();
        for (task, finished) in parts {
            let part = serde_json::json!({"task": task, "finished": finished, "status": "Success"});
            fs::write(part_dir.join(format!("{task}.json")), part.to_string()).unwrap();
        }
        dir
    }

    #[test]
    fn stats_that_are_not_outputs_of_the_recorded_run_are_left_overs() {
        // Run 2 re-aligned but its flagstat and mosdepth failed (best effort): only bcftools stats is its own.
        let record = serde_json::json!({
            "schema": 1, "sample_id": "S1", "run_name": "S1-run2", "status": "succeeded",
            "outputs": [{"key": "stats/S1/bcftools.stats.txt", "bytes": 1}],
        });
        let dir = root_with(record, &[]);
        let inputs = Inputs::load(dir.path(), "S1");
        assert_eq!(
            inputs.flagstat,
            Loaded::Missing(
                "stats/S1/flagstat.txt is not an output of run S1-run2: left over from an earlier run".into()
            )
        );
        assert!(matches!(inputs.mosdepth, Loaded::Missing(_)));
        assert!(matches!(inputs.mosdepth_dist, Loaded::Missing(_)));
        assert!(matches!(inputs.fastp, Loaded::Missing(_)));
        assert!(matches!(inputs.bcftools, Loaded::Ok(_)));
        assert_eq!(inputs.layout(), None, "neither flagstat nor the record give one");
    }

    #[test]
    fn a_record_without_outputs_trusts_the_stats_files() {
        let dir = root_with(serde_json::json!({"schema": 1, "sample_id": "S1", "status": "succeeded"}), &[]);
        let inputs = Inputs::load(dir.path(), "S1");
        assert!(inputs.flagstat.ok().is_some() && inputs.bcftools.ok().is_some());
    }

    #[test]
    fn a_record_older_than_a_stage_part_is_stale() {
        // Variants failed after a successful earlier run, and its re-assembly of the stored copy failed.
        let record = serde_json::json!({"schema": 1, "sample_id": "S1", "status": "succeeded", "updated": "2026-11-02T10:00:00Z"});
        let dir =
            root_with(record.clone(), &[("Align", "2026-11-02T10:00:00Z"), ("CallVariants", "2026-11-02T11:30:00Z")]);
        let Loaded::Invalid(why) = Inputs::load(dir.path(), "S1").record else { panic!("not invalid") };
        assert!(why.starts_with("stale: stage CallVariants finished at 2026-11-02T11:30:00Z"), "{why}");

        let dir = root_with(record, &[("Align", "2026-11-02T09:00:00Z"), ("CallVariants", "2026-11-02T10:00:00Z")]);
        assert!(Inputs::load(dir.path(), "S1").record.ok().is_some(), "parts up to the same second are fine");
    }

    #[test]
    fn flagstat_must_agree_with_the_recorded_layout() {
        let record = serde_json::json!({"schema": 1, "sample_id": "S1", "status": "succeeded", "layout": "paired"});
        let dir = root_with(record, &[]);
        let inputs = Inputs::load(dir.path(), "S1");
        assert_eq!(inputs.flagstat, Loaded::Invalid("shows single-end reads, the run record says paired".into()));
        assert_eq!(inputs.fastp, Loaded::Invalid("shows single-end reads, the run record says paired".into()));
        assert_eq!(inputs.layout().as_deref(), Some("paired"));
    }

    #[test]
    fn fastp_and_flagstat_must_agree_on_the_layout_without_a_record() {
        let dir = tempfile::tempdir().unwrap();
        let stats = dir.path().join("stats/S1");
        fs::create_dir_all(&stats).unwrap();
        fs::write(stats.join("flagstat.txt"), PAIRED_FLAGSTAT).unwrap();
        fs::create_dir_all(dir.path().join("qc_reports")).unwrap();
        fs::write(dir.path().join("qc_reports/S1.fastp.json"), SINGLE_FASTP).unwrap();
        let inputs = Inputs::load(dir.path(), "S1");
        assert_eq!(inputs.fastp, Loaded::Invalid("shows single-end reads, the alignment (flagstat) paired-end".into()));
        assert!(inputs.flagstat.ok().is_some());
    }
}
