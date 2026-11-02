//! Thresholds, from a TOML file (see thresholds/default.toml for every option, documented).
//!
//! A config file is the whole set: checks it doesn't list are not run. Without `--config` the gate
//! uses thresholds/default.toml, compiled into the binary.

use std::collections::BTreeMap;
use std::fmt;

use serde::{Deserialize, Serialize};

pub const DEFAULT_TOML: &str = include_str!("../thresholds/default.toml");

/// A number the gate can check. Declaration order is the order of the verdict's checks and metrics.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Metric {
    /// Reads (each mate counts), primary records, QC-passed. flagstat.
    Reads,
    /// Mapped reads, % of reads. flagstat.
    MappedPct,
    /// Reads marked as duplicates, % of reads. flagstat.
    DuplicatePct,
    /// Properly paired reads, % of paired reads. Paired-end only; not applicable to single-end. flagstat.
    ProperlyPairedPct,
    /// Mean depth over the whole reference (duplicates excluded). mosdepth summary.
    MeanDepth,
    /// VCF records. bcftools stats.
    Variants,
    /// SNPs. bcftools stats.
    Snps,
    /// Indels. bcftools stats.
    Indels,
    /// Transitions / transversions. Undefined without transversions. bcftools stats.
    Tstv,
}

impl Metric {
    pub const ALL: [Metric; 9] = [
        Metric::Reads,
        Metric::MappedPct,
        Metric::DuplicatePct,
        Metric::ProperlyPairedPct,
        Metric::MeanDepth,
        Metric::Variants,
        Metric::Snps,
        Metric::Indels,
        Metric::Tstv,
    ];

    pub fn name(self) -> &'static str {
        match self {
            Metric::Reads => "reads",
            Metric::MappedPct => "mapped_pct",
            Metric::DuplicatePct => "duplicate_pct",
            Metric::ProperlyPairedPct => "properly_paired_pct",
            Metric::MeanDepth => "mean_depth",
            Metric::Variants => "variants",
            Metric::Snps => "snps",
            Metric::Indels => "indels",
            Metric::Tstv => "tstv",
        }
    }
}

impl fmt::Display for Metric {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.name())
    }
}

/// Inclusive bounds: a value equal to `min` or `max` passes.
#[derive(Debug, Clone, Copy, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Bounds {
    pub min: Option<f64>,
    pub max: Option<f64>,
}

/// What a check counts as when its input file is missing or its value is undefined (Ti/Tv without
/// transversions). An input that exists but can't be parsed always fails.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum OnMissing {
    #[default]
    Fail,
    Skip,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunRecordRule {
    /// The run record statuses that pass (app/provenance.py: succeeded | failed | incomplete).
    pub statuses: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    #[serde(default)]
    pub on_missing: OnMissing,
    pub run_record: Option<RunRecordRule>,
    #[serde(default)]
    pub checks: BTreeMap<Metric, Bounds>,
}

impl Config {
    pub fn parse(text: &str) -> Result<Config, String> {
        let config: Config = toml::from_str(text).map_err(|e| e.to_string().trim_end().to_string())?;
        config.validate()?;
        Ok(config)
    }

    pub fn default_config() -> Config {
        Config::parse(DEFAULT_TOML).expect("thresholds/default.toml is valid (tested)")
    }

    fn validate(&self) -> Result<(), String> {
        for (metric, b) in &self.checks {
            if b.min.is_none() && b.max.is_none() {
                return Err(format!("checks.{metric}: give min, max or both (or leave the check out)"));
            }
            if b.min.into_iter().chain(b.max).any(|v| !v.is_finite()) {
                return Err(format!("checks.{metric}: bounds must be finite numbers"));
            }
            if let (Some(min), Some(max)) = (b.min, b.max) {
                if min > max {
                    return Err(format!("checks.{metric}: min {min} is greater than max {max}"));
                }
            }
        }
        if let Some(rule) = &self.run_record {
            if rule.statuses.is_empty() {
                return Err(
                    "run_record.statuses is empty: no run could pass (leave [run_record] out to skip the check)".into(),
                );
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_shipped_configs_are_valid() {
        let default = Config::default_config();
        assert!(default.checks.contains_key(&Metric::Tstv));
        assert_eq!(default.on_missing, OnMissing::Fail);
        let synthetic = Config::parse(include_str!("../thresholds/synthetic.toml")).unwrap();
        assert!(synthetic.checks.contains_key(&Metric::MeanDepth));
    }

    #[test]
    fn every_metric_name_matches_its_serde_name() {
        for m in Metric::ALL {
            let parsed: BTreeMap<Metric, Bounds> = toml::from_str(&format!("{} = {{ min = 1 }}", m.name())).unwrap();
            assert!(parsed.contains_key(&m), "{m}");
        }
    }

    #[test]
    fn an_empty_config_checks_nothing() {
        let c = Config::parse("").unwrap();
        assert!(c.checks.is_empty() && c.run_record.is_none());
    }

    #[test]
    fn integers_are_accepted_as_bounds() {
        let c = Config::parse("[checks]\nsnps = { min = 1 }\n").unwrap();
        assert_eq!(c.checks[&Metric::Snps].min, Some(1.0));
    }

    #[test]
    fn mistakes_are_named() {
        let err = |text: &str| Config::parse(text).unwrap_err();
        assert!(err("[checks]\nmapped_percent = { min = 1 }").contains("unknown variant"));
        assert!(err("[checks]\nmapped_pct = { minimum = 1 }").contains("unknown field"));
        assert!(err("[checks]\nmapped_pct = {}").contains("give min, max or both"));
        assert!(err("[checks]\ntstv = { min = 2.3, max = 1.9 }").contains("greater than max"));
        assert!(err("[checks]\ntstv = { min = nan }").contains("finite"));
        assert!(err("on_missing = \"ignore\"").contains("unknown variant"));
        assert!(err("[run_record]\nstatuses = []").contains("empty"));
        assert!(err("thresholds = 1").contains("unknown field"));
    }
}
