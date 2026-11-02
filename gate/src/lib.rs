//! Geyser quality gate: a sample's QC statistics and run record against configurable thresholds.
//!
//! [`inputs`] parses what the pipeline already writes, [`config`] holds the thresholds, and
//! [`gate::evaluate`] turns both into a [`gate::Verdict`]. The binary (src/main.rs) prints the verdict
//! as JSON and exits 0 (pass), 1 (fail) or 2 (usage or config error).

pub mod config;
pub mod gate;
pub mod inputs;

/// A sample ID the gate will turn into paths: no separators, no `..`, nothing empty.
pub fn valid_sample_id(id: &str) -> bool {
    !id.is_empty() && id != "." && id != ".." && !id.contains(['/', '\\']) && !id.chars().any(char::is_control)
}

#[cfg(test)]
mod tests {
    #[test]
    fn sample_ids_cannot_leave_the_root() {
        for good in ["SYNTH001", "HG003_chr20", "S1.v2", "CLOUD002"] {
            assert!(super::valid_sample_id(good), "{good}");
        }
        for bad in ["", ".", "..", "../S1", "a/b", "a\\b", "S1\n"] {
            assert!(!super::valid_sample_id(bad), "{bad:?}");
        }
    }
}
