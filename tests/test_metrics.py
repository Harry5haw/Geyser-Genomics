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
