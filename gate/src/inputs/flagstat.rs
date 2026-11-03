//! samtools flagstat, default text output (stats/{id}/flagstat.txt).

use std::collections::HashMap;

fn pct(n: u64, d: u64) -> Option<f64> {
    (d > 0).then(|| n as f64 * 100.0 / d as f64)
}

/// The QC-passed counts from `samtools flagstat` that the gate uses.
///
/// The `primary*` lines exist since samtools 1.13; older output falls back to the all-records counts.
#[derive(Debug, Clone, PartialEq)]
pub struct Flagstat {
    pub in_total: u64,
    pub primary: Option<u64>,
    /// The QC-failed column of the `primary` line (reads with flag 0x200; fastp still counts them).
    pub primary_failed: Option<u64>,
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
        let mut counts: HashMap<&str, (u64, u64)> = HashMap::new();
        for line in text.lines() {
            let Some((passed, rest)) = line.split_once(" + ") else {
                continue;
            };
            let Some((failed, label)) = rest.split_once(' ') else {
                continue;
            };
            let (Ok(passed), Ok(failed)) = (passed.trim().parse::<u64>(), failed.parse::<u64>()) else {
                continue;
            };
            let label = label.split(" (").next().unwrap_or(label).trim();
            // First wins: "with mate mapped to a different chr" appears twice (the second is mapQ>=5).
            counts.entry(label).or_insert((passed, failed));
        }
        let get = |label: &str| counts.get(label).map(|(passed, _)| *passed);
        let need = |label: &str| get(label).ok_or_else(|| format!("no '{label}' line: not samtools flagstat output"));
        Ok(Flagstat {
            in_total: need("in total")?,
            primary: get("primary"),
            primary_failed: counts.get("primary").map(|(_, failed)| *failed),
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

    /// Every primary record, QC-passed and QC-failed: one per read in the input (unmapped reads
    /// included). None without the `primary` line (samtools < 1.13), where only all-records counts exist.
    pub fn primary_reads_all(&self) -> Option<u64> {
        Some(self.primary? + self.primary_failed.unwrap_or(0))
    }

    pub fn is_paired(&self) -> bool {
        self.paired_in_sequencing > 0
    }

    /// Properly paired reads as a percentage of paired reads; None for single-end data.
    pub fn properly_paired_pct(&self) -> Option<f64> {
        pct(self.properly_paired, self.paired_in_sequencing)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::inputs::test_fixtures::*;

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
    fn flagstat_counts_every_primary_record_for_the_read_count() {
        let text = "1000 + 7 in total (QC-passed reads + QC-failed reads)\n990 + 7 primary\n10 + 0 supplementary\n\
                    0 + 0 duplicates\n990 + 7 mapped (99.00% : N/A)\n";
        assert_eq!(Flagstat::parse(text).unwrap().primary_reads_all(), Some(997), "QC-failed reads are reads too");
        let old =
            "200 + 3 in total (QC-passed reads + QC-failed reads)\n10 + 0 duplicates\n150 + 1 mapped (75.00% : N/A)\n";
        assert_eq!(Flagstat::parse(old).unwrap().primary_reads_all(), None, "samtools < 1.13: no primary line");
    }
}
