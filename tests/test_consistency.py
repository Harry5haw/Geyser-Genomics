"""Nothing at build time checks that tasks.py, the state machine and the local runner agree. This does."""

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
    # File order: the QC branch, the align branch, then variants (run_local runs them in that order).
    assert state_machine_stages() == run_local.STAGES


def test_variant_shards_and_merge_use_the_planners_window():
    # Scatter-gather (#108): the Map runs 'variants --region', the merge 'variants --window <W>', and the
    # planner 'regions --window <W>' with the same W, or the merge would look for parts that don't exist.
    variants = [extra for stage, _, extra in state_machine_commands() if stage == "variants"]
    assert [re.findall(r"'(--[\w-]+)'", extra) for extra in variants] == [["--region"], ["--window"]]
    windows = re.findall(r"'--window',\s*'([^']+)'", asl_text())
    assert len(windows) == 2 and set(windows) == {"${variant_region_window}"}  # planner and merge
    assert "'regions', $.reference_name, '--window'" in asl_text() and "'--output-key'" in asl_text()


def test_state_machine_is_rendered_from_the_asl_file():
    with open(os.path.join(REPO_ROOT, "infrastructure", "step_functions.tf"), encoding="utf-8") as fh:
        tf = fh.read()
    assert 'templatefile("${path.module}/state_machine.asl.json"' in tf
    assert "States.Array" not in tf  # no second copy of the stage commands


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
