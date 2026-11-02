//! The four inputs the gate reads, as the pipeline writes them (app/tasks.py, app/provenance.py):
//!
//! ```text
//! stats/{id}/flagstat.txt          samtools flagstat, default text output   (align stage)
//! stats/{id}/mosdepth.summary.txt  mosdepth -n summary                      (align stage)
//! stats/{id}/bcftools.stats.txt    bcftools stats -F <ref> -s -             (variants stage)
//! runs/{id}.provenance.json        the provenance run record, schema 1      (every stage)
//! ```
//!
//! The parsers take the tools' own output and nothing else: the gate never re-runs a tool.

use std::collections::HashMap;
use std::fmt;
use std::fs;
use std::io::ErrorKind;
use std::path::{Path, PathBuf};

use serde::Deserialize;

/// The QC-passed counts from `samtools flagstat` that the gate uses.
///
/// The `primary*` lines exist since samtools 1.13; older output falls back to the all-records counts.
#[derive(Debug, Clone, PartialEq)]
pub struct Flagstat {
    pub in_total: u64,
    pub primary: Option<u64>,
    pub mapped: u64,
    pub primary_mapped: Option<u64>,
    pub duplicates: u64,
    pub primary_duplicates: Option<u64>,
    pub paired_in_sequencing: u64,
    pub properly_paired: u64,
}

impl Flagstat {
    /// Parse the default text output: `<passed> + <failed> <label>[ (<percentages>)]` per line.
    pub fn parse(text: &str) -> Result<Self, String> {
        let mut counts: HashMap<&str, u64> = HashMap::new();
        for line in text.lines() {
            let Some((passed, rest)) = line.split_once(" + ") else {
                continue;
            };
            let Some((failed, label)) = rest.split_once(' ') else {
                continue;
            };
            let (Ok(passed), Ok(_)) = (passed.trim().parse::<u64>(), failed.parse::<u64>()) else {
                continue;
            };
            let label = label.split(" (").next().unwrap_or(label).trim();
            // First wins: "with mate mapped to a different chr" appears twice (the second is mapQ>=5).
            counts.entry(label).or_insert(passed);
        }
        let get = |label: &str| counts.get(label).copied();
        let need = |label: &str| get(label).ok_or_else(|| format!("no '{label}' line: not samtools flagstat output"));
        Ok(Flagstat {
            in_total: need("in total")?,
            primary: get("primary"),
            mapped: need("mapped")?,
            primary_mapped: get("primary mapped"),
            duplicates: need("duplicates")?,
            primary_duplicates: get("primary duplicates"),
            paired_in_sequencing: get("paired in sequencing").unwrap_or(0),
            properly_paired: get("properly paired").unwrap_or(0),
        })
    }

    /// Reads (each mate counts): primary records, so secondary and supplementary alignments don't inflate it.
    pub fn reads(&self) -> u64 {
        self.primary.unwrap_or(self.in_total)
    }

    /// Mapped reads as a percentage of reads (primary records where flagstat reports them).
    pub fn mapped_pct(&self) -> Option<f64> {
        match (self.primary_mapped, self.primary) {
            (Some(mapped), Some(primary)) => pct(mapped, primary),
            _ => pct(self.mapped, self.in_total),
        }
    }

    /// Reads marked as duplicates (samtools markdup marks, never removes) as a percentage of reads.
    pub fn duplicate_pct(&self) -> Option<f64> {
        match (self.primary_duplicates, self.primary) {
            (Some(dups), Some(primary)) => pct(dups, primary),
            _ => pct(self.duplicates, self.in_total),
        }
    }

    pub fn is_paired(&self) -> bool {
        self.paired_in_sequencing > 0
    }

    /// Properly paired reads as a percentage of paired reads; None for single-end data.
    pub fn properly_paired_pct(&self) -> Option<f64> {
        pct(self.properly_paired, self.paired_in_sequencing)
    }
}

/// The `total` row of `<prefix>.mosdepth.summary.txt`.
#[derive(Debug, Clone, PartialEq)]
pub struct MosdepthSummary {
    pub length: u64,
    pub bases: u64,
}

impl MosdepthSummary {
    pub fn parse(text: &str) -> Result<Self, String> {
        let mut rows = text.lines().filter(|l| !l.trim().is_empty()).map(|l| l.split('\t').collect::<Vec<_>>());
        let header = rows.next().ok_or("empty file")?;
        let column = |name: &str| {
            header.iter().position(|h| *h == name).ok_or_else(|| format!("no '{name}' column: not a mosdepth summary"))
        };
        let (length_col, bases_col) = (column("length")?, column("bases")?);
        let total = rows.find(|r| r.first() == Some(&"total")).ok_or("no 'total' row")?;
        let field = |col: usize, name: &str| {
            total.get(col).and_then(|v| v.parse::<u64>().ok()).ok_or_else(|| format!("bad '{name}' in the total row"))
        };
        Ok(MosdepthSummary { length: field(length_col, "length")?, bases: field(bases_col, "bases")? })
    }

    /// Mean depth over the whole reference: bases / length (mosdepth's `mean` column, unrounded).
    pub fn mean_depth(&self) -> Option<f64> {
        (self.length > 0).then(|| self.bases as f64 / self.length as f64)
    }
}

/// Counts from the `SN` and `TSTV` sections of `bcftools stats` (the first file, id 0).
#[derive(Debug, Clone, PartialEq)]
pub struct BcftoolsStats {
    pub records: u64,
    pub snps: u64,
    pub indels: u64,
    pub transitions: Option<u64>,
    pub transversions: Option<u64>,
}

impl BcftoolsStats {
    pub fn parse(text: &str) -> Result<Self, String> {
        let mut sn: HashMap<&str, u64> = HashMap::new();
        let (mut ts, mut tv) = (None, None);
        for line in text.lines() {
            let cols: Vec<&str> = line.split('\t').collect();
            match cols.as_slice() {
                ["SN", "0", label, value, ..] => {
                    if let Ok(v) = value.trim().parse() {
                        sn.insert(label.trim_end_matches(':'), v);
                    }
                }
                ["TSTV", "0", t, v, ..] => {
                    ts = t.parse().ok();
                    tv = v.parse().ok();
                }
                _ => {}
            }
        }
        let need = |label: &str| {
            sn.get(label).copied().ok_or_else(|| format!("no 'SN {label}' line: not bcftools stats output"))
        };
        Ok(BcftoolsStats {
            records: need("number of records")?,
            snps: need("number of SNPs")?,
            indels: need("number of indels")?,
            transitions: ts,
            transversions: tv,
        })
    }

    /// Ti/Tv from the counts. bcftools prints 0.00 when there are no transversions; here it is undefined (None).
    pub fn tstv(&self) -> Option<f64> {
        match (self.transitions, self.transversions) {
            (Some(ts), Some(tv)) if tv > 0 => Some(ts as f64 / tv as f64),
            _ => None,
        }
    }
}

/// The fields of the provenance run record (app/provenance.py, schema 1) the gate reads. Others are ignored.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct RunRecord {
    pub schema: Option<u64>,
    pub sample_id: Option<String>,
    pub run_name: Option<String>,
    pub status: Option<String>,
    pub layout: Option<String>,
}

impl RunRecord {
    pub fn parse(text: &str) -> Result<Self, String> {
        let record: RunRecord = serde_json::from_str(text).map_err(|e| format!("not a run record: {e}"))?;
        match record.schema {
            Some(1) | None => Ok(record),
            Some(other) => Err(format!("run record schema {other}; this gate reads schema 1")),
        }
    }
}

fn pct(n: u64, d: u64) -> Option<f64> {
    (d > 0).then(|| n as f64 * 100.0 / d as f64)
}

/// One input as the gate found it.
#[derive(Debug, Clone, PartialEq)]
pub enum Loaded<T> {
    Ok(T),
    /// The file doesn't exist: the stage didn't run, or its (best-effort) stats tool failed.
    Missing,
    /// The file exists but can't be read or parsed.
    Invalid(String),
}

impl<T> Loaded<T> {
    pub fn ok(&self) -> Option<&T> {
        match self {
            Loaded::Ok(v) => Some(v),
            _ => None,
        }
    }
}

/// The inputs by name, in verdict order.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Input {
    Flagstat,
    Mosdepth,
    Bcftools,
    RunRecord,
}

impl Input {
    pub const ALL: [Input; 4] = [Input::Flagstat, Input::Mosdepth, Input::Bcftools, Input::RunRecord];

    pub fn name(self) -> &'static str {
        match self {
            Input::Flagstat => "flagstat",
            Input::Mosdepth => "mosdepth_summary",
            Input::Bcftools => "bcftools_stats",
            Input::RunRecord => "run_record",
        }
    }

    /// The storage key, as in tasks.keys_for / provenance.record_key.
    pub fn key(self, sample_id: &str) -> String {
        match self {
            Input::Flagstat => format!("stats/{sample_id}/flagstat.txt"),
            Input::Mosdepth => format!("stats/{sample_id}/mosdepth.summary.txt"),
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
    pub flagstat: Loaded<Flagstat>,
    pub mosdepth: Loaded<MosdepthSummary>,
    pub bcftools: Loaded<BcftoolsStats>,
    pub record: Loaded<RunRecord>,
}

impl Inputs {
    /// Read the sample's inputs from `root`, a folder laid out like the bucket (e.g. local_bucket/).
    pub fn load(root: &Path, sample_id: &str) -> Inputs {
        let path = |input: Input| -> PathBuf { root.join(input.key(sample_id)) };
        let mut record = load(&path(Input::RunRecord), RunRecord::parse);
        if let Loaded::Ok(RunRecord { sample_id: Some(other), .. }) = &record {
            if other != sample_id {
                record = Loaded::Invalid(format!("the record is for sample '{other}'"));
            }
        }
        Inputs {
            sample_id: sample_id.to_string(),
            flagstat: load(&path(Input::Flagstat), Flagstat::parse),
            mosdepth: load(&path(Input::Mosdepth), MosdepthSummary::parse),
            bcftools: load(&path(Input::Bcftools), BcftoolsStats::parse),
            record,
        }
    }

    /// (missing, error) of one input.
    pub fn state(&self, input: Input) -> (bool, Option<&str>) {
        fn of<T>(l: &Loaded<T>) -> (bool, Option<&str>) {
            match l {
                Loaded::Ok(_) => (false, None),
                Loaded::Missing => (true, None),
                Loaded::Invalid(e) => (false, Some(e)),
            }
        }
        match input {
            Input::Flagstat => of(&self.flagstat),
            Input::Mosdepth => of(&self.mosdepth),
            Input::Bcftools => of(&self.bcftools),
            Input::RunRecord => of(&self.record),
        }
    }

    /// The layout: the alignment's (flagstat sees the reads), else the run record's.
    pub fn layout(&self) -> Option<String> {
        match &self.flagstat {
            Loaded::Ok(f) => Some(if f.is_paired() { "paired" } else { "single" }.to_string()),
            _ => self.record.ok().and_then(|r| r.layout.clone()),
        }
    }
}

fn load<T>(path: &Path, parse: fn(&str) -> Result<T, String>) -> Loaded<T> {
    match fs::read_to_string(path) {
        Ok(text) => match parse(&text) {
            Ok(v) => Loaded::Ok(v),
            Err(e) => Loaded::Invalid(e),
        },
        Err(e) if e.kind() == ErrorKind::NotFound => Loaded::Missing,
        Err(e) => Loaded::Invalid(e.to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // Real tool output on the synthetic samples (samtools 1.21, mosdepth 0.3.15, bcftools 1.21), shared
    // with the Python parsers' tests.
    const SINGLE_FLAGSTAT: &str = include_str!("../../tests/fixtures/stats/single.flagstat.txt");
    const PAIRED_FLAGSTAT: &str = include_str!("../../tests/fixtures/stats/paired.flagstat.txt");
    const SINGLE_MOSDEPTH: &str = include_str!("../../tests/fixtures/stats/single.mosdepth.summary.txt");
    const PAIRED_MOSDEPTH: &str = include_str!("../../tests/fixtures/stats/paired.mosdepth.summary.txt");
    const PAIRED_BCFTOOLS: &str = include_str!("../../tests/fixtures/stats/paired.bcftools.stats.txt");
    const SINGLE_BCFTOOLS: &str = include_str!("../../tests/fixtures/stats/single.bcftools.stats.txt");

    #[test]
    fn flagstat_single_end() {
        let f = Flagstat::parse(SINGLE_FLAGSTAT).unwrap();
        assert_eq!(f.reads(), 10_000);
        assert_eq!(f.mapped_pct(), Some(100.0));
        assert!((f.duplicate_pct().unwrap() - 4.85).abs() < 1e-9);
        assert!(!f.is_paired());
        assert_eq!(f.properly_paired_pct(), None, "N/A for single-end reads");
    }

    #[test]
    fn flagstat_paired_end() {
        let f = Flagstat::parse(PAIRED_FLAGSTAT).unwrap();
        assert_eq!((f.reads(), f.paired_in_sequencing, f.properly_paired), (10_000, 10_000, 10_000));
        assert!((f.duplicate_pct().unwrap() - 0.04).abs() < 1e-9);
        assert_eq!(f.properly_paired_pct(), Some(100.0));
    }

    #[test]
    fn flagstat_counts_primary_records_so_supplementary_alignments_dont_inflate_reads() {
        let text = "1050 + 0 in total (QC-passed reads + QC-failed reads)\n\
                    1000 + 0 primary\n\
                    50 + 0 supplementary\n\
                    30 + 0 duplicates\n\
                    20 + 0 primary duplicates\n\
                    1000 + 0 mapped (95.24% : N/A)\n\
                    950 + 0 primary mapped (95.00% : N/A)\n";
        let f = Flagstat::parse(text).unwrap();
        assert_eq!(f.reads(), 1000);
        assert_eq!(f.mapped_pct(), Some(95.0));
        assert_eq!(f.duplicate_pct(), Some(2.0));
    }

    #[test]
    fn flagstat_before_samtools_1_13_has_no_primary_lines() {
        let text = "200 + 3 in total (QC-passed reads + QC-failed reads)\n\
                    10 + 0 duplicates\n\
                    150 + 1 mapped (75.00% : N/A)\n";
        let f = Flagstat::parse(text).unwrap();
        assert_eq!(f.reads(), 200, "QC-passed only");
        assert_eq!(f.mapped_pct(), Some(75.0));
        assert_eq!(f.duplicate_pct(), Some(5.0));
    }

    #[test]
    fn flagstat_rejects_other_files() {
        assert!(Flagstat::parse(SINGLE_MOSDEPTH).unwrap_err().contains("not samtools flagstat"));
        assert!(Flagstat::parse("").is_err());
    }

    #[test]
    fn flagstat_with_no_reads_has_no_percentages() {
        let f = Flagstat::parse("0 + 0 in total (QC-passed reads + QC-failed reads)\n0 + 0 primary\n0 + 0 duplicates\n0 + 0 mapped (N/A : N/A)\n0 + 0 primary mapped (N/A : N/A)\n").unwrap();
        assert_eq!((f.reads(), f.mapped_pct(), f.duplicate_pct()), (0, None, None));
    }

    #[test]
    fn mosdepth_summary() {
        let single = MosdepthSummary::parse(SINGLE_MOSDEPTH).unwrap();
        assert_eq!((single.length, single.bases), (51_000, 1_427_250));
        assert!((single.mean_depth().unwrap() - 27.985).abs() < 1e-3, "mosdepth prints 27.99");
        let paired = MosdepthSummary::parse(PAIRED_MOSDEPTH).unwrap();
        assert!((paired.mean_depth().unwrap() - 29.40).abs() < 1e-2);
    }

    #[test]
    fn mosdepth_reads_columns_by_name_and_needs_the_total_row() {
        let reordered = "chrom\tbases\tlength\tmean\nchr1\t300\t100\t3.00\ntotal\t300\t100\t3.00\n";
        assert_eq!(MosdepthSummary::parse(reordered).unwrap().mean_depth(), Some(3.0));
        let no_total = "chrom\tlength\tbases\tmean\tmin\tmax\nchr1\t100\t300\t3.00\t0\t5\n";
        assert!(MosdepthSummary::parse(no_total).unwrap_err().contains("total"));
        assert!(MosdepthSummary::parse(SINGLE_FLAGSTAT).is_err());
        let empty_reference = "chrom\tlength\tbases\tmean\ntotal\t0\t0\t0\n";
        assert_eq!(MosdepthSummary::parse(empty_reference).unwrap().mean_depth(), None);
    }

    #[test]
    fn bcftools_stats() {
        for text in [SINGLE_BCFTOOLS, PAIRED_BCFTOOLS] {
            let b = BcftoolsStats::parse(text).unwrap();
            assert_eq!((b.records, b.snps, b.indels), (10, 10, 0));
            assert_eq!((b.transitions, b.transversions), (Some(6), Some(4)));
            assert_eq!(b.tstv(), Some(1.5));
        }
    }

    #[test]
    fn bcftools_tstv_is_undefined_without_transversions() {
        let text = "SN\t0\tnumber of records:\t2\nSN\t0\tnumber of SNPs:\t2\nSN\t0\tnumber of indels:\t0\nTSTV\t0\t2\t0\t0.00\t2\t0\t0.00\n";
        assert_eq!(BcftoolsStats::parse(text).unwrap().tstv(), None);
        let no_tstv_row = "SN\t0\tnumber of records:\t0\nSN\t0\tnumber of SNPs:\t0\nSN\t0\tnumber of indels:\t0\n";
        assert_eq!(BcftoolsStats::parse(no_tstv_row).unwrap().tstv(), None);
    }

    #[test]
    fn bcftools_reads_only_the_first_file() {
        let text = "SN\t0\tnumber of records:\t5\nSN\t0\tnumber of SNPs:\t4\nSN\t0\tnumber of indels:\t1\n\
                    SN\t1\tnumber of records:\t99\nTSTV\t0\t3\t1\t3.00\t3\t1\t3.00\nTSTV\t1\t9\t9\t1.00\t9\t9\t1.00\n";
        let b = BcftoolsStats::parse(text).unwrap();
        assert_eq!((b.records, b.tstv()), (5, Some(3.0)));
        assert!(BcftoolsStats::parse(SINGLE_FLAGSTAT).unwrap_err().contains("not bcftools stats"));
    }

    #[test]
    fn run_record() {
        let r = RunRecord::parse(r#"{"schema": 1, "sample_id": "S1", "status": "succeeded", "layout": "paired", "run_name": null, "stages": {}}"#).unwrap();
        assert_eq!(r.status.as_deref(), Some("succeeded"));
        assert_eq!(r.run_name, None);
        assert!(RunRecord::parse(r#"{"schema": 2}"#).unwrap_err().contains("schema 2"));
        assert!(RunRecord::parse("[]").is_err());
        assert!(RunRecord::parse("not json").is_err());
    }

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
        assert_eq!(inputs.bcftools, Loaded::Missing);
        assert_eq!(inputs.record, Loaded::Invalid("the record is for sample 'S2'".into()));
        assert_eq!(inputs.layout().as_deref(), Some("single"));
    }
}
