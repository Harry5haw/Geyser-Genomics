"""run_local.py: caller selection, docker commands and truth checks (no Docker needed)."""

import gzip
import json
import os

import run_local


def names(jobs):
    return [j["name"] for j in jobs]


def test_default_caller_runs_exactly_the_state_machine_stages():
    jobs = run_local.pipeline_jobs("S1", "ref.fa")
    assert [j["cmd"] for j in jobs] == [
        run_local.stage_command(stage, "S1", "ref.fa", needs_ref) for stage, needs_ref in run_local.STAGES
    ]
    assert all(j["image"] == "geyser-app" and not j["gpus"] for j in jobs)


def test_deepvariant_caller_replaces_variants_stage_and_uses_gpu():
    jobs = run_local.pipeline_jobs("S1", "ref.fa", caller="deepvariant")
    assert names(jobs) == ["qc", "align", "deepvariant"]
    dv = jobs[-1]
    assert dv["image"] == "geyser-deepvariant" and dv["gpus"]
    assert dv["cmd"] == ["/opt/geyser-venv/bin/python", "deepvariant_task.py", "S1", "ref.fa"]


def test_both_callers_run_bcftools_then_deepvariant():
    assert names(run_local.pipeline_jobs("S1", "ref.fa", caller="both")) == ["qc", "align", "variants", "deepvariant"]


def test_skip_to_variants_starts_at_variant_calling():
    assert names(run_local.pipeline_jobs("S1", "r", caller="deepvariant", skip_to_variants=True)) == ["deepvariant"]
    assert names(run_local.pipeline_jobs("S1", "r", caller="both", skip_to_variants=True)) == [
        "variants",
        "deepvariant",
    ]


def test_docker_command_adds_gpus_only_when_asked():
    with_gpu = run_local.docker_command(["x"], "root", "img", gpus=True)
    without = run_local.docker_command(["x"], "root", "img")
    assert with_gpu[:5] == ["docker", "run", "--rm", "--gpus", "all"]
    assert "--gpus" not in without
    assert without[-2:] == ["img", "x"]
    assert "STORAGE_BACKEND=local" in without and "LOCAL_STORAGE_DIR=/data" in without


def test_memory_cap_only_when_given():
    capped = run_local.docker_command(["x"], "root", "img", memory="8g")
    assert capped[3:7] == ["--memory", "8g", "--memory-swap", "8g"]
    assert "--memory" not in run_local.docker_command(["x"], "root", "img")


def test_memory_cap_applies_to_deepvariant_only(monkeypatch):
    seen = []
    monkeypatch.setattr(run_local.subprocess, "run", lambda cmd: seen.append(cmd) or type("R", (), {"returncode": 0}))
    for job in run_local.pipeline_jobs("S1", "r", caller="both", skip_to_variants=True):
        run_local.run_job(job, "docker", "root", dv_memory="8g")
    assert ["--memory" in cmd for cmd in seen] == [False, True]


def test_aligner_choice_reaches_the_stage_containers(monkeypatch):
    seen = []
    monkeypatch.setattr(run_local.subprocess, "run", lambda cmd: seen.append(cmd) or type("R", (), {"returncode": 0}))
    monkeypatch.setenv("ALIGNER", "bwa")
    for job in run_local.pipeline_jobs("S1", "r"):
        run_local.run_job(job, "docker", "root")
    assert all("ALIGNER=bwa" in cmd for cmd in seen) and len(seen) == len(run_local.STAGES)

    seen.clear()
    monkeypatch.delenv("ALIGNER")
    run_local.run_job(run_local.pipeline_jobs("S1", "r")[0], "docker", "root")
    assert not any(arg.startswith("ALIGNER=") for arg in seen[0])  # unset: the image default (bwa-mem2)


def test_no_gpu_flag_reaches_the_job():
    jobs = run_local.pipeline_jobs("S1", "r", caller="deepvariant", gpus=False)
    assert not jobs[-1]["gpus"]


def write_vcf(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS\n"
    body += "".join(f"c\t{pos}\t.\t{ref}\t{alt}\t50\t{flt}\t.\tGT\t1/1\n" for pos, ref, alt, flt in rows)
    path.write_bytes(gzip.compress(body.encode()))


def test_read_vcf_calls_skips_refcall_and_placeholder_alts(tmp_path):
    vcf = tmp_path / "a.vcf.gz"
    write_vcf(vcf, [(10, "A", "G", "PASS"), (20, "C", "T", "RefCall"), (30, "G", "<*>", "."), (40, "T", "A,C", ".")])
    assert run_local.read_vcf_calls(vcf) == {(10, "A", "G"), (40, "T", "A"), (40, "T", "C")}


def test_check_against_truth_per_caller(tmp_path):
    (tmp_path / "truth").mkdir()
    (tmp_path / "truth" / "S1.json").write_text(json.dumps({"snps": [{"pos": 10, "ref": "A", "alt": "G"}]}))
    write_vcf(tmp_path / "variants" / "S1.vcf.gz", [(10, "A", "G", ".")])
    write_vcf(tmp_path / "variants_deepvariant" / "S1.vcf.gz", [(10, "A", "G", "PASS"), (99, "C", "T", "PASS")])
    assert run_local.check_against_truth(str(tmp_path), "S1", "bcftools")
    assert not run_local.check_against_truth(str(tmp_path), "S1", "deepvariant")  # one extra call


def test_giab_truth_is_counted_inside_bed_only(tmp_path):
    write_vcf(
        tmp_path / "truth" / "S1.vcf.gz", [(150, "A", "G", "PASS"), (160, "C", "T", "PASS"), (999, "G", "A", "PASS")]
    )
    (tmp_path / "truth" / "S1.bed").write_text("c\t100\t200\n")
    write_vcf(
        tmp_path / "variants_deepvariant" / "S1.vcf.gz",
        [(150, "A", "G", "PASS"), (170, "T", "C", "PASS"), (999, "G", "A", "PASS")],
    )
    counts = run_local.truth_counts(str(tmp_path), "S1", "deepvariant")
    assert (counts["kind"], counts["truth"], counts["tp"]) == ("giab", 2, 1)
    assert len(counts["missed"]) == 1 and len(counts["extra"]) == 1
    assert run_local.check_against_truth(str(tmp_path), "S1", "deepvariant")  # report only


def test_bed_boundaries_are_half_open_across_many_intervals(tmp_path):
    vcf = tmp_path / "a.vcf.gz"
    # BED (100, 200] and (300, 400], given out of order
    write_vcf(vcf, [(p, "A", "G", "PASS") for p in (100, 101, 200, 201, 300, 301, 400, 401, 50)])
    assert {c[0] for c in run_local.read_vcf_calls(vcf, [(300, 400), (100, 200)])} == {101, 200, 301, 400}


def test_missing_truth_fails_the_check(tmp_path):
    assert not run_local.check_against_truth(str(tmp_path), "S1")


def test_every_job_of_a_run_gets_the_same_run_name(monkeypatch):
    seen = []
    monkeypatch.setattr(run_local.subprocess, "run", lambda cmd: seen.append(cmd) or type("R", (), {"returncode": 0}))
    env = run_local.run_env("S1", "geyser-app")
    for job in run_local.pipeline_jobs("S1", "r"):
        run_local.run_job(job, "docker", "root", env=env)
    names = {arg for cmd in seen for arg in cmd if arg.startswith("RUN_NAME=")}
    assert names == {f"RUN_NAME={env['RUN_NAME']}"} and env["RUN_NAME"].startswith("S1-")
    assert all("APP_VERSION=local:geyser-app" in cmd for cmd in seen)


# --- quality gate (#144) ---------------------------------------------------------------------------


def test_the_gate_runs_after_variant_calling_when_asked():
    jobs = run_local.pipeline_jobs("S1", "r.fa", gate=True)
    assert [j["name"] for j in jobs] == ["qc", "align", "variants", "gate"]
    assert jobs[-1]["cmd"] == ["python", "tasks.py", "gate", "S1"] and jobs[-1]["image"] == "geyser-app"
    assert "env" not in jobs[-1], "no gate_config: the host's GATE_CONFIG or the built-in thresholds"


def test_the_gate_follows_the_merge_of_a_scattered_call_and_precedes_deepvariant():
    jobs = run_local.pipeline_jobs("S1", "r.fa", "both", regions=["c:1-10"], shard_window=10, gate=True)
    assert [j["name"] for j in jobs] == ["qc", "align", "variants-shard", "variants", "gate", "deepvariant"]


def test_no_gate_without_a_bcftools_call_set():
    # It reads bcftools stats: DeepVariant-only runs have none yet.
    jobs = run_local.pipeline_jobs("S1", "r.fa", "deepvariant", gate=True)
    assert "gate" not in [j["name"] for j in jobs]


def test_pipeline_jobs_default_is_still_the_state_machine():
    assert "gate" not in [j["name"] for j in run_local.pipeline_jobs("S1", "r.fa")]


def test_synthetic_samples_get_the_synthetic_thresholds(tmp_path, monkeypatch):
    monkeypatch.delenv("GATE_CONFIG", raising=False)
    assert run_local.gate_config_for(str(tmp_path), "S1", "docker") is None, "real data: built-in thresholds"
    (tmp_path / "truth").mkdir()
    (tmp_path / "truth" / "S1.json").write_text("{}")
    assert run_local.gate_config_for(str(tmp_path), "S1", "docker") == "/opt/geyser-gate/thresholds/synthetic.toml"
    assert run_local.gate_config_for(str(tmp_path), "S1", "host").endswith(
        os.path.join("gate", "thresholds", "synthetic.toml")
    )
    monkeypatch.setenv("GATE_CONFIG", "/my/own.toml")
    assert run_local.gate_config_for(str(tmp_path), "S1", "docker") is None, "the host's choice is passed through"


def test_gate_settings_reach_the_gate_container(monkeypatch):
    seen = []
    monkeypatch.setattr(run_local.subprocess, "run", lambda cmd: seen.append(cmd) or type("R", (), {"returncode": 0}))
    monkeypatch.setenv("GATE_MODE", "fail")
    (job,) = [j for j in run_local.pipeline_jobs("S1", "r", gate=True, gate_config="/c.toml") if j["name"] == "gate"]
    run_local.run_job(job, "docker", "root", env=run_local.run_env("S1", "geyser-app"))
    assert "GATE_MODE=fail" in seen[0] and "GATE_CONFIG=/c.toml" in seen[0]


def test_main_runs_the_gate_unless_no_gate(tmp_path, monkeypatch):
    (tmp_path / "raw_reads").mkdir()
    (tmp_path / "raw_reads" / "S1.fastq.gz").write_bytes(b"")
    ran = []
    monkeypatch.setattr(run_local, "run_job", lambda job, *a, **k: ran.append(job["name"]) or 0)
    assert run_local.main(["--root", str(tmp_path), "--sample", "S1", "--reference", "r.fa"]) == 0
    assert ran == ["qc", "align", "variants", "gate"]
    ran.clear()
    assert run_local.main(["--root", str(tmp_path), "--sample", "S1", "--reference", "r.fa", "--no-gate"]) == 0
    assert ran == ["qc", "align", "variants"]
