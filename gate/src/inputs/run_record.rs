//! The provenance run record (runs/{id}.provenance.json, app/provenance.py) and its stage parts.

use serde::Deserialize;

/// The run record schema this gate reads (app/provenance.py SCHEMA_VERSION; tests/test_consistency.py checks them).
pub const RUN_RECORD_SCHEMA: u64 = 1;

/// One entry of the record's `outputs`.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct Output {
    pub key: String,
}

/// The fields of the provenance run record (app/provenance.py, schema 1) the gate reads. Others are ignored.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct RunRecord {
    pub schema: Option<u64>,
    pub sample_id: Option<String>,
    pub run_name: Option<String>,
    pub status: Option<String>,
    pub layout: Option<String>,
    /// When the record was assembled (`%Y-%m-%dT%H:%M:%SZ`, so string order is time order).
    pub updated: Option<String>,
    /// Every key the record's run uploaded (upstream stages inherited from earlier runs included).
    pub outputs: Option<Vec<Output>>,
}

impl RunRecord {
    pub fn parse(text: &str) -> Result<Self, String> {
        let record: RunRecord = serde_json::from_str(text).map_err(|e| format!("not a run record: {e}"))?;
        match record.schema {
            Some(RUN_RECORD_SCHEMA) | None => Ok(record),
            Some(other) => Err(format!("run record schema {other}; this gate reads schema {RUN_RECORD_SCHEMA}")),
        }
    }

    pub(super) fn has_output(&self, key: &str) -> Option<bool> {
        self.outputs.as_ref().map(|outputs| outputs.iter().any(|o| o.key == key))
    }
}

/// The fields of one stage's part (runs/{id}.provenance/{Task}.json) the gate reads.
#[derive(Debug, Deserialize)]
pub(super) struct Part {
    pub(super) task: Option<String>,
    pub(super) finished: Option<String>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn run_record() {
        let r = RunRecord::parse(r#"{"schema": 1, "sample_id": "S1", "status": "succeeded", "layout": "paired", "run_name": null, "stages": {}}"#).unwrap();
        assert_eq!(r.status.as_deref(), Some("succeeded"));
        assert_eq!(r.run_name, None);
        assert!(RunRecord::parse(r#"{"schema": 2}"#).unwrap_err().contains("schema 2"));
        assert!(RunRecord::parse("[]").is_err());
        assert!(RunRecord::parse("not json").is_err());
    }
}
