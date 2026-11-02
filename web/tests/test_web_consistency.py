"""The UI's stage table must agree with the pipeline: stage order, metric names and output keys."""

import os
import re

from geyser_web.pipeline import REPO_ROOT, keys_for, run_local
from samples.status import STAGES


def test_stage_order_matches_local_runner_and_state_machine():
    # tests/test_consistency.py already ties run_local.STAGES to infrastructure/state_machine.asl.json
    assert [s.name for s in STAGES] == [name for name, _ in run_local.STAGES]


def test_metric_names_match_tasks_py():
    with open(os.path.join(REPO_ROOT, "app", "tasks.py"), encoding="utf-8") as fh:
        source = fh.read()
    decorated = re.findall(r'@timed_stage\("(\w+)"\)\s*\ndef (\w+)_task', source)
    by_stage = {func: metric for metric, func in decorated}
    assert {s.name: s.metric for s in STAGES} == by_stage


def test_stage_outputs_are_real_keys():
    keys = keys_for("X")
    for st in STAGES:
        assert all(name in keys for name in st.output)
