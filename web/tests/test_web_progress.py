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


def test_equal_shares_and_ticks_at_their_ends():
    p = pm.progress(aligning(), EXPECTED, now=T0 + 20)
    assert [(t.start, t.end) for t in p.ticks] == [(0, 1 / 3), (1 / 3, 2 / 3), (2 / 3, 1)]
    assert p.tick_xs == [333, 667]


def test_the_running_stage_grows_with_time_and_never_reaches_its_tick():
    values = [pm.progress(aligning(), EXPECTED, now=T0 + 10 + s).value for s in (0, 25, 50, 75, 99, 500, 10_000)]
    assert values == sorted(values)  # monotonic
    assert values[2] == pytest.approx(1 / 3 + 0.5 / 3)  # halfway through its expected time: halfway along its share
    assert max(values) == pytest.approx(1 / 3 + pm.CAP / 3) and max(values) < 2 / 3  # capped short of the tick


def test_time_left_counts_the_stages_still_to_come():
    p = pm.progress(aligning(), EXPECTED, now=T0 + 10 + 40)
    assert p.seconds_left == 60 + 50 and p.current == "Alignment"
    assert p.valuetext == "Aligning, stage 2 of 3, about 2 min left"


def test_done_fills_and_failed_stops_at_the_failed_stage():
    done = pm.progress(sample("succeeded", [(*QC, "done", 1), (*AL, "done", 1), (*VC, "done", 1)]), EXPECTED)
    assert (done.value, done.fail_at, done.valuetext) == (1.0, None, "Complete, all stages done")
    failed = pm.progress(sample("failed", [(*QC, "done", 1), (*AL, "failed", None), (*VC, "pending", None)]), EXPECTED)
    assert failed.value == failed.fail_at == pytest.approx(1 / 3) and failed.valuetext == "Failed at Alignment"


def test_queued_is_empty_and_a_cloud_run_without_a_start_shows_a_sliver_without_an_estimate():
    assert pm.progress(sample("queued", [(*QC, "pending", None)]), EXPECTED).value == 0
    cloud = pm.progress(sample("running", [(*QC, "running", None), (*AL, "pending", None)], started=None), EXPECTED)
    assert cloud.value == pytest.approx(pm.MIN_SHOWN / 2) and cloud.seconds_left is None
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
    assert f'<rect x="0" y="0" width="{p.width}"' in html  # the drawn length
    assert ('class="hp-break"' in html) == (state == "failed")  # the helix breaks where the run failed
    assert "style=" not in html  # the hosted CSP allows no inline style


def test_only_a_running_bar_flows_and_reduced_motion_stops_it():
    here = os.path.dirname(CSS)
    with open(os.path.join(here, "_progress.css"), encoding="utf-8") as fh:
        css = fh.read()
    assert re.findall(r"([^{}\n]+)\{animation:hp-flow", css) == [".hp.running .hp-flow"]
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
    assert page.count('role="progressbar"') == 1 and "hp-strand" in page
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
    assert p.stage_started == T0 + 300 and p.value == pytest.approx(1 / 3 + 0.5 / 3)  # 50 s of 100: halfway
    assert (p.expected, p.later, p.span, p.now, p.lead) == (
        100.0,
        50.0,
        (1 / 3, 2 / 3),
        T0 + 350,
        "Aligning, stage 2 of 3",
    )
    html = render_to_string("samples/_progress.html", {"p": p, "helix": pm.HELIX, "turbo": [], "turbo_dark": []})
    assert f'data-stage-start="{T0 + 300:.3f}"' in html and 'data-expected="100.000"' in html
    assert 'data-from="0.33333" data-to="0.66667"' in html and 'data-lead="Aligning, stage 2 of 3"' in html
    done = pm.progress(sample("succeeded", [(*QC, "done", 1)]), EXPECTED)
    assert "data-stage-start" not in render_to_string(
        "samples/_progress.html", {"p": done, "helix": pm.HELIX, "turbo": [], "turbo_dark": []}
    )  # nothing to grow once it's over
