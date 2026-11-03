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


def test_state_machine_matches_local_runner():
    # File order: the QC branch, the align branch, variants, then the quality gate (#144). run_local runs
    # the core stages in that order and the gate as its last job (pipeline_jobs(gate=True)).
    assert state_machine_stages() == run_local.STAGES + [("gate", False)]


def test_variant_shards_and_merge_use_the_planners_window():
    # Scatter-gather (#108): the Map runs 'variants --region', the merge 'variants --window <W>', and the
    # planner 'regions --window <W>' with the same W, or the merge would look for parts that don't exist.
    variants = [extra for stage, _, extra in state_machine_commands() if stage == "variants"]
    assert [re.findall(r"'(--[\w-]+)'", extra) for extra in variants] == [["--region"], ["--window"]]
    windows = re.findall(r"'--window',\s*'([^']+)'", asl_text())
    assert len(windows) == 2 and set(windows) == {"${variant_region_window}"}  # planner and merge
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
    assert set(tasks.TASKS) == {stage for stage, _ in state_machine_stages()}


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


def test_lifecycle_expires_the_apps_intermediate_prefix():
    # #62: parts are written under tasks.INTERMEDIATE_PREFIX; the rule that clears them must match it.
    rules = _lifecycle_prefixes()
    assert rules["expire-intermediate"] == (tasks.INTERMEDIATE_PREFIX, True)
    assert tasks.region_part_key("S1", "chr20:1-100").startswith(tasks.INTERMEDIATE_PREFIX)


def test_lifecycle_never_expires_raw_reads_references_or_outputs():
    # Decided with the user (2 Nov 2026): only disposable prefixes expire. A rule with an expiration must
    # not cover (or sit inside) raw_reads/, reference/ or any output prefix the pipeline writes.
    kept = {"raw_reads/", "reference/"} | {key.split("/")[0] + "/" for key in tasks.keys_for("S1").values()}
    kept |= {key.split("/")[0] + "/" for key in tasks.keys_for("S1", tasks.PAIRED).values()}
    for rule_id, (prefix, expires) in _lifecycle_prefixes().items():
        if not expires:
            continue  # e.g. aborting unfinished multipart uploads touches no stored object
        assert prefix, f"{rule_id} expires the whole bucket"
        assert not any(k.startswith(prefix) or prefix.startswith(k) for k in kept), (rule_id, prefix)


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
    assert prefixes and prefixes == {p.rstrip("-") for p, _ in provenance.JOB_NAME_STAGES}, prefixes


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
