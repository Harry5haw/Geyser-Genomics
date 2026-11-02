//! geyser-gate: exit 0 if the sample passes the quality gate, 1 if it fails, 2 on a usage or config error.

use std::fs;
use std::path::PathBuf;
use std::process::ExitCode;

use clap::Parser;

use geyser_gate::config::{Config, DEFAULT_TOML};
use geyser_gate::gate::{Status, evaluate};
use geyser_gate::inputs::Inputs;
use geyser_gate::valid_sample_id;

const EXIT_FAIL: u8 = 1;
const EXIT_ERROR: u8 = 2; // clap's own exit code for usage errors

#[derive(Parser)]
#[command(
    name = "geyser-gate",
    version,
    about = "Check a sample's QC statistics (stats/{id}/) and run record (runs/{id}.provenance.json) against thresholds",
    after_help = "Exit status: 0 pass, 1 fail (the verdict's \"reasons\" say why), 2 usage or config error."
)]
struct Cli {
    /// Sample ID, as in raw_reads/<id>.fastq.gz
    #[arg(long, required_unless_present = "print_default_config")]
    sample: Option<String>,

    /// Folder laid out like the bucket: holds stats/ and runs/ (e.g. local_bucket)
    #[arg(long, default_value = ".")]
    root: PathBuf,

    /// Thresholds (TOML). Default: the built-in thresholds/default.toml (human germline WGS, ~30x)
    #[arg(long)]
    config: Option<PathBuf>,

    /// Write the verdict JSON to this file instead of stdout
    #[arg(long, short)]
    output: Option<PathBuf>,

    /// No summary on stderr
    #[arg(long, short)]
    quiet: bool,

    /// Print the built-in thresholds and exit
    #[arg(long)]
    print_default_config: bool,
}

fn error(message: impl std::fmt::Display) -> ExitCode {
    eprintln!("geyser-gate: error: {message}");
    ExitCode::from(EXIT_ERROR)
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    if cli.print_default_config {
        print!("{DEFAULT_TOML}");
        return ExitCode::SUCCESS;
    }
    let sample = cli.sample.expect("clap requires --sample");
    if !valid_sample_id(&sample) {
        return error(format!("invalid sample ID {sample:?}"));
    }
    if !cli.root.is_dir() {
        return error(format!("--root {} is not a folder", cli.root.display()));
    }
    let (config, source) = match &cli.config {
        None => (Config::default_config(), "built-in default".to_string()),
        Some(path) => match fs::read_to_string(path).map_err(|e| e.to_string()).and_then(|t| Config::parse(&t)) {
            Ok(config) => (config, path.display().to_string()),
            Err(e) => return error(format!("config {}: {e}", path.display())),
        },
    };

    let verdict = evaluate(&config, &source, &Inputs::load(&cli.root, &sample));
    let json = serde_json::to_string_pretty(&verdict).expect("the verdict serialises") + "\n";
    match &cli.output {
        Some(path) => {
            if let Err(e) = fs::write(path, &json) {
                return error(format!("could not write {}: {e}", path.display()));
            }
        }
        None => print!("{json}"),
    }

    if !cli.quiet {
        for check in &verdict.checks {
            let mark = match check.status {
                Status::Pass => "pass",
                Status::Fail => "FAIL",
                Status::Skipped => "skip",
                Status::NotApplicable => "n/a ",
            };
            let value = match &check.value {
                None => "-".to_string(),
                Some(serde_json::Value::String(s)) => s.clone(),
                Some(v) => v.to_string(),
            };
            let reason = check.reason.as_deref().map(|r| format!("  ({r})")).unwrap_or_default();
            eprintln!("  {mark}  {:<20} {value}{reason}", check.name);
        }
        let n_failed = verdict.reasons.len();
        match n_failed {
            0 => eprintln!("geyser-gate: {sample}: PASS ({} checks)", verdict.checks.len()),
            _ => eprintln!("geyser-gate: {sample}: FAIL ({n_failed} of {} checks)", verdict.checks.len()),
        }
    }
    if verdict.passed() { ExitCode::SUCCESS } else { ExitCode::from(EXIT_FAIL) }
}
