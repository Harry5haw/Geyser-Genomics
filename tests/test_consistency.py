"""Nothing at build time checks that tasks.py, the state machine and the local runner agree. This does."""

import fnmatch
import glob
import json
import os
import re

import run_local
import tasks
from conftest import REPO_ROOT

# A stage command: tasks.py <stage> $.srr_id [$.reference_name] [extra flags, e.g. '--region', $.region].
ARRAY_RE = re.compile(
    r"States\.Array\('python',\s*'tasks\.py',\s*'(\w+)',\s*\$\.srr_id(,\s*\$\.reference_name)?(?P<extra>(,\s*'--[\w-]+',\s*[^,)]+)*)\)"
)


def asl_text():
    with open(os.path.join(REPO_ROOT, "infrastructure", "state_machine.asl.json"), encoding="utf-8") as fh:
        return fh.read()


def state_machine_commands():
    """[(stage, needs_reference, extra flags)] in file order."""
    return [(m.group(1), bool(m.group(2)), m.group("extra")) for m in ARRAY_RE.finditer(asl_text())]


def state_machine_stages():
    """The distinct stages in file order (the variant shards and their merge are both 'variants')."""
    stages = []
    for stage, needs_ref, _ in state_machine_commands():
        if (stage, needs_ref) not in stages:
            stages.append((stage, needs_ref))
    return stages


# v1.3's large route (docs/design/genome-scale-app.md): the app half adds these stages and job names, the infra
# half the states that run them. Either can merge first, so the checks below require them in the state machine
# only once its large route exists, and then all of them.
LARGE_ROUTE = {"preflight": False, "align_chunk": True, "align_merge": True}
LARGE_ROUTE_JOBS = {"Preflight", "AlignChunk", "AlignMerge"}


def large_route_in_state_machine() -> bool:
    return any(stage in LARGE_ROUTE for stage, _, _ in state_machine_commands())


def test_state_machine_matches_local_runner():
    # File order: the QC branch, the align branch, variants, then the quality gate (#144). run_local runs
    # the core stages in that order and the gate as its last job (pipeline_jobs(gate=True)). The large route's
    # stages (an alternative to align) are checked on their own.
    stages = state_machine_stages()
    assert [s for s in stages if s[0] not in LARGE_ROUTE] == run_local.STAGES + [("gate", False)]
    if large_route_in_state_machine():
        assert {s for s in stages if s[0] in LARGE_ROUTE} == set(LARGE_ROUTE.items())


def test_variant_shards_and_merge_use_the_planners_window():
    # Scatter-gather (#108): the Map runs 'variants --region', the merge 'variants --window <W>', and the
    # planner 'regions --window <W>' with the same W, or the merge would look for parts that don't exist.
    variants = [extra for stage, _, extra in state_machine_commands() if stage == "variants"]
    assert [re.findall(r"'(--[\w-]+)'", extra) for extra in variants] == [["--region"], ["--window"]]
    # Both read the run's window (Set_Run_Settings: the Terraform default, or the run's override), never a
    # template value of their own.
    windows = re.findall(r"'--window',\s*([^,)]+)", asl_text())
    assert len(windows) == 2 and set(windows) == {"$.run_settings.variant_region_window"}  # planner and merge
    assert "'regions', $.reference_name, '--window'" in asl_text() and "'--output-key'" in asl_text()
    # The planner's regions file is disposable too: under the prefix #62's lifecycle rule expires.
    keys = re.findall(r"States\.Format\('([^']*regions[^']*)'", asl_text())
    assert len(keys) == 2 and len(set(keys)) == 1 and keys[0].startswith(tasks.INTERMEDIATE_PREFIX), keys


def test_state_machine_is_rendered_from_the_asl_file():
    with open(os.path.join(REPO_ROOT, "infrastructure", "step_functions.tf"), encoding="utf-8") as fh:
        tf = fh.read()
    assert 'templatefile("${path.module}/state_machine.asl.json"' in tf
    assert "States.Array" not in tf  # no second copy of the stage commands


def test_the_state_machine_runs_every_task():
    # The reverse of test_state_machine_stages_exist_in_tasks: a new tasks.py stage needs its state too
    # (the repo rule: tasks, state machine and run_local change together). The gate's arrived with #144.
    expected = set(tasks.TASKS) - (set() if large_route_in_state_machine() else set(LARGE_ROUTE))
    assert expected == {stage for stage, _ in state_machine_stages()}


def test_state_machine_stages_exist_in_tasks():
    for stage, needs_ref in state_machine_stages():
        assert stage in tasks.TASKS
        assert tasks.TASKS[stage][1] == needs_ref, f"reference argument mismatch for '{stage}'"


def test_trigger_and_tasks_agree_on_raw_key_names(trigger):
    # The Lambda zip can't import tasks.py, so the _R1/_R2 convention lives in both. Keep them in step.
    assert trigger.MATES == tasks.MATES
    single = tasks.keys_for("S1")
    paired = tasks.keys_for("S1", "paired")
    assert trigger.parse_raw_key(single["raw"]) == ("S1", None)
    for mate in tasks.MATES:
        assert trigger.parse_raw_key(paired[f"raw_{mate}"]) == ("S1", mate)
    assert trigger.mate_key(paired["raw_R1"]) == paired["raw_R2"]


def test_dashboard_log_links_match_the_terraform_log_groups():
    # scripts/monitoring.py names the Batch and trigger-Lambda log groups that resilience.tf creates.
    import monitoring

    with open(os.path.join(REPO_ROOT, "infrastructure", "resilience.tf"), encoding="utf-8") as fh:
        tf = fh.read()
    for prefix, constant in (
        ("/aws/batch/", monitoring.BATCH_LOG_GROUP),
        ("/aws/lambda/", monitoring.TRIGGER_LOG_GROUP),
    ):
        template = re.search(r'name\s*=\s*"(' + re.escape(prefix) + r'[^"]+)"', tf).group(1)
        rendered = template.replace("${var.project_name}", "geyser").replace("${var.environment}", "dev")
        assert rendered == constant


def test_quality_gate_reads_the_keys_the_stages_write():
    # gate/ (Rust) hardcodes the stats and run record keys; a rename in tasks.py or provenance.py must reach it.
    import provenance

    # gate/src/inputs/: mod.rs has the keys, run_record.rs the schema; read them all, wherever they live.
    inputs_dir = os.path.join(REPO_ROOT, "gate", "src", "inputs")
    rs = ""
    for path in sorted(glob.glob(os.path.join(inputs_dir, "**", "*.rs"), recursive=True)):
        with open(path, encoding="utf-8") as fh:
            rs += fh.read()
    gate_keys = {
        k.replace("{sample_id}", "S1") for k in re.findall(r'format!\("((?:qc_reports|stats|runs)/[^"]+)"', rs)
    }
    keys = tasks.keys_for("S1")
    expected = {keys[k] for k in ("qc_json", "flagstat", "mosdepth_summary", "mosdepth_dist", "bcftools_stats")}
    expected |= {provenance.record_key("S1"), provenance.part_prefix("S1")}
    assert gate_keys == expected
    # The gate reads one run record schema; a schema bump in provenance.py must reach the gate.
    gate_schema = re.search(r"pub const RUN_RECORD_SCHEMA: u64 = (\d+);", rs)
    assert gate_schema, "no RUN_RECORD_SCHEMA in gate/src/inputs/ (run_record.rs)"
    assert int(gate_schema.group(1)) == provenance.SCHEMA_VERSION


def _lifecycle_prefixes():
    """{rule id: prefix} from infrastructure/storage_lifecycle.tf (an empty filter = the whole bucket)."""
    with open(os.path.join(REPO_ROOT, "infrastructure", "storage_lifecycle.tf"), encoding="utf-8") as fh:
        tf = fh.read()
    rules = {}
    for block in tf.split("  rule {")[1:]:
        rule_id = re.search(r'id\s*=\s*"([^"]+)"', block).group(1)
        prefix = re.search(r'prefix\s*=\s*"([^"]*)"', block)
        rules[rule_id] = (prefix.group(1) if prefix else "", "expiration {" in block)
    return rules


def test_raw_reads_tier_only_large_objects_never_expire_and_match_the_route():
    # v1.3 (the user's decision, 4 Nov 2026): only raw_reads/ objects of at least large_input_gb move to Glacier
    # Instant Retrieval, after raw_reads_tier_days (30); smaller ones stay in Standard; none ever expire. The size
    # is the large route's threshold: S3's object_size_greater_than is strict, so the filter is one byte under it.
    with open(os.path.join(REPO_ROOT, "infrastructure", "storage_lifecycle.tf"), encoding="utf-8") as fh:
        tf = fh.read()
    (block,) = [b for b in tf.split("  rule {")[1:] if 'id     = "tier-raw-reads"' in b or '"tier-raw-reads"' in b]
    assert re.search(r'and\s*\{[^}]*prefix\s*=\s*"raw_reads/"', block)
    assert re.search(r"object_size_greater_than\s*=\s*local\.large_input_bytes\s*-\s*1\b", block)
    assert re.search(r'storage_class\s*=\s*"GLACIER_IR"', block) and "max(var.raw_reads_tier_days, 1)" in block
    assert "expiration" not in block and "noncurrent" not in block
    with open(os.path.join(REPO_ROOT, "infrastructure", "step_functions.tf"), encoding="utf-8") as fh:
        assert re.search(r"large_input_bytes\s*=\s*floor\(var\.large_input_gb \* 1000000000\)", fh.read())
    with open(os.path.join(REPO_ROOT, "infrastructure", "variables.tf"), encoding="utf-8") as fh:
        variables_tf = fh.read()
    for name, default in (("large_input_gb", 5), ("raw_reads_tier_days", 30)):
        block = variables_tf[variables_tf.index(f'variable "{name}"') :]
        assert int(re.search(r"default\s*=\s*(\d+)", block).group(1)) == default, name


def test_lifecycle_expires_the_apps_intermediate_prefix():
    # #62: parts are written under tasks.INTERMEDIATE_PREFIX; the rule that clears them must match it.
    rules = _lifecycle_prefixes()
    assert rules["expire-intermediate"] == (tasks.INTERMEDIATE_PREFIX, True)
    assert tasks.region_part_key("S1", "chr20:1-100").startswith(tasks.INTERMEDIATE_PREFIX)


def _alignment_prefix():
    """The one prefix every alignment form (and its index) is written under."""
    keys = tasks.keys_for("S1")
    (prefix,) = {keys[name].split("/")[0] + "/" for name in (*tasks.ALIGNMENT_FORMS, "crai")}
    return prefix


def test_lifecycle_never_expires_raw_reads_references_or_outputs():
    # Decided with the user (2 Nov 2026): only disposable prefixes expire. A rule with an expiration must
    # not cover (or sit inside) raw_reads/, reference/ or any output prefix the pipeline writes, except
    # alignments/, which expires at its break-even (recomputable from the raw reads; the test below).
    kept = {"raw_reads/", "reference/"} | {key.split("/")[0] + "/" for key in tasks.keys_for("S1").values()}
    kept |= {key.split("/")[0] + "/" for key in tasks.keys_for("S1", tasks.PAIRED).values()}
    kept.discard(_alignment_prefix())
    for rule_id, (prefix, expires) in _lifecycle_prefixes().items():
        if not expires:
            continue  # e.g. aborting unfinished multipart uploads touches no stored object
        assert prefix, f"{rule_id} expires the whole bucket"
        assert not any(k.startswith(prefix) or prefix.startswith(k) for k in kept), (rule_id, prefix)


def test_every_batch_job_carries_a_stage_tag():
    # #64: Cost Explorer splits Batch cost by the Stage tag (propagated to the Fargate task), so every
    # submitJob in the state machine, including any added later (shards), must set one.
    with open(os.path.join(REPO_ROOT, "infrastructure", "state_machine.asl.json"), encoding="utf-8") as fh:
        asl = fh.read()
    submits = asl.count('"arn:aws:states:::batch:submitJob.sync"')
    stages = re.findall(r'"Tags":\s*\{\s*"Stage":\s*"(\w+)"\s*\}', asl)
    assert submits and len(stages) == submits, (submits, stages)
    assert {"qc", "align", "variants"} <= set(stages)
    # Every job definition, in any file, hands the job's tags to its task (Fargate or ECS on EC2).
    import glob

    for path in glob.glob(os.path.join(REPO_ROOT, "infrastructure", "*.tf")):
        with open(path, encoding="utf-8") as fh:
            tf = fh.read()
        for block in re.split(r'(?m)^resource "', tf)[1:]:
            if block.startswith('aws_batch_job_definition"'):
                assert re.search(r"(?m)^  propagate_tags\s*=\s*true", block), (path, block.splitlines()[0])


def test_queue_wait_widgets_read_the_terraform_event_log_group():
    # #138: batch_wait.tf sends Batch RUNNING events to this group; the dashboard queries it.
    import monitoring

    with open(os.path.join(REPO_ROOT, "infrastructure", "batch_wait.tf"), encoding="utf-8") as fh:
        tf = fh.read()
    template = re.search(r'name\s*=\s*"(/aws/events/[^"]+)"', tf).group(1)
    rendered = template.replace("${var.project_name}", "geyser").replace("${var.environment}", "dev")
    assert rendered == monitoring.BATCH_EVENTS_LOG_GROUP


def test_failure_parsing_knows_every_job_name_the_state_machine_sets():
    # provenance.parse_failure names the failing stage from the Batch job name in a Catch's Cause, so every
    # JobName prefix the state machine builds must map to a stage, and none may be stale.
    import provenance

    prefixes = set(re.findall(r"States\.Format\('(\w+)-\{\}", asl_text()))
    known = {p.rstrip("-") for p, _ in provenance.JOB_NAME_STAGES}
    if not large_route_in_state_machine():
        known -= LARGE_ROUTE_JOBS
    assert prefixes and prefixes == known, prefixes


def test_gate_profiles_agree_across_the_state_machine_run_local_and_the_thresholds():
    # Check_Gate_Profile's allowed values, run_local's --gate-profile choices and gate/thresholds/*.toml
    # (the files the image ships to $GATE_THRESHOLDS_DIR) name the same profiles (#157).
    asl = asl_text()
    start = asl.index('"Check_Gate_Profile": {')  # the state, not StartAt
    choice = asl[start : asl.index('"Default_Gate_Profile": {', start)]
    in_state_machine = set(re.findall(r'"StringEquals": "(\w+)"', choice))
    shipped = {
        f[: -len(".toml")] for f in os.listdir(os.path.join(REPO_ROOT, "gate", "thresholds")) if f.endswith(".toml")
    }
    assert in_state_machine == set(run_local.GATE_PROFILES) == shipped


def test_the_state_machines_aligners_are_the_apps():
    # Choose_Aligner (#146): every aligner it routes is one tasks.py knows, a run without one gets the app's
    # default (bwa-mem2, in geyser-app), and giraffe goes to the job definition whose image sets ALIGNER=giraffe.
    asl = asl_text()
    start = asl.index('"Choose_Aligner": {')
    choice = asl[start : asl.index('"Prepare_Giraffe_Command": {', start)]
    routes = dict(re.findall(r'"StringEquals": "([\w-]+)", "Next": "(\w+)"', choice))
    assert routes == {tasks.DEFAULT_ALIGNER: "Prepare_Align_Command", "giraffe": "Prepare_Giraffe_Command"}
    assert set(routes) <= set(tasks.ALIGNERS)
    with open(os.path.join(REPO_ROOT, "app-giraffe", "Dockerfile"), encoding="utf-8") as fh:
        assert re.search(r"^ENV ALIGNER=giraffe\b", fh.read(), re.M)


def test_the_giraffe_job_definition_uses_the_apps_kmc_default():
    # variables.tf giraffe_kmc_memory_gb (the cloud's KMC_MEMORY_GB) starts from tasks.py's default.
    with open(os.path.join(REPO_ROOT, "infrastructure", "variables.tf"), encoding="utf-8") as fh:
        tf = fh.read()
    block = tf[tf.index('variable "giraffe_kmc_memory_gb"') :]
    (default,) = re.findall(r"default\s*=\s*(\d+)", block[: block.index("validation")])
    assert int(default) == tasks.DEFAULT_KMC_MEMORY_GB


def _choice_matches(rule, value):
    """Evaluate one Choice rule on $.reference_name = value: only the operators Check_Reference_Name uses."""
    if "And" in rule:
        return all(_choice_matches(r, value) for r in rule["And"])
    if "Not" in rule:
        return not _choice_matches(rule["Not"], value)
    assert rule["Variable"] == "$.reference_name", rule
    if "IsPresent" in rule:
        return (value is not _ABSENT) == rule["IsPresent"]
    if "IsString" in rule:
        return isinstance(value, str) == rule["IsString"]
    if not isinstance(value, str):  # string comparisons are false on other types
        return False
    if "StringEquals" in rule:
        return value == rule["StringEquals"]
    if "StringMatches" in rule:
        return fnmatch.fnmatchcase(value, rule["StringMatches"])
    raise AssertionError(f"Check_Reference_Name uses an operator this test doesn't know: {rule}")


_ABSENT = object()


def test_reference_rules_agree_across_the_state_machine_and_run_local(tmp_path):
    # Check_Reference_Name (which names may be looked up) and run_local.reference_problem refuse the same
    # names, and Check_Reference looks where tasks.py reads the FASTA (tasks.REFERENCE_PREFIX).
    states = json.loads(re.sub(r"\$\{\w+\}", "0", asl_text()))["States"]
    choice = states["Check_Reference_Name"]
    (lookup,) = [c for c in choice["Choices"] if c["Next"] == "Check_Reference"]
    (reference_dir := tmp_path / "reference").mkdir()
    for value in ["chr20.fa", "GRCh38 v2.fa", "", "sub/r.fa", "/r.fa", "r.fa/", 42, None, ["chr20.fa"], {"a": 1}]:
        if isinstance(value, str) and value and "/" not in value:
            (reference_dir / value).write_text(">r\nA\n")
        assert _choice_matches(lookup, value) == (run_local.reference_problem(str(tmp_path), value) is None), value
    assert _choice_matches(choice["Choices"][0], _ABSENT)  # a missing name has its own message
    check = states["Check_Reference"]
    assert check["Resource"] == "arn:aws:states:::aws-sdk:s3:headObject"
    assert check["Parameters"]["Key.$"] == f"States.Format('{tasks.REFERENCE_PREFIX}{{}}', $.reference_name)"


def test_run_overrides_agree_with_their_checks_and_terraform():
    # Run_Setting_Rules (per-run overrides): setting_count is what the unknown-key check counts, so it must be
    # the number of defaults; Set_Run_Settings hands on exactly those keys; the window bounds in the template
    # are step_functions.tf's locals, and variables.tf's default window is inside them.
    states = json.loads(re.sub(r"\$\{\w+\}", "0", asl_text()))["States"]
    rules = states["Run_Setting_Rules"]["Result"]
    # Seven since v1.3: the size and window (#192), and the genome's parallelism (chunk_count, align_concurrency,
    # calling_concurrency, merge_threads).
    assert rules["setting_count"] == len(rules["defaults"]) == 7
    assert set(states["Set_Run_Settings"]["Parameters"]) == {f"{k}.$" for k in rules["defaults"]}
    with open(os.path.join(REPO_ROOT, "infrastructure", "step_functions.tf"), encoding="utf-8") as fh:
        sfn_tf = fh.read()
    with open(os.path.join(REPO_ROOT, "infrastructure", "variables.tf"), encoding="utf-8") as fh:
        variables_tf = fh.read()
    low = int(re.search(r"variant_region_window_min\s*=\s*(\d+)", sfn_tf).group(1))
    high = int(re.search(r"variant_region_window_max\s*=\s*(\d+)", sfn_tf).group(1))
    block = variables_tf[variables_tf.index('variable "variant_region_window"') :]
    default = int(re.search(r"default\s*=\s*(\d+)", block).group(1))
    assert low <= default <= high
    for name in ("align_sizes", "variant_region_window_min", "variant_region_window_max"):
        assert f"${{{name}}}" in asl_text() and re.search(rf"\b{name}\s*=", sfn_tf), name
    # The parallelism maxima are derived in step_functions.tf (from the Fargate Spot cap) and rendered in.
    for name in ("align_concurrency_max", "calling_concurrency_max", "merge_threads_max", "large_input_bytes"):
        assert f"${{{name}}}" in asl_text() and re.search(rf"\b{name}\s*=\s*\S", sfn_tf), name
    assert "fargate_spot_max_vcpus" in sfn_tf.split("align_concurrency_max")[1].split("\n")[0]


def test_alignments_expire_by_break_even_and_nothing_else_shares_the_prefix():
    # The alignments rule covers exactly the CRAM, its index and an old BAM, and only those: no other key
    # the pipeline writes (a VCF, a report) lives under the same prefix.
    prefix = _alignment_prefix()
    assert _lifecycle_prefixes()["expire-alignments"] == (prefix, True)
    alignment_keys = {"cram", "crai", "bam"}
    for layout in (None, tasks.PAIRED):
        keys = tasks.keys_for("S1", layout) if layout else tasks.keys_for("S1")
        assert {name for name, key in keys.items() if key.startswith(prefix)} == alignment_keys
    with open(os.path.join(REPO_ROOT, "infrastructure", "variables.tf"), encoding="utf-8") as fh:
        tf = fh.read()
    block = tf[tf.index('variable "alignment_expiry_days"') :]
    assert re.search(r"default\s*=\s*180\b", block[: block.index("validation")])
