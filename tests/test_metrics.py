import json

from metrics import LocalMetrics, NullMetrics, metrics_from_env


def test_local_metrics_appends_json_lines(tmp_path):
    path = tmp_path / "m" / "metrics.jsonl"
    m = LocalMetrics(str(path))
    m.record("Align", "S1", "Success", 1.23456)
    m.record("Align", "S1", "Failure", 2.0)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r["status"] for r in rows] == ["Success", "Failure"]
    assert rows[0]["duration_seconds"] == 1.235


def test_local_storage_defaults_to_local_metrics(tmp_path):
    m = metrics_from_env({"STORAGE_BACKEND": "local", "LOCAL_STORAGE_DIR": str(tmp_path)})
    assert isinstance(m, LocalMetrics)
    assert m.path.startswith(str(tmp_path))


def test_metrics_can_be_disabled():
    assert isinstance(metrics_from_env({"STORAGE_BACKEND": "local", "METRICS_BACKEND": "none"}), NullMetrics)


def _stubbed_cloudwatch():
    from botocore.stub import Stubber

    from metrics import CloudWatchMetrics

    m = CloudWatchMetrics("eu-west-2")
    return m, Stubber(m._client)


def _duration(status, value):
    return {
        "MetricName": "Duration",
        "Dimensions": [
            {"Name": "TaskName", "Value": "Align"},
            {"Name": "SampleId", "Value": "S1"},
            {"Name": "Status", "Value": status},
        ],
        "Value": value,
        "Unit": "Seconds",
    }


def test_cloudwatch_success_sends_duration_only():
    # The dashboard scripts read exactly this namespace, metric name and dimension set.
    m, stub = _stubbed_cloudwatch()
    stub.add_response("put_metric_data", {}, {"Namespace": "GeyserGenomics", "MetricData": [_duration("Success", 1.5)]})
    with stub:
        m.record("Align", "S1", "Success", 1.5)
    stub.assert_no_pending_responses()


def test_cloudwatch_failure_also_counts_the_failure():
    m, stub = _stubbed_cloudwatch()
    failure = {
        "MetricName": "FailureCount",
        "Dimensions": [{"Name": "TaskName", "Value": "Align"}, {"Name": "SampleId", "Value": "S1"}],
        "Value": 1,
        "Unit": "Count",
    }
    stub.add_response(
        "put_metric_data", {}, {"Namespace": "GeyserGenomics", "MetricData": [_duration("Failure", 2.0), failure]}
    )
    with stub:
        m.record("Align", "S1", "Failure", 2.0)
    stub.assert_no_pending_responses()


def test_s3_storage_defaults_to_cloudwatch():
    from metrics import CloudWatchMetrics

    assert isinstance(metrics_from_env({"STORAGE_BACKEND": "s3"}), CloudWatchMetrics)


def test_s3_compatible_endpoint_defaults_to_no_metrics():
    # #80: an S3-compatible service outside AWS has no CloudWatch next to it.
    for var in ("S3_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3"):
        env = {"STORAGE_BACKEND": "s3", var: "https://s3.gra.io.cloud.ovh.net"}
        assert isinstance(metrics_from_env(env), NullMetrics), var


def test_an_aws_s3_endpoint_still_defaults_to_cloudwatch():
    from metrics import CloudWatchMetrics

    env = {"STORAGE_BACKEND": "s3", "S3_ENDPOINT_URL": "https://s3.eu-west-2.amazonaws.com"}
    assert isinstance(metrics_from_env(env), CloudWatchMetrics)


def test_s3_compatible_endpoint_can_still_choose_a_metrics_backend(tmp_path):
    env = {
        "STORAGE_BACKEND": "s3",
        "S3_ENDPOINT_URL": "http://localhost:9000",
        "METRICS_BACKEND": "local",
        "METRICS_FILE": str(tmp_path / "m.jsonl"),
    }
    assert isinstance(metrics_from_env(env), LocalMetrics)


def test_metrics_file_override(tmp_path):
    path = str(tmp_path / "custom.jsonl")
    m = metrics_from_env({"STORAGE_BACKEND": "local", "METRICS_FILE": path})
    assert m.path == path


def test_unknown_metrics_backend_is_rejected():
    import pytest

    with pytest.raises(RuntimeError):
        metrics_from_env({"METRICS_BACKEND": "prometheus"})


# ---- counts by kind (scripts/reconcile.py --publish-metrics) ----


def test_local_counts_are_one_line_per_kind_and_skipped_by_the_stage_reader(tmp_path):
    path = tmp_path / "metrics.jsonl"
    m = LocalMetrics(str(path))
    m.record("Align", "S1", "Success", 1.0)
    m.record_counts("ReconcileProblems", "Problem", {"LoneMate": 2, "Total": 2})
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[1:] == [
        {
            "timestamp": rows[1]["timestamp"],
            "metric": "ReconcileProblems",
            "dimensions": {"Problem": k},
            "value": v,
            "unit": "Count",
        }
        for k, v in (("LoneMate", 2), ("Total", 2))
    ]
    # web/samples/status.py reads only lines with task, sample_id and status: the stage record, not the counts.
    assert [r for r in rows if {"task", "sample_id", "status"} <= r.keys()] == rows[:1]


def test_cloudwatch_counts_send_one_value_per_kind():
    m, stub = _stubbed_cloudwatch()
    data = [
        {
            "MetricName": "ReconcileProblems",
            "Dimensions": [{"Name": "Problem", "Value": k}],
            "Value": v,
            "Unit": "Count",
        }
        for k, v in (("LoneMate", 1), ("Ignored", 0), ("Total", 1))
    ]
    stub.add_response("put_metric_data", {}, {"Namespace": "GeyserGenomics", "MetricData": data})
    with stub:
        m.record_counts("ReconcileProblems", "Problem", {"LoneMate": 1, "Ignored": 0, "Total": 1})
    stub.assert_no_pending_responses()


def test_null_metrics_ignore_counts():
    NullMetrics().record_counts("ReconcileProblems", "Problem", {"Total": 3})
