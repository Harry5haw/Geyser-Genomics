//! The binary end to end: bucket-shaped folders built from real pipeline output, exit codes, the verdict JSON.
//!
//! Fixtures, all real output:
//! - tests/fixtures/{SYNTH001,PAIRED001}/: everything one `run_local.py --check` run wrote for the sample
//!   (single-end, paired-end): fastp JSON, flagstat, mosdepth summary and distribution, bcftools stats, run record
//! - tests/fixtures/{HG003,HG005}_chr20/: the same tools on the GIAB chr20 benchmark data (geyser-data),
//!   the data the default thresholds were checked on. No run record (not run through run_local).
//!   HG003's alignment is the pipeline's align stage on its reads (aligner benchmark); HG005's is the
//!   downloaded 30x BAM, and its fastp report is of reads extracted from that BAM.
//! - tests/fixtures/{CLOUD015,CLOUD006}/: two cloud smoke checkpoints (single-, paired-end) as the cloud wrote
//!   them, run record included: 10,000 reads from a 50 kb window of chr20, aligned to the whole of chr20
//!   (the data thresholds/smoke.toml is calibrated on)
//! - tests/fixtures/HG003_chr20_giraffe/: HG003's same reads aligned to the HPRC chr20 graph with vg giraffe
//!   (giraffe benchmark, #129) and called with DeepVariant: bcftools stats of its PASS calls, and of all
//!   its records (RefCall candidates included) to show why DeepVariant stats must be PASS-only.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use serde_json::{Value, json};

/// fixture file -> key under the root, for sample `{s}`
const FILES: [(&str, &str); 5] = [
    ("fastp.json", "qc_reports/{s}.fastp.json"),
    ("flagstat.txt", "stats/{s}/flagstat.txt"),
    ("mosdepth.summary.txt", "stats/{s}/mosdepth.summary.txt"),
    ("mosdepth.global.dist.txt", "stats/{s}/mosdepth.global.dist.txt"),
    ("bcftools.stats.txt", "stats/{s}/bcftools.stats.txt"),
];

fn crate_path(rel: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join(rel)
}

fn synthetic_config() -> String {
    crate_path("thresholds/synthetic.toml").display().to_string()
}

/// A root laid out like local_bucket/ holding fixture folder `sample`'s files (and `record`, if given).
fn bucket(sample: &str, record: Option<String>) -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    for (fixture, key) in FILES {
        let to = dir.path().join(key.replace("{s}", sample));
        fs::create_dir_all(to.parent().unwrap()).unwrap();
        fs::copy(crate_path(&format!("tests/fixtures/{sample}/{fixture}")), to).unwrap();
    }
    if let Some(record) = record {
        fs::create_dir_all(dir.path().join("runs")).unwrap();
        fs::write(dir.path().join(format!("runs/{sample}.provenance.json")), record).unwrap();
    }
    dir
}

/// One synthetic sample ("single" -> SYNTH001, "paired" -> PAIRED001) with its run record's status set
/// to `status`. Returns (root, sample ID).
fn synthetic_bucket(layout: &str, status: &str) -> (tempfile::TempDir, &'static str) {
    let sample = if layout == "paired" { "PAIRED001" } else { "SYNTH001" };
    let record = fs::read_to_string(crate_path(&format!("tests/fixtures/{sample}/provenance.json"))).unwrap();
    let record = record.replace("\"status\": \"succeeded\"", &format!("\"status\": \"{status}\""));
    (bucket(sample, Some(record)), sample)
}

/// A GIAB sample with a minimal succeeded record (no outputs list, so every stats file counts).
fn giab_bucket(sample: &str) -> tempfile::TempDir {
    let record = json!({"schema": 1, "sample_id": sample, "status": "succeeded", "run_name": null});
    bucket(sample, Some(record.to_string()))
}

/// The giraffe alignment of HG003's reads: its own stats, HG003's fastp report (the same reads), and
/// `bcftools` = which DeepVariant stats file to use as bcftools.stats.txt.
fn giraffe_bucket(bcftools: &str) -> tempfile::TempDir {
    let sample = "HG003_chr20_giraffe";
    let dir = tempfile::tempdir().unwrap();
    for (fixture, key) in FILES {
        let from = match fixture {
            "fastp.json" => "HG003_chr20/fastp.json".to_string(),
            "bcftools.stats.txt" => format!("{sample}/{bcftools}"),
            _ => format!("{sample}/{fixture}"),
        };
        let to = dir.path().join(key.replace("{s}", sample));
        fs::create_dir_all(to.parent().unwrap()).unwrap();
        fs::copy(crate_path(&format!("tests/fixtures/{from}")), to).unwrap();
    }
    fs::create_dir_all(dir.path().join("runs")).unwrap();
    let record = json!({"schema": 1, "sample_id": sample, "status": "succeeded", "run_name": null});
    fs::write(dir.path().join(format!("runs/{sample}.provenance.json")), record.to_string()).unwrap();
    dir
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
    assert_eq!(v["metrics"]["mean_depth"], 28.545);
    assert_eq!(v["metrics"]["tstv"], 1.5);
    assert_eq!(v["metrics"]["q30_pct"], 100.0);
    assert_eq!(v["metrics"]["gc_pct"], 49.5339);
    assert_eq!(v["metrics"]["fastp_duplicate_pct"], 3.61);
    assert_eq!(v["metrics"]["read_count_difference"], 0);
    assert_eq!(v["metrics"]["breadth_10x_pct"], 100.0);
    assert_eq!(v["metrics"]["properly_paired_pct"], Value::Null);
    assert_eq!(check(&v, "properly_paired_pct")["status"], "not_applicable");
    assert_eq!(check(&v, "run_status")["value"], "succeeded");
    assert!(stderr.contains("SYNTH001: PASS (12 checks)"), "{stderr}");
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
fn hg003_chr20_passes_the_default_thresholds() {
    let dir = giab_bucket("HG003_chr20");
    let (code, v, stderr) = run(dir.path(), "HG003_chr20", &[]);
    assert_eq!(code, 0, "{stderr}");
    let m = &v["metrics"];
    let expected = json!({
        "q30_pct": 91.3949, "gc_pct": 43.8635, "fastp_duplicate_pct": 5.7937, "reads": 18960080,
        "read_count_difference": 0, "mapped_pct": 99.7744, "duplicate_pct": 10.9884,
        "properly_paired_pct": 99.0502, "mean_depth": 36.7812, "breadth_1x_pct": 99.0, "breadth_10x_pct": 98.0,
        "breadth_20x_pct": 97.0, "variants": 137668, "snps": 118410, "indels": 19258, "tstv": 1.8103,
    });
    assert_eq!(m, &expected);
    assert_eq!(v["layout"], "paired");
}

#[test]
fn hg005_chr20_fails_only_on_an_alignment_that_is_not_from_its_reads() {
    // A mismatch of inputs, which the check exists to catch: the alignment is the downloaded BAM, and the
    // fastp report is of reads extracted from it, minus the 377,078 whose mates map to other chromosomes
    // (samtools fastq drops them). So the alignment holds exactly 377,078 reads more than the "upload".
    let dir = giab_bucket("HG005_chr20");
    let (code, v, _) = run(dir.path(), "HG005_chr20", &[]);
    assert_eq!(code, 1);
    assert_eq!(reasons(&v), ["read_count_difference 377078 is above the maximum 0"]);
    let m = &v["metrics"];
    assert_eq!(
        [&m["q30_pct"], &m["gc_pct"], &m["mapped_pct"], &m["mean_depth"], &m["breadth_10x_pct"], &m["tstv"]],
        [&json!(91.7611), &json!(44.0655), &json!(99.8188), &json!(32.6433), &json!(98.0), &json!(1.8482)]
    );
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
    assert!(stderr.contains("SYNTH001: FAIL (1 of 11 checks)"), "{stderr}");
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
    assert_eq!(reasons(&v).len(), 5, "reads, in alignment, mapped, duplicates, properly paired: {:?}", reasons(&v));
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

#[test]
fn a_graph_alignment_passes_the_default_thresholds() {
    let dir = giraffe_bucket("bcftools.stats.txt");
    let (code, v, stderr) = run(dir.path(), "HG003_chr20_giraffe", &[]);
    assert_eq!(code, 0, "{stderr}");
    let m = &v["metrics"];
    assert_eq!(m["read_count_difference"], 0, "giraffe keeps every read, as bwa-mem2 does");
    assert_eq!(
        [&m["mapped_pct"], &m["duplicate_pct"], &m["properly_paired_pct"], &m["mean_depth"], &m["breadth_10x_pct"]],
        [&json!(98.7374), &json!(10.989), &json!(97.6577), &json!(36.2856), &json!(98.0)]
    );
    assert_eq!((&m["snps"], &m["tstv"]), (&json!(90280), &json!(1.9632)), "DeepVariant PASS calls");
}

#[test]
fn deepvariant_stats_must_count_pass_calls_only() {
    // Over all records, DeepVariant's RefCall candidates (mostly errors) pull Ti/Tv to 0.86.
    let dir = giraffe_bucket("bcftools.deepvariant.all-records.stats.txt");
    let (code, v, _) = run(dir.path(), "HG003_chr20_giraffe", &[]);
    assert_eq!(code, 1);
    assert_eq!(reasons(&v), ["tstv 0.8564 is below the minimum 1.7"]);
}

fn smoke_config() -> String {
    crate_path("thresholds/smoke.toml").display().to_string()
}

/// A cloud smoke checkpoint as the cloud wrote it, run record included: CLOUD015 (single-end) or CLOUD006
/// (paired-end), 10,000 reads from a 50 kb window of chr20 judged over the whole of chr20.
fn cloud_bucket(sample: &str) -> tempfile::TempDir {
    let record = fs::read_to_string(crate_path(&format!("tests/fixtures/{sample}/provenance.json"))).unwrap();
    bucket(sample, Some(record))
}

#[test]
fn cloud_smoke_tests_pass_the_smoke_profile() {
    let dir = cloud_bucket("CLOUD015");
    let (code, v, stderr) = run(dir.path(), "CLOUD015", &["--config", &smoke_config()]);
    assert_eq!(code, 0, "{stderr}");
    assert_eq!(v["verdict"], "pass");
    assert_eq!(v["layout"], "single");
    let m = &v["metrics"];
    assert_eq!(
        [&m["gc_pct"], &m["mean_depth"], &m["breadth_10x_pct"], &m["duplicate_pct"], &m["snps"], &m["tstv"]],
        [&json!(37.4985), &json!(0.0222), &json!(0.0), &json!(4.78), &json!(10), &json!(0.25)]
    );
    assert_eq!(check(&v, "mean_depth")["status"], "pass");
    assert!(stderr.contains("CLOUD015: PASS (11 checks)"), "{stderr}");

    let dir = cloud_bucket("CLOUD006");
    let (code, v, stderr) = run(dir.path(), "CLOUD006", &["--config", &smoke_config()]);
    assert_eq!(code, 0, "{stderr}");
    assert_eq!(v["layout"], "paired");
    assert_eq!(check(&v, "properly_paired_pct")["value"], 100.0);
    assert_eq!([&v["metrics"]["gc_pct"], &v["metrics"]["mean_depth"]], [&json!(37.2803), &json!(0.0232)]);
}

#[test]
fn cloud_smoke_tests_fail_the_synthetic_profile_on_what_the_smoke_profile_relaxes() {
    // Why `smoke` exists (#157): `synthetic` is calibrated for make_test_data.py's 50 kb random reference.
    let dir = cloud_bucket("CLOUD015");
    let (code, v, _) = run(dir.path(), "CLOUD015", &["--config", &synthetic_config()]);
    assert_eq!(code, 1);
    assert_eq!(
        reasons(&v),
        [
            "gc_pct 37.4985 is below the minimum 45",
            "mean_depth 0.0222 is below the minimum 25",
            "breadth_10x_pct 0 is below the minimum 95"
        ]
    );
}

#[test]
fn the_smoke_profile_fails_real_samples_and_local_synthetic_data() {
    // A profile for one fixed input: a real sample or the local synthetic data uploaded with
    // gate-profile=smoke fails, rather than passing with checks that don't fit it.
    let dir = giab_bucket("HG003_chr20");
    let (code, v, _) = run(dir.path(), "HG003_chr20", &["--config", &smoke_config()]);
    assert_eq!(code, 1);
    assert!(reasons(&v).contains(&"mean_depth 36.7812 is above the maximum 0.03"), "{:?}", reasons(&v));
    assert!(reasons(&v).contains(&"snps 118410 is above the maximum 10"), "{:?}", reasons(&v));

    let (root, sample) = synthetic_bucket("single", "succeeded");
    let (code, v, _) = run(root.path(), sample, &["--config", &smoke_config()]);
    assert_eq!(code, 1);
    assert_eq!(reasons(&v), ["gc_pct 49.5339 is above the maximum 40", "mean_depth 28.545 is above the maximum 0.03"]);
}
