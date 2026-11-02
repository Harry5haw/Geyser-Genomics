"""Nothing at build time checks that tasks.py, the state machine and the local runner agree. This does."""

import os
import re

import run_local
import tasks
from conftest import REPO_ROOT

ARRAY_RE = re.compile(r"States\.Array\('python',\s*'tasks\.py',\s*'(\w+)',\s*\$\.srr_id(,\s*\$\.reference_name)?\)")


def state_machine_stages():
    with open(os.path.join(REPO_ROOT, "infrastructure", "state_machine.asl.json"), encoding="utf-8") as fh:
        return [(m.group(1), bool(m.group(2))) for m in ARRAY_RE.finditer(fh.read())]


def test_state_machine_matches_local_runner():
    # File order: the QC branch, the align branch, then variants (run_local runs them in that order).
    assert state_machine_stages() == run_local.STAGES


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
