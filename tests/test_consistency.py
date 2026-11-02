"""Nothing at build time checks that tasks.py, the state machine and the local runner agree. This does."""
import os
import re

import run_local
import tasks
from conftest import REPO_ROOT

ARRAY_RE = re.compile(
    r"States\.Array\('python',\s*'tasks\.py',\s*'(\w+)',\s*\$\.srr_id(,\s*\$\.reference_name)?\)"
)


def state_machine_stages():
    with open(os.path.join(REPO_ROOT, "infrastructure", "step_functions.tf"), encoding="utf-8") as fh:
        return [(m.group(1), bool(m.group(2))) for m in ARRAY_RE.finditer(fh.read())]


# Stages the deployed state machine may still call after they stopped being real stages. Each is a
# no-op in tasks.TASKS so a new image works before the state machine is updated; remove it from
# both once infrastructure/step_functions.tf no longer calls it.
RETIRED_STAGES = {"decompress"}


def test_state_machine_matches_local_runner():
    assert [s for s in state_machine_stages() if s[0] not in RETIRED_STAGES] == run_local.STAGES


def test_retired_stages_are_not_run_or_timed():
    for stage in RETIRED_STAGES:
        assert stage in tasks.TASKS, f"keep '{stage}' as a no-op until the state machine stops calling it"
        assert stage not in dict(run_local.STAGES)
        assert not hasattr(tasks.TASKS[stage][0], "__wrapped__"), f"'{stage}' must not record a metric"


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
