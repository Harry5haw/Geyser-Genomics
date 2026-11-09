"""Dashboard and alarm builders (scripts/monitoring.py) and their CLIs. Nothing here reaches AWS."""

import json
import os
import re
from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest

import deepvariant_task
import deploy_alarms
import deploy_dashboard
import monitoring
import query_pipeline_duration
import runtime_deploy_pipeline_dashboard
from conftest import REPO_ROOT

# Obviously fake ARNs: tests must not contain a real account ID either.
SFN_ARN = "arn:aws:states:eu-west-2:000000000000:stateMachine:geyser-pipeline-sfn-dev"
SNS_ARN = "arn:aws:sns:eu-west-2:000000000000:geyser-PipelineStatus-dev"
ACCOUNT_ID_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")
METRIC_ID_RE = re.compile(r"^[a-z][a-zA-Z0-9_]*$")
SCRIPTS = (
    "monitoring.py",
    "deploy_dashboard.py",
    "runtime_deploy_pipeline_dashboard.py",
    "deploy_alarms.py",
    "query_pipeline_duration.py",
)


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


def test_shard_stage_matches_tasks_py():
    import tasks

    assert [s.task_name for s in monitoring.STAGE_GROUPS["shards"]] == [tasks.SHARD_METRIC]
    assert "." not in tasks.SHARD_METRIC  # a timed stage (records failures), not a success-only sub-step


def test_gate_stage_matches_tasks_py():
    import tasks

    assert [s.task_name for s in monitoring.STAGE_GROUPS["gate"]] == [tasks.GATE_METRIC]


def test_deepvariant_stages_match_deepvariant_task():
    names = [s.task_name for s in monitoring.STAGE_GROUPS["deepvariant"]]
    assert names == ["DeepVariant", *deepvariant_task.STEP_METRICS.values()]
    stage_names = getattr(deepvariant_task, "STAGE_NAME", None)
    if stage_names is None:  # before pangenome support (#51) lands
        with open(os.path.join(REPO_ROOT, "app-deepvariant", "deepvariant_task.py"), encoding="utf-8") as fh:
            assert '@timed_stage("DeepVariant")' in fh.read()
    else:
        assert stage_names[False] == "DeepVariant"


def test_pangenome_stages_match_deepvariant_task_once_it_exists():
    """Pangenome-aware DeepVariant (#51) publishes PangenomeDeepVariant(.Step) metrics; the dashboard expects them."""
    if not hasattr(deepvariant_task, "PANGENOME_STEP_METRICS"):
        pytest.skip("pangenome DeepVariant not on this branch yet")
    names = [s.task_name for s in monitoring.STAGE_GROUPS["pangenome"]]
    assert names == [deepvariant_task.STAGE_NAME[True], *deepvariant_task.PANGENOME_STEP_METRICS.values()]


def test_only_timed_stages_record_failures():
    for stages in monitoring.STAGE_GROUPS.values():
        assert all(s.records_failures == ("." not in s.task_name) for s in stages)


def test_caller_stages_builds_a_new_caller():
    stages = monitoring.caller_stages("Pangenome")
    assert [s.task_name for s in stages] == [
        "Pangenome",
        "Pangenome.MakeExamples",
        "Pangenome.CallVariants",
        "Pangenome.PostprocessVariants",
    ]


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
        assert w["type"] in ("metric", "text", "log")
        assert 0 <= w["x"] and w["x"] + w["width"] <= monitoring.GRID_WIDTH and w["y"] >= 0
        if w["type"] == "log":
            props = w["properties"]
            assert props["region"] == "eu-west-2" and props["view"] in ("table", "timeSeries")
            groups = (monitoring.BATCH_LOG_GROUP, monitoring.BATCH_EVENTS_LOG_GROUP)
            assert any(props["query"].startswith(f"SOURCE '{g}' | ") for g in groups)
        if w["type"] == "metric":
            props = w["properties"]
            # AWS/Billing exists only in us-east-1: the one cross-region widget (O9).
            billing = props["metrics"][0][:2] == ["AWS/Billing", "EstimatedCharges"]
            assert props["region"] == ("us-east-1" if billing else "eu-west-2") and props["metrics"]
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
    titles = [
        w["properties"].get("title", "") for w in monitoring.build_dashboard(state_machine_arn=SFN_ARN)["widgets"]
    ]
    assert any("runs per day" in t.lower() for t in titles)
    assert any("End-to-end" in t for t in titles)
    assert any("Failures by stage" in t for t in titles)
    assert sum(t.startswith("Stage duration") for t in titles) == len(monitoring.STAGE_GROUPS)


def test_every_stage_appears_in_the_duration_widgets():
    per_sample = json.dumps(monitoring.build_dashboard(schema="per-sample"))
    aggregated = json.dumps(monitoring.build_dashboard())  # the default: what app/metrics.py publishes
    for name in all_stage_names():
        assert f'TaskName=\\"{name}\\"' in per_sample
        assert f'"TaskName", "{name}"' in aggregated


def test_dashboard_default_schema_matches_what_metrics_publishes():
    # app/metrics.py publishes Duration with TaskName + Status only; the default dashboard must read that shape.
    body = monitoring.build_dashboard()
    assert "SampleId" not in json.dumps(body)


def test_per_sample_duration_uses_exact_success_search():
    rows = monitoring.duration_metrics([monitoring.Stage("Align")], "per-sample", "GeyserGenomics", 3600)
    exprs = [r[0]["expression"] for r in rows]
    assert exprs == [
        'AVG(SEARCH(\'{GeyserGenomics,SampleId,Status,TaskName} MetricName="Duration" TaskName="Align" '
        "Status=\"Success\"', 'Average', 3600))",
        'MAX(SEARCH(\'{GeyserGenomics,SampleId,Status,TaskName} MetricName="Duration" TaskName="Align" '
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
    assert any("Runs per day (from QualityControl" in w["properties"].get("title", "") for w in body["widgets"])


def test_runtime_dashboard_needs_arn():
    with pytest.raises(ValueError):
        monitoring.build_pipeline_runtime_dashboard("eu-west-2", None)


# ------------------------------------------------------------------ alarm and alarm widget


def test_alarm_is_trigger_lambda_errors_without_email_by_default():
    (alarm,) = monitoring.build_alarms()
    assert alarm["AlarmName"] == "geyser-dev-trigger-lambda-errors"
    assert alarm["Namespace"] == "AWS/Lambda" and alarm["MetricName"] == "Errors"
    assert alarm["Dimensions"] == [{"Name": "FunctionName", "Value": "geyser-sfn-trigger"}]
    assert alarm["AlarmActions"] == [] and alarm["ActionsEnabled"] is False
    assert alarm["Threshold"] == 1 and alarm["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"
    assert alarm["TreatMissingData"] == "notBreaching"


def test_alarm_emails_only_when_topic_given():
    (alarm,) = monitoring.build_alarms(SNS_ARN)
    assert alarm["AlarmActions"] == [SNS_ARN] and alarm["ActionsEnabled"] is True


def test_default_trigger_function_name_matches_terraform():
    with open(os.path.join(REPO_ROOT, "infrastructure", "lambda_trigger.tf"), encoding="utf-8") as fh:
        assert 'function_name = "${var.project_name}-sfn-trigger"' in fh.read()
    assert monitoring.DEFAULT_TRIGGER_FUNCTION == "geyser-sfn-trigger"  # project_name = "geyser"


def test_alarm_arguments_validated():
    with pytest.raises(ValueError):
        monitoring.build_alarms(trigger_function_name="")


def test_alarm_widget_shows_the_deployed_alarm():
    arns = [monitoring.alarm_arn("eu-west-2", "000000000000", n) for n in monitoring.alarm_names("geyser-dev")]
    assert arns == ["arn:aws:cloudwatch:eu-west-2:000000000000:alarm:geyser-dev-trigger-lambda-errors"]
    assert monitoring.alarm_names() == [a["AlarmName"] for a in monitoring.build_alarms()]
    body = monitoring.build_dashboard(alarm_arns=arns)
    (widget,) = [w for w in body["widgets"] if w["type"] == "alarm"]
    assert widget["properties"]["alarms"] == arns
    assert body["widgets"][0]["width"] + widget["width"] == monitoring.GRID_WIDTH  # shares the top row
    assert not [w for w in monitoring.build_dashboard()["widgets"] if w["type"] == "alarm"]


# ------------------------------------------------------------------ no hard-coded account IDs or ARNs

FULL_ARN_RE = re.compile(r"arn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:\d{12}:")


def test_no_account_ids_or_arns_in_monitoring_scripts():
    for name in SCRIPTS:
        with open(os.path.join(REPO_ROOT, "scripts", name), encoding="utf-8") as fh:
            source = fh.read()
        assert not ACCOUNT_ID_RE.search(source), name
        assert not FULL_ARN_RE.search(source), name


def test_dry_run_json_has_no_account_ids(capsys, monkeypatch):
    for var in ("SFN_ARN", "TRIGGER_FUNCTION_NAME", "AWS_ACCOUNT_ID"):
        monkeypatch.delenv(var, raising=False)
    deploy_dashboard.main(["--dry-run"])
    deploy_alarms.main(["--dry-run"])
    out = capsys.readouterr().out
    assert not ACCOUNT_ID_RE.search(out)
    assert f":{deploy_dashboard.PLACEHOLDER_ACCOUNT}:alarm:" in out


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
    assert "QualityControl" in text and "Decompress" not in text and "DeepVariant" not in text


def test_dashboard_rejects_bad_arn():
    with pytest.raises(SystemExit):
        deploy_dashboard.main(["--dry-run", "--state-machine-arn", "not-an-arn"])


def test_dashboard_deploy_calls_put_dashboard_with_account_from_sts(monkeypatch):
    for var in ("SFN_ARN", "AWS_ACCOUNT_ID"):
        monkeypatch.delenv(var, raising=False)
    client, sts = mock.Mock(), mock.Mock()
    client.put_dashboard.return_value = {"DashboardValidationMessages": []}
    client.describe_alarms.return_value = {"MetricAlarms": [{"AlarmName": "geyser-dev-trigger-lambda-errors"}]}
    sts.get_caller_identity.return_value = {"Account": "000000000000"}
    assert deploy_dashboard.main(["--dashboard-name", "d1"], client=client, sts_client=sts) == 0
    kwargs = client.put_dashboard.call_args.kwargs
    assert kwargs["DashboardName"] == "d1"
    assert ":000000000000:alarm:geyser-dev-trigger-lambda-errors" in kwargs["DashboardBody"]


def test_dashboard_account_id_option(monkeypatch, capsys):
    monkeypatch.delenv("SFN_ARN", raising=False)
    with pytest.raises(SystemExit):
        deploy_dashboard.main(["--dry-run", "--account-id", "12345"])
    sts = mock.Mock()
    deploy_dashboard.main(["--dry-run", "--account-id", "000000000000"], sts_client=sts)
    sts.get_caller_identity.assert_not_called()
    assert ":000000000000:alarm:" in capsys.readouterr().out
    deploy_dashboard.main(["--dry-run", "--no-alarm-widget"])
    assert '"alarm"' not in capsys.readouterr().out


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


def test_alarms_dry_run_makes_no_aws_calls(capsys, monkeypatch):
    monkeypatch.delenv("TRIGGER_FUNCTION_NAME", raising=False)
    with mock.patch("boto3.client") as client:
        assert deploy_alarms.main(["--dry-run"]) == 0
    client.assert_not_called()
    (alarm,) = json.loads(capsys.readouterr().out)
    assert alarm["AlarmActions"] == []


def test_alarms_deploy_puts_the_alarm(monkeypatch):
    monkeypatch.setenv("TRIGGER_FUNCTION_NAME", "geyser-prod-sfn-trigger")
    client = mock.Mock()
    assert deploy_alarms.main(["--prefix", "geyser-prod", "--sns-topic-arn", SNS_ARN], client=client) == 0
    (call,) = client.put_metric_alarm.call_args_list
    assert call.kwargs["AlarmName"] == "geyser-prod-trigger-lambda-errors"
    assert call.kwargs["Dimensions"][0]["Value"] == "geyser-prod-sfn-trigger"
    assert call.kwargs["AlarmActions"] == [SNS_ARN]


def test_alarms_reject_bad_topic_arn():
    with pytest.raises(SystemExit):
        deploy_alarms.main(["--dry-run", "--sns-topic-arn", "nope"])


def test_alarms_deploy_reports_partial_failure():
    client = mock.Mock()
    client.put_metric_alarm.side_effect = [None, RuntimeError("denied")]
    assert deploy_alarms.deploy(client, [{"AlarmName": "a"}, {"AlarmName": "b"}]) == 1


def test_query_pipeline_duration_is_importable_and_uses_given_client(capsys, monkeypatch):
    monkeypatch.delenv("SFN_ARN", raising=False)
    with pytest.raises(SystemExit):
        query_pipeline_duration.main([])
    start = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    client = mock.Mock()
    client.list_executions.return_value = {"executions": [{"executionArn": "e1", "name": "run-1"}]}
    client.describe_execution.return_value = {
        "startDate": start,
        "stopDate": start + timedelta(minutes=2),
        "status": "SUCCEEDED",
        "input": json.dumps({"srr_id": "SRR001"}),
    }
    query_pipeline_duration.main(["--state-machine-arn", SFN_ARN], sfn_client=client)
    assert client.list_executions.call_args.kwargs["stateMachineArn"] == SFN_ARN
    out = capsys.readouterr().out
    assert "SRR001" in out and "2.00m" in out


# ------------------------------------------------------------------ console links in the header


def test_console_links_without_arns_have_no_account_specific_parts():
    links = dict(monitoring.console_links("eu-west-2"))
    assert "Pipeline runs (Step Functions)" not in links and "Data lake (S3)" not in links
    assert links["Batch job logs"].endswith("#logsV2:log-groups/log-group/$252Faws$252Fbatch$252Fgeyser-dev")
    assert links["Trigger Lambda logs"].endswith("$252Faws$252Flambda$252Fgeyser-sfn-trigger-dev")
    assert links["Step Functions error logs"].endswith(
        "log-group/$252Faws$252Fvendedlogs$252Fstates$252Fgeyser-sfn-dev"
    )
    assert "#/functions/geyser-sfn-trigger?tab=monitoring" in links["Trigger Lambda"]
    assert all(url.startswith("https://") for url in links.values())
    assert all("eu-west-2" in url for label, url in links.items() if label != "Budgets")


def test_console_links_with_state_machine_and_bucket():
    links = dict(monitoring.console_links("eu-west-2", state_machine_arn=SFN_ARN, bucket="geyser-data-lake-abc"))
    assert links["Pipeline runs (Step Functions)"].endswith(
        "#/statemachines/view/arn%3Aaws%3Astates%3Aeu-west-2%3A000000000000%3AstateMachine%3Ageyser-pipeline-sfn-dev"
    )
    assert (
        links["Data lake (S3)"] == "https://s3.console.aws.amazon.com/s3/buckets/geyser-data-lake-abc?region=eu-west-2"
    )


def test_header_widget_lists_the_links():
    body = monitoring.build_dashboard(state_machine_arn=SFN_ARN)
    header = body["widgets"][0]["properties"]["markdown"]
    for label, url in monitoring.console_links("eu-west-2", state_machine_arn=SFN_ARN):
        assert f"[{label}]({url})" in header


def test_dashboard_cli_link_options(capsys, monkeypatch):
    for var in ("SFN_ARN", "BUCKET_NAME", "TRIGGER_FUNCTION_NAME"):
        monkeypatch.delenv(var, raising=False)
    deploy_dashboard.main(["--dry-run", "--bucket", "my-bucket", "--trigger-function-name", "geyser-sfn-trigger-x"])
    header = json.loads(capsys.readouterr().out)["widgets"][0]["properties"]["markdown"]
    assert "s3/buckets/my-bucket" in header and "#/functions/geyser-sfn-trigger-x" in header


def test_dashboard_leaves_out_alarms_that_do_not_exist(monkeypatch, capsys):
    monkeypatch.delenv("SFN_ARN", raising=False)
    client, sts = mock.Mock(), mock.Mock()
    client.put_dashboard.return_value = {}
    client.describe_alarms.return_value = {"MetricAlarms": []}
    assert deploy_dashboard.main(["--alarm-prefix", "geyser-test"], client=client, sts_client=sts) == 0
    assert client.describe_alarms.call_args.kwargs["AlarmNames"] == ["geyser-test-trigger-lambda-errors"]
    assert '"alarm"' not in client.put_dashboard.call_args.kwargs["DashboardBody"]
    assert "does not exist" in capsys.readouterr().err
    sts.get_caller_identity.assert_not_called()


def test_dashboard_still_deploys_if_alarms_cannot_be_checked(monkeypatch, capsys):
    monkeypatch.delenv("SFN_ARN", raising=False)
    client, sts = mock.Mock(), mock.Mock()
    client.put_dashboard.return_value = {}
    client.describe_alarms.side_effect = RuntimeError("AccessDenied")
    sts.get_caller_identity.return_value = {"Account": "000000000000"}
    assert deploy_dashboard.main([], client=client, sts_client=sts) == 0
    assert '"alarm"' in client.put_dashboard.call_args.kwargs["DashboardBody"]
    assert "could not check" in capsys.readouterr().err


# ------------------------------------------------------------------ result quality (#58)

FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures", "stats")


def _python_pattern(insights_pattern: str):
    """A QUALITY_PARSE entry as a Python regex: /.../ with (?<name>) groups, or a "glob" as a, b."""
    if insights_pattern.startswith("/"):
        return re.compile(insights_pattern[1:-1].replace("(?<", "(?P<"))
    glob, _, names = insights_pattern.partition(" as ")
    names = [n.strip() for n in names.split(",")]
    parts = re.escape(glob.strip('"')).split(r"\*")
    regex = "".join(p + (f"(?P<{names[i]}>.*?)" if i < len(names) else "") for i, p in enumerate(parts))
    return re.compile(regex + "$")


def _extract(lines):
    """What the dashboard's parse commands pull out of these log lines (first match per field)."""
    fields = {}
    for _, pattern in monitoring.QUALITY_PARSE:
        regex = _python_pattern(pattern)
        for line in lines:
            m = regex.search(line)
            if m:
                fields.update({k: v for k, v in m.groupdict().items() if k not in fields})
    return fields


def _job_log_lines(capsys, layout):
    """The lines a real align + variants run prints, made by the code that prints them."""
    import tasks

    for label, parse, name in (
        ("flagstat", tasks.parse_flagstat, "flagstat.txt"),
        ("mosdepth", tasks.parse_mosdepth_summary, "mosdepth.summary.txt"),
        ("bcftools stats", tasks.parse_bcftools_stats, "bcftools.stats.txt"),
    ):
        tasks._log_summary(label, parse, os.path.join(FIXTURES, f"{layout}.{name}"))
    return capsys.readouterr().out.splitlines()


@pytest.mark.parametrize("layout", ["single", "paired"])
def test_quality_patterns_read_the_lines_the_stages_print(capsys, layout):
    # If app/tasks.py changes its headline format, the dashboard would silently show blanks: this fails first.
    import tasks

    lines = _job_log_lines(capsys, layout)
    got = _extract(lines)

    def read(parse, name):
        with open(os.path.join(FIXTURES, f"{layout}.{name}")) as fh:
            return parse(fh.read())

    flag = read(tasks.parse_flagstat, "flagstat.txt")
    depth = read(tasks.parse_mosdepth_summary, "mosdepth.summary.txt")
    calls = read(tasks.parse_bcftools_stats, "bcftools.stats.txt")
    assert int(got["x_reads"]) == flag["in_total"]
    assert int(got["x_dups"]) == flag["duplicates"]
    assert float(got["x_mapped"]) == flag["mapped_pct"]  # mapped_pct, not primary_mapped_pct
    assert float(got["x_depth"]) == depth["mean_depth"]
    assert (int(got["x_vars"]), int(got["x_snps"]), int(got["x_indels"])) == (
        calls["records"],
        calls["snps"],
        calls["indels"],
    )
    if calls.get("tstv") is not None:
        assert float(got["x_tstv"]) == calls["tstv"]


def test_quality_patterns_read_the_stage_start_line(local_env, capsys):
    import tasks

    tasks.timed_stage("Align")(lambda sample_id: None)("SAMPLE_7")
    got = _extract(capsys.readouterr().out.splitlines())
    assert (got["x_task"], got["x_sample"]) == ("Align", "SAMPLE_7")


def test_quality_query_shape():
    q = monitoring.quality_query()
    assert q.count("| stats ") == 2 and "by @logStream" in q and q.rstrip().endswith("| limit 100")
    assert f"| display {', '.join(monitoring.QUALITY_COLUMNS)}" in q
    assert "| filter mapped_pct" not in q
    # Every parsed field is used by the first stats, and the aliases never reuse a parsed name.
    parsed = set(re.findall(r"\(\?<(x_\w+)>", q)) | {"x_sample"}
    assert all(f"({name})" in q for name in parsed - {"x_task"})
    filtered = monitoring.quality_query(monitoring.QualityThresholds(min_mapped_pct=95, max_dup_pct=10))
    assert "| filter mapped_pct < 95 or dup_pct > 10 or variants = 0" in filtered
    assert "snps >= 1000 and (tstv < 1.8 or tstv > 2.4)" in filtered
    assert filtered.index("| filter mapped_pct") < filtered.index("| display")


def test_quality_trend_query_bins_by_day():
    q = monitoring.quality_trend_query()
    assert "by bin(1d)" in q and "avg(x_mapped)" in q and "100 * x_dups / x_reads" in q


def test_dashboard_quality_widgets_on_by_default_and_optional():
    logs = [w for w in monitoring.build_dashboard(queue_wait=False, runs=False)["widgets"] if w["type"] == "log"]
    assert [w["properties"]["view"] for w in logs] == ["table", "table", "timeSeries"]
    assert "outside quality thresholds" in logs[1]["properties"]["title"]
    custom = monitoring.build_dashboard(batch_log_group="/aws/batch/geyser-prod", queue_wait=False, runs=False)
    assert all(
        w["properties"]["query"].startswith("SOURCE '/aws/batch/geyser-prod' | ")
        for w in custom["widgets"]
        if w["type"] == "log"
    )
    assert not [
        w
        for w in monitoring.build_dashboard(quality=False, queue_wait=False, runs=False)["widgets"]
        if w["type"] == "log"
    ]


def test_dashboard_cli_quality_options(capsys, monkeypatch):
    monkeypatch.delenv("SFN_ARN", raising=False)
    deploy_dashboard.main(["--dry-run", "--batch-log-group", "/aws/batch/other"])
    body = json.loads(capsys.readouterr().out)
    assert any(w["type"] == "log" and "SOURCE '/aws/batch/other'" in w["properties"]["query"] for w in body["widgets"])
    deploy_dashboard.main(["--dry-run", "--no-quality", "--no-runs"])
    quality = [
        w
        for w in json.loads(capsys.readouterr().out)["widgets"]
        if w["type"] == "log" and w["properties"]["query"].startswith(f"SOURCE '{monitoring.BATCH_LOG_GROUP}' | ")
    ]
    assert not quality  # the queue-wait widgets read the Batch event log group and stay


def test_run_records_link_only_with_a_bucket():
    labels = dict(monitoring.console_links(bucket="my-bucket"))
    assert labels["Run records (S3 runs/)"].endswith(
        "s3/buckets/my-bucket?region=eu-west-2&prefix=runs/&showversions=false"
    )
    assert "Run records (S3 runs/)" not in dict(monitoring.console_links())


def test_quality_patterns_take_the_exact_key_not_a_longer_one():
    # The fixtures have duplicates == primary duplicates, so a pattern grabbing the wrong one would
    # still pass above. Here every lookalike key comes after the real one, with a different value.
    line = (
        "flagstat: in_total=200, duplicates=12, primary_duplicates=7, mapped=190, mapped_pct=95.0, "
        "primary_mapped=150, primary_mapped_pct=88.5"
    )
    got = _extract(
        [
            line,
            "mosdepth: length=1, bases=1, mean_depth=3.5, x_mean_depth=9.9",
            "bcftools stats: records=5, snps=4, indels=1, tstv=2.1, x_tstv=9.9",
        ]
    )
    assert (got["x_reads"], got["x_dups"], got["x_mapped"]) == ("200", "12", "95.0")
    assert (got["x_depth"], got["x_tstv"]) == ("3.5", "2.1")


def test_quality_query_computes_dup_pct_per_job_and_drops_unnamed_jobs():
    q = monitoring.quality_query()
    first, second = (i for i, line in enumerate(q.splitlines()) if line.startswith("| stats "))
    lines = q.splitlines()
    assert lines[first + 1] == "| filter ispresent(sample)"
    assert lines[first + 2] == "| fields 100 * dp / rd as dpp" and first + 2 < second
    assert "max(dpp) as dup_raw" in lines[second] and "round(dup_raw, 2) as dup_pct" in q


def test_thresholds_list_aligned_samples_without_variant_stats():
    assert "(ispresent(reads) and not ispresent(variants))" in monitoring.DEFAULT_THRESHOLDS.filter_clause()


def test_batch_log_link_follows_the_log_group(capsys, monkeypatch):
    monkeypatch.delenv("SFN_ARN", raising=False)
    deploy_dashboard.main(["--dry-run", "--batch-log-group", "/aws/batch/geyser-prod"])
    header = json.loads(capsys.readouterr().out)["widgets"][0]["properties"]["markdown"]
    assert "geyser-prod" in header and "geyser-dev" not in header.split("Batch job logs](")[1].split(")")[0]


# ------------------------------------------------------------------ data lake storage (#62)


def test_storage_widgets_only_with_a_bucket():
    titles = lambda body: [w["properties"].get("title", "") for w in body["widgets"]]  # noqa: E731
    assert not any("Data lake" in t for t in titles(monitoring.build_dashboard()))
    body = monitoring.build_dashboard(bucket="my-bucket")
    storage = [w for w in body["widgets"] if w["properties"].get("title", "").startswith("Data lake")]
    assert [w["properties"]["title"] for w in storage] == ["Data lake size, GB (daily)", "Data lake objects (daily)"]
    rows = [r for w in storage for r in w["properties"]["metrics"]]
    assert ["AWS/S3", "BucketSizeBytes", "BucketName", "my-bucket", "StorageType", "StandardStorage"] == rows[0][:6]
    assert ["AWS/S3", "NumberOfObjects", "BucketName", "my-bucket", "StorageType", "AllStorageTypes"] == rows[2][:6]
    assert all(w["properties"]["period"] == 86400 for w in storage)  # S3 storage metrics are daily
    ids = [row_options(r)["id"] for r in rows]
    assert all(METRIC_ID_RE.match(i) for i in ids) and len(ids) == len(set(ids))


def test_dashboard_cli_bucket_adds_storage_widgets(capsys, monkeypatch):
    monkeypatch.delenv("SFN_ARN", raising=False)
    deploy_dashboard.main(["--dry-run", "--bucket", "my-bucket"])
    body = json.loads(capsys.readouterr().out)
    assert any(w["properties"].get("title") == "Data lake size, GB (daily)" for w in body["widgets"])


# ------------------------------------------------------------------ Batch queue wait (#138)


def _insights_regex(line):
    return re.compile(line.split("/", 1)[1].rsplit("/", 1)[0].replace("(?<", "(?P<"))


@pytest.mark.parametrize(
    "job_name, stage",
    [
        ("QualityControl-S1-S1-20261102T100000Z-abc", "QualityControl"),
        ("AlignGenome-S1-S1-20261102T100000Z-abc", "AlignGenome"),
        ("PlanRegions-S1-S1-20261102T100000Z-abc", "PlanRegions"),
        ("CallVariantsShard-S1-S1-20261102T100000Z-abc", "CallVariantsShard"),
        ("CallVariants-S1-S1-20261102T100000Z-abc", "CallVariants"),
    ],
)
def test_queue_wait_stage_comes_from_the_job_name(job_name, stage):
    assert _insights_regex(monitoring.QUEUE_WAIT_PARSE[0]).search(job_name).group("stage") == stage


def test_queue_wait_queue_comes_from_the_queue_arn():
    arn = "arn:aws:batch:eu-west-2:000000000000:job-queue/geyser-gpu-queue-dev"
    assert _insights_regex(monitoring.QUEUE_WAIT_PARSE[1]).search(arn).group("queue") == "geyser-gpu-queue-dev"


def test_queue_wait_widgets_follow_the_stage_durations():
    widgets = monitoring.build_dashboard()["widgets"]
    titles = [w["properties"].get("title", "") for w in widgets]
    first_wait = next(i for i, t in enumerate(titles) if t.startswith("Batch wait"))
    assert titles[first_wait - 1].startswith("Stage duration")
    wait = [w for w in widgets if w["properties"].get("title", "").startswith("Batch wait")]
    assert [w["properties"]["view"] for w in wait] == ["table", "timeSeries"]
    assert all(w["properties"]["query"].startswith(f"SOURCE '{monitoring.BATCH_EVENTS_LOG_GROUP}' | ") for w in wait)
    assert "(detail.startedAt - detail.createdAt) / 1000" in wait[0]["properties"]["query"]
    assert not [w for w in monitoring.build_dashboard(queue_wait=False)["widgets"] if "Batch wait" in str(w)]


def test_dashboard_cli_no_queue_wait(capsys, monkeypatch):
    monkeypatch.delenv("SFN_ARN", raising=False)
    deploy_dashboard.main(["--dry-run", "--no-queue-wait"])
    assert "Batch wait" not in capsys.readouterr().out


def test_run_widgets_read_the_run_record_line_and_spot_retries():
    widgets = {w["properties"]["title"]: w for w in monitoring.run_widgets()}
    runs = widgets["Recent runs (from the run record)"]["properties"]["query"]
    assert runs.startswith(f"SOURCE '{monitoring.BATCH_LOG_GROUP}' | filter @message like /^run-record:/")
    for field in ("caller_outcome", "route", "infra_retries", "gate", "reference", "app_version"):
        assert f'"{field}"' in runs
    outcome = next(w for t, w in widgets.items() if t.startswith("Whose calls"))["properties"]["query"]
    assert "stats count(*) as runs by outcome" in outcome
    spot = next(w for t, w in widgets.items() if t.startswith("Jobs retried"))["properties"]["query"]
    assert spot.startswith(f"SOURCE '{monitoring.BATCH_EVENTS_LOG_GROUP}' | ")
    assert "ispresent(detail.attempts.0.statusReason)" in spot


def test_run_record_parse_matches_the_apps_log_line():
    # The line app/provenance.py writes: the prefix and its JSON keys (json.dumps: '"key": value').
    import provenance

    if not hasattr(provenance, "RECORD_LOG_PREFIX"):
        pytest.skip("the run-record log line comes with #251")
    assert provenance.RECORD_LOG_PREFIX == monitoring.RUN_RECORD_PREFIX
    assert set(monitoring.RUN_RECORD_FIELDS) == {"task", *provenance.SUMMARY_FIELDS, "reference", "gate"}


def test_gpu_widgets_only_with_the_aggregated_schema_and_deepvariant():
    titles = [w["properties"].get("title", "") for w in monitoring.build_dashboard()["widgets"]]
    assert sum("on the GPU" in t for t in titles) == 2
    per_sample = monitoring.build_dashboard(schema="per-sample")["widgets"]
    assert not [w for w in per_sample if "on the GPU" in w["properties"].get("title", "")]
    core_only = monitoring.build_dashboard(groups=["core"])["widgets"]
    assert not [w for w in core_only if "on the GPU" in w["properties"].get("title", "")]


def test_header_links_the_benchmark_history():
    header = monitoring.build_dashboard()["widgets"][0]["properties"]["markdown"]
    assert monitoring.BENCHMARKS_URL in header


def test_spend_row_shows_charges_and_the_guardrail_lines():
    charges, text = monitoring.spend_widgets()
    assert charges["properties"]["region"] == "us-east-1"
    assert charges["properties"]["metrics"][0][:4] == ["AWS/Billing", "EstimatedCharges", "Currency", "USD"]
    lines = {a["value"] for a in charges["properties"]["annotations"]["horizontal"]}
    g = monitoring.GUARDRAILS
    assert lines == {g["kill_switch_usd"], g["run_block_usd"], g["gpu_block_usd"]}
    assert g["gpu_block_usd"] < g["run_block_usd"] < g["kill_switch_usd"] < g["cap_usd"]
    md = text["properties"]["markdown"]
    assert f"${g['cap_usd']}" in md and "geyser-claude-kill-switch" in md and monitoring.BUDGETS_URL in md
    assert not [w for w in monitoring.build_dashboard(spend=False)["widgets"] if "Estimated charges" in str(w)]


def test_running_now_reads_usage_and_every_bucket():
    vcpus, buckets = monitoring.running_now_widgets("eu-west-2")
    exprs = " ".join(r[0]["expression"] for r in vcpus["properties"]["metrics"])
    assert 'Service="EC2"' in exprs and 'Resource="vCPU"' in exprs and 'Service="Fargate"' in exprs
    assert 'MetricName="BucketSizeBytes"' in buckets["properties"]["metrics"][0][0]["expression"]


def test_gate_value_widgets_match_what_the_gate_publishes():
    import tasks

    assert monitoring.GATE_VALUES_METRIC == tasks.GATE_VALUES_METRIC
    assert monitoring.GATE_VALUE_CHECKS == tasks.GATE_VALUE_CHECKS
    shown = [r[3] for w in monitoring.gate_value_widgets() for r in w["properties"]["metrics"]]
    assert sorted(shown) == sorted(tasks.GATE_VALUE_CHECKS)
    titles = [w["properties"].get("title", "") for w in monitoring.build_dashboard()["widgets"]]
    assert sum(t.startswith("Quality gate:") for t in titles) == 2
