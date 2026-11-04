"""scripts/web_perf_budget.py: the HTML+CSS+JS weight budget and the Lighthouse CI summary (web-perf.yml)."""

import json
import os

import pytest

import web_perf_budget as budget
from conftest import REPO_ROOT

LIMITS = {"largest-contentful-paint": 999, "cumulative-layout-shift": 0, "total-blocking-time": 49}


def lhr(url, requests, lcp=800.0, cls=0.0, tbt=10.0):
    """A minimal Lighthouse result: requests as (resourceType, transferSize)."""
    return {
        "requestedUrl": url,
        "audits": {
            "network-requests": {
                "details": {"items": [{"resourceType": t, "transferSize": size} for t, size in requests]}
            },
            "largest-contentful-paint": {"numericValue": lcp},
            "cumulative-layout-shift": {"numericValue": cls},
            "total-blocking-time": {"numericValue": tbt},
        },
    }


LIST = "http://127.0.0.1:8000/"
DETAIL = "http://127.0.0.1:8000/samples/SYNTH001/"
LIGHT = [("Document", 9000), ("Stylesheet", 6000), ("Script", 2000), ("Image", 50000), ("Fetch", 9000)]
HEAVY = [("Document", 12000), ("Stylesheet", 82000), ("Stylesheet", 21000), ("Script", 51000)]


def test_only_html_css_and_js_count_towards_the_weight():
    assert budget.page_weight(lhr(LIST, LIGHT)) == (17000, 3)  # not the image or the data fetch


def test_the_median_run_is_reported_per_page():
    runs = [lhr(LIST, LIGHT, lcp=v) for v in (700, 1200, 900)] + [lhr(DETAIL, HEAVY, cls=0.02)]
    rows = {r["url"]: r for r in budget.summarise(runs, 30 * 1024, LIMITS)}
    assert rows[LIST]["runs"] == 3 and rows[LIST]["largest-contentful-paint"] == 900
    assert rows[LIST]["weight_ok"] and rows[LIST]["largest-contentful-paint_ok"]
    assert rows[DETAIL]["bytes"] == 166000 and not rows[DETAIL]["weight_ok"]
    assert rows[DETAIL]["cumulative-layout-shift_ok"] is False  # CLS must be exactly 0


def test_the_cli_fails_over_budget_and_writes_the_summary(tmp_path):
    for i, run in enumerate([lhr(LIST, LIGHT), lhr(DETAIL, HEAVY)]):
        (tmp_path / f"lhr-{i}.json").write_text(json.dumps(run))
    summary = tmp_path / "summary.md"
    config = os.path.join(REPO_ROOT, "lighthouserc.json")
    assert budget.main(["--dir", str(tmp_path), "--config", config, "--summary", str(summary)]) == 1
    text = summary.read_text(encoding="utf-8")
    assert "HTML+CSS+JS ≤ 30 KB" in text and "LCP ≤ 999 ms" in text and "CLS ≤ 0" in text
    assert f"| `{DETAIL}` (1 runs) | 162.1 KB ❌ |" in text and f"| `{LIST}` (1 runs) | 16.6 KB ✅ |" in text
    assert budget.main(["--dir", str(tmp_path), "--config", config, "--budget-kb", "200"]) == 0


def test_no_results_is_a_failure(tmp_path):
    assert budget.main(["--dir", str(tmp_path), "--config", os.path.join(REPO_ROOT, "lighthouserc.json")]) == 1


def test_the_timing_limits_come_from_lighthouserc():
    assert budget.limits_from_config(os.path.join(REPO_ROOT, "lighthouserc.json")) == LIMITS


@pytest.mark.parametrize("audit", list(budget.METRICS))
def test_lighthouserc_asserts_each_timing_budget_on_the_median_run(audit):
    with open(os.path.join(REPO_ROOT, "lighthouserc.json"), encoding="utf-8") as fh:
        level, options = json.load(fh)["ci"]["assert"]["assertions"][audit]
    assert level == "error" and options["aggregationMethod"] == "median-run"


def test_the_workflow_measures_both_pages_with_pinned_tools():
    with open(os.path.join(REPO_ROOT, ".github", "workflows", "web-perf.yml"), encoding="utf-8") as fh:
        workflow = fh.read()
    assert "@lhci/cli@0.16.0" in workflow and "@lhci/cli@latest" not in workflow
    assert 'DJANGO_DEBUG: "0"' in workflow  # production mode, not the dev server's DEBUG
    with open(os.path.join(REPO_ROOT, "lighthouserc.json"), encoding="utf-8") as fh:
        urls = json.load(fh)["ci"]["collect"]["url"]
    assert urls == [LIST, DETAIL]
