"""The one-helix progress bar: the maths (samples/progress.py) and its rendering (samples/_progress.html)."""

import os
import re

import pytest
from django.template.loader import render_to_string

from samples import progress as pm
from samples.status import SampleView, StageView

from .test_web_theme import CSS, contrast, theme

T0 = 1_000_000.0


def sample(state, stages, started=T0):
    views = [StageView(n, label, st, d) for n, label, st, d in stages]
    return SampleView("S1", state, views, run={"started": started} if started else None)


QC, AL, VC = ("qc", "Quality control"), ("align", "Alignment"), ("variants", "Variant calling")
EXPECTED = {"qc": 10.0, "align": 100.0, "variants": 50.0}


def aligning():
    s = sample("running", [(*QC, "done", 10.0), (*AL, "running", None), (*VC, "pending", None)])
    s.stages[0].finished = T0 + 10  # QC ended 10 s after the run started: alignment began then
    return s


A0, A1 = 10 / 160, 110 / 160  # alignment's span with EXPECTED: 10 + 100 + 50 s


def test_stage_spans_follow_expected_time_so_the_pace_is_steady():
    p = pm.progress(aligning(), EXPECTED, now=T0 + 20)
    assert [(t.start, t.end) for t in p.ticks] == [(0, A0), (A0, A1), (A1, 1)]
    assert p.tick_xs == [62, 688] and [round(t.mid, 4) for t in p.ticks] == [0.0312, 0.375, 0.8438]


def test_the_running_stage_grows_with_time_and_never_reaches_its_tick():
    values = [pm.progress(aligning(), EXPECTED, now=T0 + 10 + s).value for s in (0, 25, 50, 75, 99, 500, 10_000)]
    assert values == sorted(values)  # monotonic
    assert values[2] == pytest.approx(A0 + 0.5 * (A1 - A0))  # halfway through its expected time: halfway along
    assert max(values) <= A0 + pm.CAP * (A1 - A0) < A1  # an overrunning stage slows towards the cap, short of the tick


def test_share_is_linear_then_eases_without_a_kink_and_matches_progress_js():
    xs = [0, 0.4, 0.8, 0.81, 1.0, 2.0, 50.0]
    got = [pm.share(x) for x in xs]
    assert got[:3] == [pm.MIN_SHOWN, 0.4, 0.8] and got == sorted(got) and got[-1] <= pm.CAP
    assert (pm.share(0.8001) - pm.share(0.8)) / 0.0001 == pytest.approx(1, abs=0.01)  # slope 1 at the knee
    js_path = os.path.join(os.path.dirname(CSS), "..", "..", "static", "samples", "progress.js")
    with open(js_path, encoding="utf-8") as fh:
        js = fh.read()
    assert "(x <= 0.8 ? Math.max(0.04, x) : 0.8 + 0.17 * (1 - Math.exp(-(x - 0.8) / 0.17)))" in js
    assert (pm.KNEE, round(pm.CAP - pm.KNEE, 2), pm.MIN_SHOWN) == (0.8, 0.17, 0.04)


def test_time_left_counts_the_stages_still_to_come():
    p = pm.progress(aligning(), EXPECTED, now=T0 + 10 + 40)
    assert p.seconds_left == 60 + 50 and p.current == "Alignment"
    assert p.valuetext == "Aligning, stage 2 of 3, about 2 min left"


def test_done_fills_and_failed_stops_at_the_failed_stage():
    done = pm.progress(sample("succeeded", [(*QC, "done", 1), (*AL, "done", 1), (*VC, "done", 1)]), EXPECTED)
    assert (done.value, done.fail_at, done.valuetext) == (1.0, None, "Complete, all stages done")
    failed = pm.progress(sample("failed", [(*QC, "done", 1), (*AL, "failed", None), (*VC, "pending", None)]), EXPECTED)
    assert failed.value == failed.fail_at == pytest.approx(A0) and failed.valuetext == "Failed at Alignment"


def test_queued_is_empty_and_a_cloud_run_without_a_start_shows_a_sliver_without_an_estimate():
    assert pm.progress(sample("queued", [(*QC, "pending", None)]), EXPECTED).value == 0
    cloud = pm.progress(sample("running", [(*QC, "running", None), (*AL, "pending", None)], started=None), EXPECTED)
    assert cloud.value == pytest.approx(pm.MIN_SHOWN * 10 / 110) and cloud.seconds_left is None
    assert cloud.valuetext == "Checking quality, stage 1 of 2"


def test_expected_times_are_the_median_of_earlier_successful_runs():
    metrics = [{"task": "Align", "status": "Success", "duration_seconds": s} for s in (10, 30, 20)] + [
        {"task": "Align", "status": "Failure", "duration_seconds": 999}
    ]
    got = pm.expected_durations(metrics)
    assert got["align"] == 20 and got["qc"] == pm.DEFAULT_SECONDS["qc"]


@pytest.mark.parametrize("state", ["running", "succeeded", "failed", "queued"])
def test_the_bar_renders_as_an_accessible_progressbar(state):
    stages = {
        "running": aligning(),
        "succeeded": sample("succeeded", [(*QC, "done", 1), (*AL, "done", 1), (*VC, "done", 1)]),
        "failed": sample("failed", [(*QC, "done", 1), (*AL, "failed", None), (*VC, "pending", None)]),
        "queued": sample("queued", [(*QC, "pending", None), (*AL, "pending", None), (*VC, "pending", None)]),
    }[state]
    p = pm.progress(stages, EXPECTED, now=T0 + 60)
    html = render_to_string(
        "samples/_progress.html",
        {
            "p": p,
            "helix": pm.HELIX,
            "turbo": [(0, "#000000"), (1, "#ffffff")],
            "turbo_dark": [(0, "#000"), (1, "#fff")],
        },
    )
    assert f'class="hp {p.state}" role="progressbar"' in html and f'aria-valuenow="{p.percent}"' in html
    assert f'aria-valuetext="{p.valuetext}"' in html and html.count("<li ") == 3
    assert f'<rect class="hp-clip" x="0" y="-10" width="{p.width}" height="60"/>' in html  # the drawn length
    assert ('class="hp-final"' in html) == (state in ("succeeded", "failed"))  # the green or red layer
    assert ('class="hp-break"' in html) == (state == "failed")  # the helix breaks where the run failed
    assert "style=" not in html  # the hosted CSP allows no inline style


def test_only_a_running_bar_flows_and_reduced_motion_stops_it():
    here = os.path.dirname(CSS)
    with open(os.path.join(here, "_progress.css"), encoding="utf-8") as fh:
        css = fh.read()
    assert re.findall(r"([^{}\n]+)\{animation:hp-flow", css) == [".hp.running .hp-spin"]
    assert ".hp.spinning .hp-spin{animation:hp-spin" in css and ".hp.snapping .hp-body{animation:hp-snap" in css
    assert ".hp.fading .hp-final{animation:hp-in .6s ease-out!important}" in css  # reduced motion: a fade only
    with open(CSS, encoding="utf-8") as fh:
        assert "@media (prefers-reduced-motion:reduce){*,::before,::after{animation:none!important" in fh.read()


@pytest.mark.parametrize("name", ["light", "dark", "retro", "contrast"])
def test_done_and_failed_colours_stand_out_from_the_card(name):
    t = theme(name)
    assert contrast(t["done"], t["card"]) >= 3 and contrast(t["fail"], t["card"]) >= 3


def test_the_sample_page_shows_the_bar_and_no_per_stage_helices(client, storage):
    from .web_helpers import fastq_gz, put

    put(storage, "raw_reads/S1.fastq.gz", fastq_gz())
    page = client.get("/samples/S1/").content.decode()
    assert page.count('role="progressbar"') == 1 and 'class="hp-run"' in page and 'data-key="S1"' in page
    assert "<symbol" not in page and 'class="dna' not in page
    assert "<symbol" not in client.get("/").content.decode()


# ------------------------------------------------------------------ the debug-only preview (/demo/progress/)


def test_the_preview_is_404_without_debug(client, settings):
    settings.DEBUG = False
    assert client.get("/demo/progress/").status_code == 404
    assert client.get("/demo/progress/frame/?start=1").status_code == 404


def test_the_preview_polls_the_real_component_with_debug(client, settings):
    settings.DEBUG = True
    page = client.get("/demo/progress/").content.decode()
    assert 'role="progressbar"' in page and 'data-poll="/demo/progress/frame/?start=' in page
    frame = client.get("/demo/progress/frame/?start=1").content.decode()
    assert frame.startswith('<section class="panel" id="progress-demo" data-poll=')  # geyser.js swaps this in


def test_the_simulation_loops_running_done_failed():
    seen = []
    for t in range(0, 240, 2):
        sample, expected = pm.demo_sample(t, now=T0 + t)
        p = pm.progress(sample, expected, now=T0 + t)
        if not seen or seen[-1] != p.state:
            seen.append(p.state)
    assert seen[:5] == ["running", "succeeded", "running", "failed", "running"]


def test_publish_never_writes_the_preview(storage, tmp_path):
    from samples import publish as publish_mod

    from .web_helpers import fastq_gz, put

    put(storage, "raw_reads/S1.fastq.gz", fastq_gz())
    out = tmp_path / "site"
    publish_mod.publish(everything=True, out=str(out), log=lambda m: None)
    assert not (out / "demo").exists()


def test_the_running_stage_starts_when_the_last_one_finished_and_the_bar_carries_its_clock():
    s = sample("running", [(*QC, "done", 10.0), (*AL, "running", None), (*VC, "pending", None)])
    s.stages[0].finished = T0 + 300  # a preview delay, a queue: the stage ended well after started + duration
    p = pm.progress(s, EXPECTED, now=T0 + 350)
    assert p.stage_started == T0 + 300 and p.value == pytest.approx(A0 + 0.5 * (A1 - A0))  # 50 s of 100: halfway
    assert (p.expected, p.later, p.span, p.now, p.lead) == (
        100.0,
        50.0,
        (A0, A1),
        T0 + 350,
        "Aligning, stage 2 of 3",
    )
    html = render_to_string("samples/_progress.html", {"p": p, "helix": pm.HELIX, "turbo": [], "turbo_dark": []})
    assert f'data-stage-start="{T0 + 300:.3f}"' in html and 'data-expected="100.000"' in html
    assert 'data-from="0.06250" data-to="0.68750"' in html and 'data-lead="Aligning, stage 2 of 3"' in html
    done = pm.progress(sample("succeeded", [(*QC, "done", 1)]), EXPECTED)
    assert "data-stage-start" not in render_to_string(
        "samples/_progress.html", {"p": done, "helix": pm.HELIX, "turbo": [], "turbo_dark": []}
    )  # nothing to grow once it's over
