//! mosdepth -n: the summary (stats/{id}/mosdepth.summary.txt) and the cumulative depth distribution
//! (stats/{id}/mosdepth.global.dist.txt).

use std::collections::BTreeMap;

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

/// The `total` rows of `<prefix>.mosdepth.global.dist.txt`: depth -> fraction of the reference at
/// least that deep. mosdepth writes the fractions with 2 decimals, and leaves out a depth when no
/// base is at exactly that depth (the cumulative fraction doesn't change there): HG005 chr20 has 13
/// such gaps above 1,800x.
#[derive(Debug, Clone, PartialEq)]
pub struct MosdepthDist {
    /// Fraction of the reference at >= depth, for the depths mosdepth lists.
    pub at_least: BTreeMap<u64, f64>,
}

impl MosdepthDist {
    pub fn parse(text: &str) -> Result<Self, String> {
        let mut at_least = BTreeMap::new();
        for line in text.lines() {
            let cols: Vec<&str> = line.split('\t').collect();
            if let ["total", depth, fraction] = cols.as_slice() {
                let bad = || format!("bad total row {line:?}");
                at_least.insert(depth.parse::<u64>().map_err(|_| bad())?, fraction.parse::<f64>().map_err(|_| bad())?);
            }
        }
        if at_least.is_empty() {
            return Err("no 'total' rows: not a mosdepth global distribution".into());
        }
        Ok(MosdepthDist { at_least })
    }

    /// % of the reference covered at >= `depth`. A depth mosdepth left out has no base at exactly that
    /// depth, so it has the fraction of the next listed depth above it; deeper than any base: 0.
    pub fn breadth_pct(&self, depth: u64) -> f64 {
        self.at_least.range(depth..).next().map_or(0.0, |(_, f)| f * 100.0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::inputs::test_fixtures::*;

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
    fn mosdepth_distribution() {
        let hg003 = MosdepthDist::parse(HG003_DIST).unwrap();
        assert_eq!((hg003.breadth_pct(1), hg003.breadth_pct(10), hg003.breadth_pct(20)), (99.0, 98.0, 97.0));
        let single = MosdepthDist::parse(SINGLE_DIST).unwrap();
        assert_eq!(single.breadth_pct(10), 98.0, "--leading-gap 1000: 1 kb of the 51 kb is N, never covered");
        assert_eq!(MosdepthDist::parse(RUN_DIST).unwrap().breadth_pct(10), 100.0, "no N gap");
        let deepest = *single.at_least.keys().last().unwrap();
        assert_eq!(single.breadth_pct(deepest + 1), 0.0, "deeper than the deepest base");
        assert!(MosdepthDist::parse(SINGLE_FLAGSTAT).unwrap_err().contains("no 'total' rows"));
        assert!(MosdepthDist::parse("total\tten\t0.5\n").unwrap_err().contains("bad total row"));
        // A depth mosdepth left out: no base exactly there, so >= 1x is the same as >= 5x.
        let gap = MosdepthDist::parse("total\t5\t0.10\ntotal\t0\t1.00\n").unwrap();
        assert_eq!(
            (gap.breadth_pct(0), gap.breadth_pct(1), gap.breadth_pct(5), gap.breadth_pct(6)),
            (100.0, 10.0, 10.0, 0.0)
        );
        let hg005 =
            MosdepthDist::parse(include_str!("../../tests/fixtures/HG005_chr20/mosdepth.global.dist.txt")).unwrap();
        assert!(!hg005.at_least.contains_key(&1865), "real gaps exist");
        assert_eq!(hg005.breadth_pct(1865), hg005.breadth_pct(1866));
        assert_eq!(hg005.breadth_pct(10), 98.0);
    }
}
