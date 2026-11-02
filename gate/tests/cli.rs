//! The binary end to end: bucket-shaped folders built from real pipeline output, exit codes, the verdict JSON.
//!
//! Fixtures, all real output:
//! - ../tests/fixtures/stats/{single,paired}.*: the synthetic samples' stats (shared with the Python tests)
//! - tests/fixtures/{SYNTH001,PAIRED001}.provenance.json: run records from `run_local.py --check` (single, paired)
//! - tests/fixtures/{HG003,HG005}_chr20/: samtools flagstat, mosdepth and bcftools stats on the GIAB chr20
//!   benchmark alignments and bcftools calls (geyser-data), the data the default thresholds were checked on

use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use serde_json::{Value, json};

const STATS_FILES: [&str; 3] = ["flagstat.txt", "mosdepth.summary.txt", "bcftools.stats.txt"];

fn crate_path(rel: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join(rel)
}

fn synthetic_config() -> String {
    crate_path("thresholds/synthetic.toml").display().to_string()
}

/// A root laid out like local_bucket/ for one synthetic sample ("single" -> SYNTH001, "paired" -> PAIRED001)
/// with its run record's status set to `status`. Returns (root, sample ID).
fn synthetic_bucket(layout: &str, status: &str) -> (tempfile::TempDir, &'static str) {
    let sample = if layout == "paired" { "PAIRED001" } else { "SYNTH001" };
    let dir = tempfile::tempdir().unwrap();
    let stats = dir.path().join("stats").join(sample);
    fs::create_dir_all(&stats).unwrap();
    for name in STATS_FILES {
        fs::copy(crate_path(&format!("../tests/fixtures/stats/{layout}.{name}")), stats.join(name)).unwrap();
    }
    let record = fs::read_to_string(crate_path(&format!("tests/fixtures/{sample}.provenance.json"))).unwrap();
    let record = record.replace("\"status\": \"succeeded\"", &format!("\"status\": \"{status}\""));
    fs::create_dir_all(dir.path().join("runs")).unwrap();
    fs::write(dir.path().join(format!("runs/{sample}.provenance.json")), record).unwrap();
    (dir, sample)
}

fn gate(args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_geyser-gate")).args(args).output().unwrap()
}

/// (exit code, verdict from stdout or Null, stderr)
fn run(root: &Path, sample: &str, extra: &[&str]) -> (i32, Value, String) {
    let root = root.display().to_string();
    let mut args = vec!["--sample", sample, "--root", &root];
    args.extend_from_slice(extra);
    let out = gate(&args);
    let verdict = serde_json::from_slice(&out.stdout).unwrap_or(Value::Null);
    (out.status.code().unwrap(), verdict, String::from_utf8(out.stderr).unwrap())
}

fn check<'a>(verdict: &'a Value, name: &str) -> &'a Value {
    verdict["checks"].as_array().unwrap().iter().find(|c| c["name"] == name).unwrap()
}

fn reasons(verdict: &Value) -> Vec<&str> {
    verdict["reasons"].as_array().unwrap().iter().map(|r| r.as_str().unwrap()).collect()
}

#[test]
fn synthetic_single_end_passes_its_profile() {
    let (root, sample) = synthetic_bucket("single", "succeeded");
    let (code, v, stderr) = run(root.path(), sample, &["--config", &synthetic_config()]);
    assert_eq!(code, 0, "{stderr}");
    assert_eq!(v["verdict"], "pass");
    assert_eq!(v["reasons"], json!([]));
    assert_eq!(v["layout"], "single");
    assert_eq!(v["run_name"], "SYNTH001-20261102T152425Z-local");
    assert_eq!(v["metrics"]["reads"], 10000);
    assert_eq!(v["metrics"]["duplicate_pct"], 4.85);
    assert_eq!(v["metrics"]["mean_depth"], 27.9853);
    assert_eq!(v["metrics"]["tstv"], 1.5);
    assert_eq!(v["metrics"]["properly_paired_pct"], Value::Null);
    assert_eq!(check(&v, "properly_paired_pct")["status"], "not_applicable");
    assert_eq!(check(&v, "run_status")["value"], "succeeded");
    assert!(stderr.contains("SYNTH001: PASS (8 checks)"), "{stderr}");
}

#[test]
fn synthetic_paired_end_passes_its_profile() {
    let (root, sample) = synthetic_bucket("paired", "succeeded");
    let (code, v, stderr) = run(root.path(), sample, &["--config", &synthetic_config()]);
    assert_eq!(code, 0, "{stderr}");
    assert_eq!(v["layout"], "paired");
    assert_eq!(check(&v, "properly_paired_pct")["value"], 100.0);
    assert_eq!(v["metrics"]["duplicate_pct"], 0.04);
}

#[test]
fn giab_chr20_samples_pass_the_default_thresholds() {
    // (sample, mapped %, duplicate %, properly paired %, mean depth, SNPs, Ti/Tv)
    for (sample, mapped, dups, proper, depth, snps, tstv) in [
        ("HG003_chr20", 99.7744, 10.9884, 99.0502, 36.7812, 118_410, 1.8103),
        ("HG005_chr20", 99.8188, 10.2397, 96.9715, 32.6433, 109_870, 1.8482),
    ] {
        let dir = tempfile::tempdir().unwrap();
        let stats = dir.path().join("stats").join(sample);
        fs::create_dir_all(&stats).unwrap();
        for name in STATS_FILES {
            fs::copy(crate_path(&format!("tests/fixtures/{sample}/{name}")), stats.join(name)).unwrap();
        }
        // These ran in the benchmark scripts, not run_local, so they have no record of their own.
        fs::create_dir_all(dir.path().join("runs")).unwrap();
        let record = json!({"schema": 1, "sample_id": sample, "status": "succeeded", "run_name": null});
        fs::write(dir.path().join(format!("runs/{sample}.provenance.json")), record.to_string()).unwrap();

        let (code, v, stderr) = run(dir.path(), sample, &[]);
        assert_eq!(code, 0, "{sample}: {stderr}");
        let m = &v["metrics"];
        assert_eq!(
            [
                &m["mapped_pct"],
                &m["duplicate_pct"],
                &m["properly_paired_pct"],
                &m["mean_depth"],
                &m["snps"],
                &m["tstv"]
            ],
            [&json!(mapped), &json!(dups), &json!(proper), &json!(depth), &json!(snps), &json!(tstv)],
            "{sample}"
        );
        assert_eq!(v["layout"], "paired");
    }
}

#[test]
fn the_default_human_thresholds_fail_synthetic_data_with_a_named_reason() {
    // 10 planted SNPs with Ti/Tv 1.5 are nothing like a human genome's calls.
    let (root, sample) = synthetic_bucket("single", "succeeded");
    let (code, v, stderr) = run(root.path(), sample, &[]);
    assert_eq!(code, 1);
    assert_eq!(v["verdict"], "fail");
    assert_eq!(v["config"], "built-in default");
    assert_eq!(reasons(&v), ["tstv 1.5 is below the minimum 1.7"]);
    assert!(stderr.contains("FAIL  tstv"), "{stderr}");
    assert!(stderr.contains("SYNTH001: FAIL (1 of 7 checks)"), "{stderr}");
}

#[test]
fn a_failed_run_record_fails_the_gate() {
    let (root, sample) = synthetic_bucket("paired", "failed");
    let (code, v, _) = run(root.path(), sample, &["--config", &synthetic_config()]);
    assert_eq!(code, 1);
    assert_eq!(reasons(&v), ["run_status is 'failed', expected succeeded"]);
}

#[test]
fn a_missing_stats_file_fails_unless_skipped() {
    let (root, sample) = synthetic_bucket("single", "succeeded");
    fs::remove_file(root.path().join(format!("stats/{sample}/mosdepth.summary.txt"))).unwrap();
    let (code, v, _) = run(root.path(), sample, &["--config", &synthetic_config()]);
    assert_eq!(code, 1);
    assert_eq!(v["inputs"]["mosdepth_summary"]["status"], "missing");
    assert_eq!(reasons(&v), ["missing: stats/SYNTH001/mosdepth.summary.txt not found"]);

    let skip = root.path().join("skip.toml");
    let profile = fs::read_to_string(synthetic_config()).unwrap();
    fs::write(&skip, profile.replace("on_missing = \"fail\"", "on_missing = \"skip\"")).unwrap();
    let (code, v, _) = run(root.path(), sample, &["--config", &skip.display().to_string()]);
    assert_eq!(code, 0);
    assert_eq!(check(&v, "mean_depth")["status"], "skipped");
}

#[test]
fn a_corrupt_stats_file_fails_with_its_parse_error() {
    let (root, sample) = synthetic_bucket("paired", "succeeded");
    fs::write(root.path().join(format!("stats/{sample}/flagstat.txt")), "truncated").unwrap();
    let (code, v, _) = run(root.path(), sample, &["--config", &synthetic_config()]);
    assert_eq!(code, 1);
    assert_eq!(v["inputs"]["flagstat"]["status"], "invalid");
    assert_eq!(reasons(&v).len(), 4, "reads, mapped, duplicates, properly paired: {:?}", reasons(&v));
    assert!(reasons(&v)[0].contains("not samtools flagstat output"));
    assert_eq!(v["layout"], "paired", "from the run record when flagstat is unusable");
}

#[test]
fn output_goes_to_a_file_and_quiet_silences_the_summary() {
    let (root, sample) = synthetic_bucket("single", "succeeded");
    let out = root.path().join("verdict.json");
    let out_arg = out.display().to_string();
    let (code, stdout, stderr) =
        run(root.path(), sample, &["--config", &synthetic_config(), "--output", &out_arg, "-q"]);
    assert_eq!((code, stdout, stderr.as_str()), (0, Value::Null, ""));
    let v: Value = serde_json::from_str(&fs::read_to_string(out).unwrap()).unwrap();
    assert_eq!((&v["schema"], &v["sample_id"], &v["verdict"]), (&json!(1), &json!(sample), &json!("pass")));
}

#[test]
fn usage_and_config_errors_exit_2() {
    let (root, sample) = synthetic_bucket("single", "succeeded");
    let bad = root.path().join("bad.toml");
    fs::write(&bad, "[checks]\nmapped_percent = { min = 95 }\n").unwrap();
    let (code, v, stderr) = run(root.path(), sample, &["--config", &bad.display().to_string()]);
    assert_eq!((code, v), (2, Value::Null));
    assert!(stderr.contains("unknown variant `mapped_percent`"), "{stderr}");

    let (code, _, stderr) = run(root.path(), sample, &["--config", "does-not-exist.toml"]);
    assert_eq!(code, 2, "{stderr}");

    let root_arg = root.path().display().to_string();
    let out = gate(&["--sample", "../escape", "--root", &root_arg]);
    assert_eq!(out.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&out.stderr).contains("invalid sample ID"));

    let out = gate(&["--sample", sample, "--root", "no/such/folder"]);
    assert_eq!(out.status.code(), Some(2));

    let out = gate(&["--root", "."]);
    assert_eq!(out.status.code(), Some(2), "--sample is required");
}

#[test]
fn print_default_config_prints_the_built_in_thresholds() {
    let out = gate(&["--print-default-config"]);
    assert_eq!(out.status.code(), Some(0));
    let printed = String::from_utf8(out.stdout).unwrap();
    assert_eq!(printed, fs::read_to_string(crate_path("thresholds/default.toml")).unwrap());
}
