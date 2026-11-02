//! Inputs + config -> verdict.

use std::collections::BTreeMap;

use serde::Serialize;

use crate::config::{Bounds, Config, Metric, OnMissing};
use crate::inputs::{Input, Inputs};

/// Version of the verdict's JSON layout. Bump it on a breaking change; adding fields is not one.
pub const VERDICT_SCHEMA: u32 = 1;

/// A metric's value: counts stay integers in the JSON, ratios are rounded to 4 decimals.
#[derive(Debug, Clone, Copy, PartialEq, Serialize)]
#[serde(untagged)]
pub enum Value {
    Count(u64),
    Ratio(f64),
}

impl Value {
    fn ratio(v: f64) -> Value {
        Value::Ratio((v * 10_000.0).round() / 10_000.0)
    }

    pub fn as_f64(self) -> f64 {
        match self {
            Value::Count(n) => n as f64,
            Value::Ratio(r) => r,
        }
    }
}

impl std::fmt::Display for Value {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Value::Count(n) => write!(f, "{n}"),
            Value::Ratio(r) => write!(f, "{r}"),
        }
    }
}

/// What a metric looks like for this sample.
#[derive(Debug, Clone, PartialEq)]
enum Observed {
    Value(Value),
    /// The metric doesn't apply to this sample (properly paired % of single-end reads). Never fails.
    NotApplicable(String),
    /// The input is there but the value isn't defined (Ti/Tv without transversions): counts as missing.
    Undefined(String),
    /// The input is missing: counts as missing.
    Missing(String),
    /// The input can't be parsed: always fails.
    Invalid(String),
}

impl Metric {
    pub fn input(self) -> Input {
        match self {
            Metric::Reads | Metric::MappedPct | Metric::DuplicatePct | Metric::ProperlyPairedPct => Input::Flagstat,
            Metric::MeanDepth => Input::Mosdepth,
            Metric::Variants | Metric::Snps | Metric::Indels | Metric::Tstv => Input::Bcftools,
        }
    }

    fn observe(self, inputs: &Inputs) -> Observed {
        let key = self.input().key(&inputs.sample_id);
        match inputs.state(self.input()) {
            (true, _) => return Observed::Missing(format!("{key} not found")),
            (_, Some(err)) => return Observed::Invalid(format!("could not read {key}: {err}")),
            _ => {}
        }
        let undefined = |why: &str| Observed::Undefined(format!("{self} is undefined: {why}"));
        let ratio = |v: Option<f64>, why: &str| v.map_or_else(|| undefined(why), |v| Observed::Value(Value::ratio(v)));
        // state() said Ok, so the input's value is there.
        let (f, m, b) = (inputs.flagstat.ok(), inputs.mosdepth.ok(), inputs.bcftools.ok());
        match self {
            Metric::Reads => Observed::Value(Value::Count(f.unwrap().reads())),
            Metric::MappedPct => ratio(f.unwrap().mapped_pct(), "no reads"),
            Metric::DuplicatePct => ratio(f.unwrap().duplicate_pct(), "no reads"),
            Metric::ProperlyPairedPct if !f.unwrap().is_paired() => {
                Observed::NotApplicable("single-end reads have no pairs".into())
            }
            Metric::ProperlyPairedPct => ratio(f.unwrap().properly_paired_pct(), "no paired reads"),
            Metric::MeanDepth => ratio(m.unwrap().mean_depth(), "the reference length is 0"),
            Metric::Variants => Observed::Value(Value::Count(b.unwrap().records)),
            Metric::Snps => Observed::Value(Value::Count(b.unwrap().snps)),
            Metric::Indels => Observed::Value(Value::Count(b.unwrap().indels)),
            Metric::Tstv => ratio(b.unwrap().tstv(), "no transversions"),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Status {
    Pass,
    Fail,
    /// Missing input or undefined value, with `on_missing = "skip"`.
    Skipped,
    NotApplicable,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Check {
    /// A metric name, or "run_status".
    pub name: String,
    pub status: Status,
    pub value: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub min: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub expected: Option<Vec<String>>,
    /// The input key the value comes from.
    pub source: String,
    /// Why it failed, was skipped or doesn't apply. None when it passed.
    pub reason: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct InputState {
    pub key: String,
    /// "ok", "missing" or "invalid".
    pub status: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Verdict {
    pub schema: u32,
    pub gate_version: &'static str,
    pub sample_id: String,
    pub run_name: Option<String>,
    pub layout: Option<String>,
    /// "pass" or "fail".
    pub verdict: &'static str,
    /// One line per failed check: the named reasons the run failed the gate.
    pub reasons: Vec<String>,
    /// Where the thresholds came from: a path, or "built-in default".
    pub config: String,
    pub on_missing: OnMissing,
    pub inputs: BTreeMap<&'static str, InputState>,
    /// Every metric the inputs give, checked or not (null when unavailable or not applicable).
    pub metrics: BTreeMap<Metric, Option<Value>>,
    pub checks: Vec<Check>,
}

impl Verdict {
    pub fn passed(&self) -> bool {
        self.verdict == "pass"
    }
}

fn bound_breach(metric: Metric, v: Value, b: &Bounds) -> Option<String> {
    let x = v.as_f64();
    if let Some(min) = b.min.filter(|min| x < *min) {
        return Some(format!("{metric} {v} is below the minimum {min}"));
    }
    if let Some(max) = b.max.filter(|max| x > *max) {
        return Some(format!("{metric} {v} is above the maximum {max}"));
    }
    None
}

/// (status, reason) for an input that isn't there or a value that isn't defined.
fn missing(on_missing: OnMissing, why: String) -> (Status, Option<String>) {
    match on_missing {
        OnMissing::Fail => (Status::Fail, Some(format!("missing: {why}"))),
        OnMissing::Skip => (Status::Skipped, Some(why)),
    }
}

fn metric_check(metric: Metric, b: &Bounds, inputs: &Inputs, on_missing: OnMissing) -> Check {
    let observed = metric.observe(inputs);
    let value = match &observed {
        Observed::Value(v) => Some(serde_json::to_value(v).expect("a number serialises")),
        _ => None,
    };
    let (status, reason) = match observed {
        Observed::Value(v) => match bound_breach(metric, v, b) {
            Some(reason) => (Status::Fail, Some(reason)),
            None => (Status::Pass, None),
        },
        Observed::NotApplicable(why) => (Status::NotApplicable, Some(why)),
        Observed::Undefined(why) | Observed::Missing(why) => missing(on_missing, why),
        Observed::Invalid(why) => (Status::Fail, Some(why)),
    };
    Check {
        name: metric.name().to_string(),
        status,
        value,
        min: b.min,
        max: b.max,
        expected: None,
        source: metric.input().key(&inputs.sample_id),
        reason,
    }
}

fn run_status_check(statuses: &[String], inputs: &Inputs, on_missing: OnMissing) -> Check {
    let key = Input::RunRecord.key(&inputs.sample_id);
    let status = inputs.record.ok().and_then(|r| r.status.clone());
    let (state, reason) = match inputs.state(Input::RunRecord) {
        (true, _) => missing(on_missing, format!("{key} not found")),
        (_, Some(err)) => (Status::Fail, Some(format!("could not read {key}: {err}"))),
        _ => match &status {
            None => missing(on_missing, "the run record has no status".into()),
            Some(s) if statuses.contains(s) => (Status::Pass, None),
            Some(s) => (Status::Fail, Some(format!("run_status is '{s}', expected {}", statuses.join(" or ")))),
        },
    };
    Check {
        name: "run_status".into(),
        status: state,
        value: status.map(serde_json::Value::String),
        min: None,
        max: None,
        expected: Some(statuses.to_vec()),
        source: key,
        reason,
    }
}

/// Apply `config` to `inputs`. `config_source` names the thresholds in the verdict.
pub fn evaluate(config: &Config, config_source: &str, inputs: &Inputs) -> Verdict {
    let mut checks = Vec::new();
    if let Some(rule) = &config.run_record {
        checks.push(run_status_check(&rule.statuses, inputs, config.on_missing));
    }
    for (metric, bounds) in &config.checks {
        checks.push(metric_check(*metric, bounds, inputs, config.on_missing));
    }
    let reasons: Vec<String> = checks
        .iter()
        .filter(|c| c.status == Status::Fail)
        .map(|c| c.reason.clone().unwrap_or_else(|| format!("{} failed", c.name)))
        .collect();
    let input_states = Input::ALL
        .into_iter()
        .map(|input| {
            let (missing, error) = inputs.state(input);
            let status = if missing {
                "missing"
            } else if error.is_some() {
                "invalid"
            } else {
                "ok"
            };
            let state = InputState { key: input.key(&inputs.sample_id), status, error: error.map(String::from) };
            (input.name(), state)
        })
        .collect();
    let metrics = Metric::ALL
        .into_iter()
        .map(|m| {
            let value = match m.observe(inputs) {
                Observed::Value(v) => Some(v),
                _ => None,
            };
            (m, value)
        })
        .collect();
    Verdict {
        schema: VERDICT_SCHEMA,
        gate_version: env!("CARGO_PKG_VERSION"),
        sample_id: inputs.sample_id.clone(),
        run_name: inputs.record.ok().and_then(|r| r.run_name.clone()),
        layout: inputs.layout(),
        verdict: if reasons.is_empty() { "pass" } else { "fail" },
        reasons,
        config: config_source.to_string(),
        on_missing: config.on_missing,
        inputs: input_states,
        metrics,
        checks,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::inputs::{BcftoolsStats, Flagstat, Loaded, MosdepthSummary, RunRecord};

    fn flagstat(reads: u64, mapped: u64, dups: u64, paired: u64, proper: u64) -> Flagstat {
        Flagstat {
            in_total: reads,
            primary: Some(reads),
            mapped,
            primary_mapped: Some(mapped),
            duplicates: dups,
            primary_duplicates: Some(dups),
            paired_in_sequencing: paired,
            properly_paired: proper,
        }
    }

    fn inputs() -> Inputs {
        Inputs {
            sample_id: "S1".into(),
            flagstat: Loaded::Ok(flagstat(1000, 990, 50, 1000, 980)),
            mosdepth: Loaded::Ok(MosdepthSummary { length: 100, bases: 3000 }),
            bcftools: Loaded::Ok(BcftoolsStats {
                records: 12,
                snps: 10,
                indels: 2,
                transitions: Some(20),
                transversions: Some(10),
            }),
            record: Loaded::Ok(RunRecord {
                schema: Some(1),
                sample_id: Some("S1".into()),
                run_name: Some("S1-20261102T100000Z-abc".into()),
                status: Some("succeeded".into()),
                layout: Some("paired".into()),
            }),
        }
    }

    fn config(text: &str) -> Config {
        Config::parse(text).unwrap()
    }

    fn check<'a>(v: &'a Verdict, name: &str) -> &'a Check {
        v.checks.iter().find(|c| c.name == name).unwrap()
    }

    const ALL_CHECKS: &str = r#"
        [run_record]
        statuses = ["succeeded"]
        [checks]
        reads = { min = 1000 }
        mapped_pct = { min = 99.0 }
        duplicate_pct = { max = 5.0 }
        properly_paired_pct = { min = 98.0 }
        mean_depth = { min = 30, max = 30 }
        variants = { min = 1 }
        snps = { min = 10, max = 10 }
        indels = { max = 2 }
        tstv = { min = 2.0, max = 2.0 }
    "#;

    #[test]
    fn values_on_the_bounds_pass() {
        let v = evaluate(&config(ALL_CHECKS), "test", &inputs());
        assert!(v.passed(), "{:?}", v.reasons);
        assert_eq!(v.checks.len(), 10);
        assert!(v.checks.iter().all(|c| c.status == Status::Pass && c.reason.is_none()));
        assert_eq!(v.run_name.as_deref(), Some("S1-20261102T100000Z-abc"));
        assert_eq!(v.layout.as_deref(), Some("paired"));
    }

    #[test]
    fn each_breach_is_a_named_reason() {
        let mut i = inputs();
        i.flagstat = Loaded::Ok(flagstat(1000, 800, 120, 1000, 700));
        let v = evaluate(&config(ALL_CHECKS), "test", &i);
        assert!(!v.passed());
        assert_eq!(
            v.reasons,
            [
                "mapped_pct 80 is below the minimum 99",
                "duplicate_pct 12 is above the maximum 5",
                "properly_paired_pct 70 is below the minimum 98",
            ]
        );
        assert_eq!(check(&v, "mapped_pct").source, "stats/S1/flagstat.txt");
        assert_eq!(check(&v, "mean_depth").status, Status::Pass);
    }

    #[test]
    fn a_failed_run_fails_the_gate() {
        let mut i = inputs();
        if let Loaded::Ok(r) = &mut i.record {
            r.status = Some("failed".into());
        }
        let v = evaluate(&config(ALL_CHECKS), "test", &i);
        assert_eq!(v.reasons, ["run_status is 'failed', expected succeeded"]);
        let c = check(&v, "run_status");
        assert_eq!(c.value, Some(serde_json::json!("failed")));
        assert_eq!(c.expected.as_deref(), Some(&["succeeded".to_string()][..]));
    }

    #[test]
    fn properly_paired_does_not_apply_to_single_end_reads() {
        let mut i = inputs();
        i.flagstat = Loaded::Ok(flagstat(1000, 1000, 10, 0, 0));
        let v = evaluate(&config(ALL_CHECKS), "test", &i);
        let c = check(&v, "properly_paired_pct");
        assert_eq!((c.status, c.value.clone()), (Status::NotApplicable, None));
        assert!(v.passed());
        assert_eq!(v.layout.as_deref(), Some("single"), "the alignment's layout wins over the record's");
        assert_eq!(v.metrics[&Metric::ProperlyPairedPct], None);
    }

    #[test]
    fn missing_inputs_fail_by_default_and_can_be_skipped() {
        let mut i = inputs();
        i.bcftools = Loaded::Missing;
        let v = evaluate(&config(ALL_CHECKS), "test", &i);
        assert_eq!(v.reasons.len(), 4, "variants, snps, indels and tstv: {:?}", v.reasons);
        assert_eq!(v.reasons[0], "missing: stats/S1/bcftools.stats.txt not found");
        assert_eq!(v.inputs["bcftools_stats"].status, "missing");

        let skip = config(&format!("on_missing = \"skip\"\n{ALL_CHECKS}"));
        let v = evaluate(&skip, "test", &i);
        assert!(v.passed());
        assert_eq!(check(&v, "tstv").status, Status::Skipped);
    }

    #[test]
    fn undefined_tstv_counts_as_missing() {
        let mut i = inputs();
        i.bcftools =
            Loaded::Ok(BcftoolsStats { records: 3, snps: 3, indels: 0, transitions: Some(3), transversions: Some(0) });
        let text = "[checks]\ntstv = { min = 1.9 }";
        let v = evaluate(&config(text), "test", &i);
        assert_eq!(v.reasons, ["missing: tstv is undefined: no transversions"]);
        let v = evaluate(&config(&format!("on_missing = \"skip\"\n{text}")), "test", &i);
        assert!(v.passed());
    }

    #[test]
    fn an_unreadable_input_fails_even_when_missing_inputs_are_skipped() {
        let mut i = inputs();
        i.mosdepth = Loaded::Invalid("no 'total' row".into());
        i.record = Loaded::Invalid("not a run record".into());
        let v = evaluate(&config(&format!("on_missing = \"skip\"\n{ALL_CHECKS}")), "test", &i);
        assert_eq!(
            v.reasons,
            [
                "could not read runs/S1.provenance.json: not a run record",
                "could not read stats/S1/mosdepth.summary.txt: no 'total' row",
            ]
        );
        assert_eq!(v.inputs["mosdepth_summary"].error.as_deref(), Some("no 'total' row"));
        assert_eq!(v.run_name, None);
    }

    #[test]
    fn unchecked_metrics_are_still_reported() {
        let v = evaluate(&config(""), "test", &inputs());
        assert!(v.passed() && v.checks.is_empty());
        assert_eq!(v.metrics[&Metric::Tstv], Some(Value::Ratio(2.0)));
        assert_eq!(v.metrics[&Metric::Reads], Some(Value::Count(1000)));
        assert_eq!(v.metrics.len(), Metric::ALL.len());
    }

    #[test]
    fn verdict_json_layout() {
        let v = evaluate(&config(ALL_CHECKS), "thresholds/x.toml", &inputs());
        let json = serde_json::to_value(&v).unwrap();
        assert_eq!(json["schema"], 1);
        assert_eq!(json["verdict"], "pass");
        assert_eq!(json["config"], "thresholds/x.toml");
        assert_eq!(json["on_missing"], "fail");
        assert_eq!(json["metrics"]["reads"], 1000, "counts stay integers");
        assert_eq!(json["metrics"]["mean_depth"], 30.0);
        assert_eq!(json["checks"][0]["name"], "run_status");
        assert_eq!(json["checks"][1]["name"], "reads", "metric checks in declaration order");
        assert!(json["checks"][1].get("expected").is_none());
        assert_eq!(json["inputs"]["run_record"]["key"], "runs/S1.provenance.json");
    }

    #[test]
    fn ratios_are_rounded_to_four_decimals() {
        assert_eq!(Value::ratio(485.0 * 100.0 / 10_000.0), Value::Ratio(4.85));
        assert_eq!(Value::ratio(2.0 / 3.0), Value::Ratio(0.6667));
    }
}
