//! fastp's JSON report (qc_reports/{id}.fastp.json).

use serde::Deserialize;

/// The read-level numbers from fastp's JSON report. The QC stage runs fastp report-only (no trimming
/// or filtering), so "before filtering" is the upload as it is. With adapter trimming disabled, fastp
/// doesn't detect adapters: the report has no adapter numbers to check.
#[derive(Debug, Clone, PartialEq)]
pub struct Fastp {
    /// Reads in the upload (each mate counts).
    pub total_reads: u64,
    pub q30_rate: f64,
    pub gc_content: f64,
    pub duplication_rate: Option<f64>,
    pub paired: bool,
}

#[derive(Deserialize)]
struct FastpJson {
    summary: FastpSummary,
    duplication: Option<FastpDuplication>,
}

#[derive(Deserialize)]
struct FastpSummary {
    sequencing: String,
    before_filtering: FastpCounts,
}

#[derive(Deserialize)]
struct FastpCounts {
    total_reads: u64,
    q30_rate: f64,
    gc_content: f64,
}

#[derive(Deserialize)]
struct FastpDuplication {
    rate: f64,
}

impl Fastp {
    pub fn parse(text: &str) -> Result<Self, String> {
        let json: FastpJson = serde_json::from_str(text).map_err(|e| format!("not a fastp JSON report: {e}"))?;
        let paired = match json.summary.sequencing.split(" end").next() {
            Some("paired") => true,
            Some("single") => false,
            _ => return Err(format!("unknown fastp sequencing {:?}", json.summary.sequencing)),
        };
        let b = json.summary.before_filtering;
        Ok(Fastp {
            total_reads: b.total_reads,
            q30_rate: b.q30_rate,
            gc_content: b.gc_content,
            duplication_rate: json.duplication.map(|d| d.rate),
            paired,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::inputs::test_fixtures::*;

    #[test]
    fn fastp_report() {
        let single = Fastp::parse(SINGLE_FASTP).unwrap();
        assert_eq!((single.total_reads, single.q30_rate, single.paired), (10_000, 1.0, false));
        assert_eq!(single.duplication_rate, Some(0.0361));
        let paired = Fastp::parse(PAIRED_FASTP).unwrap();
        assert_eq!((paired.total_reads, paired.paired), (10_000, true), "both mates count, as in flagstat");
        let hg003 = Fastp::parse(HG003_FASTP).unwrap();
        assert_eq!(hg003.total_reads, 18_960_080, "= flagstat's primary reads for the same BAM");
        assert!((hg003.q30_rate - 0.9139).abs() < 1e-4 && (hg003.gc_content - 0.4386).abs() < 1e-4);
    }

    #[test]
    fn fastp_rejects_other_files() {
        assert!(Fastp::parse(r#"{"schema": 1}"#).unwrap_err().contains("not a fastp JSON report"));
        assert!(Fastp::parse(SINGLE_FLAGSTAT).is_err());
        let odd = SINGLE_FASTP.replace("single end (150 cycles)", "long reads");
        assert!(Fastp::parse(&odd).unwrap_err().contains("unknown fastp sequencing"));
    }
}
