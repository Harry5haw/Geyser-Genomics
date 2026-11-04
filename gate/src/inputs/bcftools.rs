//! bcftools stats -F <ref> -s - (stats/{id}/bcftools.stats.txt).

use std::collections::HashMap;

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
                    // A TSTV row whose counts don't parse is a broken file, not an undefined ratio.
                    let count = |s: &str| s.trim().parse::<u64>().map_err(|_| format!("bad TSTV count {s:?}"));
                    ts = Some(count(t)?);
                    tv = Some(count(v)?);
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::inputs::test_fixtures::*;

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
    fn a_tstv_row_that_does_not_parse_is_invalid() {
        let text = "SN\t0\tnumber of records:\t2\nSN\t0\tnumber of SNPs:\t2\nSN\t0\tnumber of indels:\t0\nTSTV\t0\t2\tx\t0.00\n";
        assert!(BcftoolsStats::parse(text).unwrap_err().contains("bad TSTV count"));
    }
}
