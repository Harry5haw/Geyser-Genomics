"""Dashboard and alarm builders (scripts/monitoring.py) and their CLIs. Nothing here reaches AWS."""
import json
import os
import re
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

import deploy_alarms
import deploy_dashboard
import deepvariant_task
import monitoring
import query_pipeline_duration
import runtime_deploy_pipeline_dashboard
from conftest import REPO_ROOT

# Obviously fake ARNs: tests must not contain a real account ID either.
SFN_ARN = "arn:aws:states:eu-west-2:000000000000:stateMachine:geyser-pipeline-sfn-dev"
SNS_ARN = "arn:aws:sns:eu-west-2:000000000000:geyser-PipelineStatus-dev"
ACCOUNT_ID_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")
METRIC_ID_RE = re.compile(r"^[a-z][a-zA-Z0-9_]*$")
SCRIPTS = ("monitoring.py", "deploy_dashboard.py", "runtime_deploy_pipeline_dashboard.py", "deploy_alarms.py",
           "query_pipeline_duration.py")


def all_stage_names():
    return {s.task_name for stages in monitoring.STAGE_GROUPS.values() for s in stages}


def metric_rows(body):
    return [row for w in body["widgets"] if w["type"] == "metric" for row in w["properties"]["metrics"]]


def row_options(row):
    return row[-1] if isinstance(row[-1], dict) else {}


# ------------------------------------------------------------------ stage list matches the pipeline code


def test_core_stages_match_timed_stage_names_in_tasks():
    with open(os.path.join(REPO_ROOT, "app", "tasks.py"), encoding="utf-8") as fh:
        timed = re.findall(r'@timed_stage\("([\w.]+)"\)', fh.read())
    assert [s.task_name for s in monitoring.STAGE_GROUPS["core"]] == timed


def test_deepvariant_stages_match_deepvariant_task():
    names = [s.task_name for s in monitoring.STAGE_GROUPS["deepvariant"]]
    assert names == ["DeepVariant", *deepvariant_task.STEP_METRICS.values()]
    with open(os.path.join(REPO_ROOT, "app-deepvariant", "deepvariant_task.py"), encoding="utf-8") as fh:
        assert '@timed_stage("DeepVariant")' in fh.read()


def test_only_timed_stages_record_failures():
    for stages in monitoring.STAGE_GROUPS.values():
        assert all(s.records_failures == ("." not in s.task_name) for s in stages)


def test_caller_stages_builds_a_new_caller():
    stages = monitoring.caller_stages("Pangenome")
    assert [s.task_name for s in stages] == [
        "Pangenome", "Pangenome.MakeExamples", "Pangenome.CallVariants", "Pangenome.PostprocessVariants"]


def test_unknown_group_and_schema_rejected():
    with pytest.raises(ValueError):
        monitoring.build_dashboard(groups=["nope"])
    with pytest.raises(ValueError):
        monitoring.build_dashboard(schema="nope")


# ------------------------------------------------------------------ dashboard


@pytest.mark.parametrize("schema", monitoring.SCHEMAS)
@pytest.mark.parametrize("sfn_arn", [None, SFN_ARN])
def test_dashboard_shape(schema, sfn_arn):
    body = monitoring.build_dashboard(state_machine_arn=sfn_arn, schema=schema)
    assert json.loads(json.dumps(body)) == body
    assert body["start"] == "-P14D" and body["periodOverride"] == "inherit"
    for w in body["widgets"]:
        assert w["type"] in ("metric", "text")
        assert 0 <= w["x"] and w["x"] + w["width"] <= monitoring.GRID_WIDTH and w["y"] >= 0
        if w["type"] == "metric":
            props = w["properties"]
            assert props["region"] == "eu-west-2" and props["metrics"]
            ids = [row_options(r).get("id") for r in props["metrics"]]
            assert all(METRIC_ID_RE.match(i) for i in ids), ids
            assert len(ids) == len(set(ids)), f"duplicate ids in {props['title']}"


def test_widgets_do_not_overlap():
    body = monitoring.build_dashboard(state_machine_arn=SFN_ARN)
    cells = set()
    for w in body["widgets"]:
        new = {(x, y) for x in range(w["x"], w["x"] + w["width"]) for y in range(w["y"], w["y"] + w["height"])}
        assert not cells & new
        cells |= new


def test_dashboard_has_the_widgets_from_issue_48():
    titles = [w["properties"].get("title", "") for w in monitoring.build_dashboard(state_machine_arn=SFN_ARN)["widgets"]]
    assert any("runs per day" in t.lower() for t in titles)
    assert any("End-to-end" in t for t in titles)
    assert any("Failures by stage" in t for t in titles)
    assert sum(t.startswith("Stage duration") for t in titles) == len(monitoring.STAGE_GROUPS)


def test_every_stage_appears_in_the_duration_widgets():
    text = json.dumps(monitoring.build_dashboard())
    for name in all_stage_names():
        assert f'TaskName=\\"{name}\\"' in text


def test_per_sample_duration_uses_exact_success_search():
    rows = monitoring.duration_metrics([monitoring.Stage("Align")], "per-sample", "GeyserGenomics", 3600)
    exprs = [r[0]["expression"] for r in rows]
    assert exprs == [
        "AVG(SEARCH('{GeyserGenomics,SampleId,Status,TaskName} MetricName=\"Duration\" TaskName=\"Align\" "
        "Status=\"Success\"', 'Average', 3600))",
        "MAX(SEARCH('{GeyserGenomics,SampleId,Status,TaskName} MetricName=\"Duration\" TaskName=\"Align\" "
        "Status=\"Success\"', 'Maximum', 3600))",
    ]


def test_aggregated_duration_gives_real_percentiles():
    rows = monitoring.duration_metrics([monitoring.Stage("Align")], "aggregated", "GeyserGenomics", 3600)
    assert [r[:6] for r in rows] == [["GeyserGenomics", "Duration", "TaskName", "Align", "Status", "Success"]] * 2
    assert [r[6]["stat"] for r in rows] == ["p50", "p95"]


def test_failures_only_for_failure_recording_stages():
    stages = [s for g in monitoring.STAGE_GROUPS.values() for s in g]
    for schema in monitoring.SCHEMAS:
        rows = monitoring.failure_metrics(stages, schema, "GeyserGenomics", 86400)
        assert [row_options(r)["label"] for r in rows] == [s.task_name for s in stages if s.records_failures]
        assert all("FailureCount" in json.dumps(r) for r in rows)


def test_pipeline_widgets_use_step_functions_metrics():
    body = monitoring.build_pipeline_runtime_dashboard("eu-west-2", SFN_ARN)
    rows = metric_rows(body)
    named = [r for r in rows if r[0] == "AWS/States"]
    assert {r[1] for r in named} >= {"ExecutionsStarted", "ExecutionsFailed", "ExecutionTime"}
    assert all(r[2:4] == ["StateMachineArn", SFN_ARN] for r in named)
    assert {row_options(r)["stat"] for r in named if r[1] == "ExecutionTime"} == {"p50", "p95"}
    assert {"ms_p50/1000", "ms_p95/1000"} <= {row_options(r).get("expression") for r in rows}


def test_without_state_machine_runs_per_day_falls_back_to_stage_metrics():
    body = monitoring.build_dashboard()
    assert "AWS/States" not in json.dumps(body)
    assert any("Runs per day (from Decompress" in w["properties"].get("title", "") for w in body["widgets"])


def test_runtime_dashboard_needs_arn():
    with pytest.raises(ValueError):
        monitoring.build_pipeline_runtime_dashboard("eu-west-2", None)


# ------------------------------------------------------------------ alarms (one email per incident)


def read_infra(name):
    with open(os.path.join(REPO_ROOT, "infrastructure", name), encoding="utf-8") as fh:
        return fh.read()


def test_only_alarm_is_trigger_lambda_errors():
    (alarm,) = monitoring.build_alarms(SNS_ARN)
    assert alarm["AlarmName"] == "geyser-dev-trigger-lambda-errors"
    assert alarm["Namespace"] == "AWS/Lambda" and alarm["MetricName"] == "Errors"
    assert alarm["Dimensions"] == [{"Name": "FunctionName", "Value": "geyser-sfn-trigger"}]
    assert alarm["AlarmActions"] == [SNS_ARN]
    assert alarm["Threshold"] == 1 and alarm["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"
    assert alarm["TreatMissingData"] == "notBreaching"


def test_no_alarm_duplicates_notify_failure():
    text = json.dumps(monitoring.build_alarms(SNS_ARN))
    assert "ExecutionsFailed" not in text and "FailureCount" not in text


def test_notify_failure_still_emails_stage_failures():
    """The no-duplicate design relies on this. If the SNS publish moves to EventBridge, revisit build_alarms."""
    sfn = read_infra("step_functions.tf")
    notify = sfn[sfn.index("Notify_Failure = {"):sfn.index("Pipeline_Failed = {")]
    assert "arn:aws:states:::sns:publish" in notify
    assert "aws_sns_topic.geyser_pipeline_status_topic.arn" in notify
    assert sfn.count('Next = "Notify_Failure"') == 4  # every stage's Catch


def test_default_trigger_function_name_matches_terraform():
    assert 'function_name = "${var.project_name}-sfn-trigger"' in read_infra("lambda_trigger.tf")
    assert monitoring.DEFAULT_TRIGGER_FUNCTION == "geyser-sfn-trigger"


def test_alarm_arguments_validated():
    with pytest.raises(ValueError):
        monitoring.build_alarms(None)
    with pytest.raises(ValueError):
        monitoring.build_alarms(SNS_ARN, trigger_function_name="")


# ------------------------------------------------------------------ no hard-coded account IDs or ARNs


def test_no_account_ids_or_arns_in_monitoring_scripts():
    for name in SCRIPTS:
        with open(os.path.join(REPO_ROOT, "scripts", name), encoding="utf-8") as fh:
            source = fh.read()
        assert not ACCOUNT_ID_RE.search(source), name
        assert "arn:aws:" not in source, name


def test_generated_json_has_no_account_ids_unless_passed_in(capsys, monkeypatch):
    for var in ("SNS_TOPIC_ARN", "SFN_ARN", "TRIGGER_FUNCTION_NAME"):
        monkeypatch.delenv(var, raising=False)
    deploy_dashboard.main(["--dry-run"])
    deploy_alarms.main(["--dry-run"])
    out = capsys.readouterr().out
    assert not ACCOUNT_ID_RE.search(out) and "arn:" not in out


# ------------------------------------------------------------------ CLIs (dry run and mocked deploys)


def test_dashboard_dry_run_prints_valid_json_without_aws(capsys, monkeypatch):
    monkeypatch.delenv("SFN_ARN", raising=False)
    with mock.patch("boto3.client") as client:
        assert deploy_dashboard.main(["--dry-run", "--state-machine-arn", SFN_ARN, "--schema", "aggregated"]) == 0
    client.assert_not_called()
    body = json.loads(capsys.readouterr().out)
    assert body["widgets"] and SFN_ARN in json.dumps(body)


def test_dashboard_groups_option(capsys, monkeypatch):
    monkeypatch.delenv("SFN_ARN", raising=False)
    deploy_dashboard.main(["--dry-run", "--groups", "core"])
    text = capsys.readouterr().out
    assert "Decompress" in text and "DeepVariant" not in text


def test_dashboard_rejects_bad_arn():
    with pytest.raises(SystemExit):
        deploy_dashboard.main(["--dry-run", "--state-machine-arn", "not-an-arn"])


def test_dashboard_deploy_calls_put_dashboard(monkeypatch):
    monkeypatch.delenv("SFN_ARN", raising=False)
    client = mock.Mock()
    client.put_dashboard.return_value = {"DashboardValidationMessages": []}
    assert deploy_dashboard.main(["--dashboard-name", "d1"], client=client) == 0
    kwargs = client.put_dashboard.call_args.kwargs
    assert kwargs["DashboardName"] == "d1" and json.loads(kwargs["DashboardBody"])["widgets"]


def test_dashboard_deploy_reports_errors():
    client = mock.Mock()
    client.put_dashboard.side_effect = RuntimeError("denied")
    assert deploy_dashboard.deploy(client, "d1", {"widgets": []}) == 1


def test_runtime_dashboard_cli(capsys, monkeypatch):
    monkeypatch.delenv("SFN_ARN", raising=False)
    with pytest.raises(SystemExit):
        runtime_deploy_pipeline_dashboard.main(["--dry-run"])
    monkeypatch.setenv("SFN_ARN", SFN_ARN)
    assert runtime_deploy_pipeline_dashboard.main(["--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["widgets"]
    client = mock.Mock()
    client.put_dashboard.return_value = {}
    assert runtime_deploy_pipeline_dashboard.main([], client=client) == 0
    assert client.put_dashboard.call_args.kwargs["DashboardName"] == "geyser-pipeline-runtime"


def test_alarms_dry_run_uses_placeholder(capsys, monkeypatch):
    for var in ("SNS_TOPIC_ARN", "TRIGGER_FUNCTION_NAME"):
        monkeypatch.delenv(var, raising=False)
    with mock.patch("boto3.client") as client:
        assert deploy_alarms.main(["--dry-run"]) == 0
    client.assert_not_called()
    (alarm,) = json.loads(capsys.readouterr().out)
    assert alarm["AlarmActions"] == [deploy_alarms.PLACEHOLDER_SNS]


def test_alarms_deploy_requires_topic(monkeypatch):
    monkeypatch.delenv("SNS_TOPIC_ARN", raising=False)
    with pytest.raises(SystemExit):
        deploy_alarms.main([], client=mock.Mock())


def test_alarms_deploy_puts_the_alarm(monkeypatch):
    monkeypatch.setenv("SNS_TOPIC_ARN", SNS_ARN)
    monkeypatch.setenv("TRIGGER_FUNCTION_NAME", "geyser-prod-sfn-trigger")
    client = mock.Mock()
    assert deploy_alarms.main(["--prefix", "geyser-prod"], client=client) == 0
    (call,) = client.put_metric_alarm.call_args_list
    assert call.kwargs["AlarmName"] == "geyser-prod-trigger-lambda-errors"
    assert call.kwargs["Dimensions"][0]["Value"] == "geyser-prod-sfn-trigger"


def test_alarms_deploy_reports_partial_failure():
    client = mock.Mock()
    client.put_metric_alarm.side_effect = [None, RuntimeError("denied")]
    assert deploy_alarms.deploy(client, [{"AlarmName": "a"}, {"AlarmName": "b"}]) == 1


def test_query_pipeline_duration_is_importable_and_uses_given_client(capsys, monkeypatch):
    monkeypatch.delenv("SFN_ARN", raising=False)
    with pytest.raises(SystemExit):
        query_pipeline_duration.main([])
    start = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    client = mock.Mock()
    client.list_executions.return_value = {"executions": [{"executionArn": "e1", "name": "run-1"}]}
    client.describe_execution.return_value = {
        "startDate": start, "stopDate": start + timedelta(minutes=2), "status": "SUCCEEDED",
        "input": json.dumps({"srr_id": "SRR001"})}
    query_pipeline_duration.main(["--state-machine-arn", SFN_ARN], sfn_client=client)
    assert client.list_executions.call_args.kwargs["stateMachineArn"] == SFN_ARN
    out = capsys.readouterr().out
    assert "SRR001" in out and "2.00m" in out
