"""One progress figure for a run: how far along it is, where it stands, and roughly how long is left.

The sample page draws it as a single double helix across the stages (samples/_progress.html). Each stage gets an
equal share of the bar, so its tick and label sit at fixed places (no inline positions: the hosted CSP allows no
style attributes). Within the running stage the helix grows with the stage's elapsed time over its expected time,
but never reaches the next tick until the stage is done; a done run fills the bar, and a failed run stops at the
start of the stage that failed.

Expected stage times are the median of this site's earlier successful runs of that stage (the local metrics), else a
default. The running stage's start is known for a local run (the worker records when the run started; stages run one
after another); in the cloud it isn't (a stage writes its run-record part when it ends), so there the bar shows the
stage under way without an estimate.
"""

import statistics
import time
from dataclasses import dataclass, field

from . import status as status_mod

DEFAULT_SECONDS = {"qc": 60.0, "align": 300.0, "variants": 180.0}  # a small sample on a laptop or Fargate
VERBS = {"qc": "Checking quality", "align": "Aligning", "variants": "Calling variants"}
CAP = 0.95  # the running stage never draws past 95% of its share: the next tick means "done"
MIN_SHOWN = 0.04  # a stage that has just started shows a sliver, so the bar visibly moves on


@dataclass
class Tick:
    name: str
    label: str
    state: str  # done | running | failed | pending
    start: float  # 0..1 along the bar
    end: float


@dataclass
class Progress:
    state: str  # queued | running | succeeded | failed
    value: float  # 0..1: the drawn length
    ticks: list = field(default_factory=list)
    current: str = ""  # the running (or failed) stage's label
    seconds_left: int | None = None
    fail_at: float | None = None  # where the helix breaks (the failed stage's start)
    valuetext: str = ""
    # For geyser.js to keep growing the bar between polls (running, with a known stage start only):
    stage_started: float | None = None  # epoch seconds
    expected: float | None = None  # the running stage's expected seconds
    later: float = 0.0  # expected seconds of the stages after it
    span: tuple = (0.0, 0.0)  # the running stage's share of the bar (start, end), 0..1
    now: float | None = None  # the server's clock when rendered (the browser corrects for its own)
    lead: str = ""  # "Aligning, stage 2 of 3": the text before the time left

    @property
    def percent(self) -> int:
        return round(self.value * 100)

    @property
    def width(self) -> int:
        """The drawn length in the SVG's 1000-unit viewBox."""
        return round(self.value * 1000)

    @property
    def tick_xs(self) -> list:
        return [round(t.end * 1000) for t in self.ticks[:-1]]


def expected_durations(metrics=None) -> dict:
    """{stage: seconds}: the median Duration of earlier successful runs per stage, else DEFAULT_SECONDS."""
    by_metric = {st.metric: st.name for st in status_mod.STAGES}
    seen = {}
    for rec in metrics or []:
        name = by_metric.get(rec.get("task") or rec.get("metric") or rec.get("TaskName"))
        seconds = rec.get("duration_seconds")
        if name and seconds and (rec.get("status") in (None, "Success")):
            seen.setdefault(name, []).append(float(seconds))
    return {
        name: statistics.median(seen[name]) if seen.get(name) else DEFAULT_SECONDS[name] for name in DEFAULT_SECONDS
    }


def _say(seconds: float) -> str:
    if seconds < 90:
        return f"about {max(5, round(seconds / 5) * 5)} s left"
    if seconds < 3600:
        return f"about {round(seconds / 60)} min left"
    return f"about {seconds / 3600:.1f} h left"


def progress(sample, expected=None, now=None) -> Progress:
    """The run's progress for the bar (pure: everything it needs comes in)."""
    expected = {**DEFAULT_SECONDS, **(expected or {})}
    now = time.time() if now is None else now
    stages = sample.stages
    n = len(stages) or 1
    ticks = [Tick(s.name, s.label, s.state, i / n, (i + 1) / n) for i, s in enumerate(stages)]
    done = [t for t in ticks if t.state == "done"]

    if sample.state == "succeeded":
        return Progress("succeeded", 1.0, ticks, valuetext="Complete, all stages done")
    failed = next((t for t in ticks if t.state == "failed"), None)
    if sample.state == "failed":
        at = failed.start if failed else (max((t.end for t in done), default=0.0))
        label = failed.label if failed else ""
        text = f"Failed at {label}" if label else "Failed"
        return Progress("failed", at, ticks, current=label, fail_at=at, valuetext=text)
    if sample.state == "queued":
        return Progress("queued", 0.0, ticks, valuetext="Queued, waiting to start")

    running = [t for t in ticks if t.state == "running"]
    if not running:  # under way, but no stage reports running yet
        value = max((t.end for t in ticks if t.state == "done"), default=0.0)
        return Progress("running", value, ticks, valuetext="Starting")
    cur = running[0]
    # The running stage's start: local runs go one stage at a time from the worker's start.
    run = sample.run or {}
    # Local runs go one stage at a time: the running stage began when the last one finished (or the run started).
    marks = [s.finished for s in stages if s.state == "done" and s.finished] + [run.get("started")]
    started = max((m for m in marks if m), default=None)
    elapsed = None if started is None else max(0.0, now - started)
    exp = expected.get(cur.name, 60.0)
    share = MIN_SHOWN if elapsed is None else min(CAP, max(MIN_SHOWN, elapsed / exp))
    value = cur.start + share * (cur.end - cur.start)
    later = sum(expected.get(t.name, 60.0) for t in ticks if t.state == "pending")
    seconds_left = None if elapsed is None else round(max(0.0, exp - elapsed) + later)
    index = ticks.index(cur) + 1
    verb = VERBS.get(cur.name, cur.label)
    lead = f"{verb}, stage {index} of {len(ticks)}"
    text = lead + (f", {_say(seconds_left)}" if seconds_left is not None else "")
    return Progress(
        "running",
        value,
        ticks,
        current=cur.label,
        seconds_left=seconds_left,
        valuetext=text,
        stage_started=started if elapsed is not None else None,
        expected=exp,
        later=later,
        span=(cur.start, cur.end),
        now=now,
        lead=lead,
    )


# --- The helix drawn by samples/_progress.html: a 1000-unit-wide viewBox stretched to the page width (the strokes
# don't scale: vector-effect), one period every 40 units, from -80 to 1080 so the flow animation (a translate by
# one period) never shows an end. Built once.
PERIOD, AMPLITUDE, MID = 40, 11, 20


def _strand(sign: int) -> str:
    import math

    points = []
    for x in range(-80, 1081, 4):
        points.append(f"{x} {MID + sign * AMPLITUDE * math.sin(2 * math.pi * x / PERIOD):.1f}")
    return "M" + " L".join(points)


def _rungs() -> str:
    import math

    out = []
    for x in range(-80, 1081, 5):
        s = abs(math.sin(2 * math.pi * x / PERIOD))
        if s > 0.3:
            out.append(f"M{x} {MID - AMPLITUDE * s:.1f}V{MID + AMPLITUDE * s:.1f}")
    return "".join(out)


HELIX = {"a": _strand(1), "b": _strand(-1), "rungs": _rungs()}


# --- A simulated run for the debug-only preview (/demo/progress/, views.progress_demo): the real component, driven
# by a fake clock, so the bar can be watched for a whole run (a real demo sample finishes in seconds).
DEMO_STAGES = (
    ("qc", "Quality control", 8.0),
    ("align", "Alignment", 20.0),
    ("variants", "Variant calling", 14.0),
    ("gate", "Quality gate", 4.0),
    ("annotate", "Annotation", 10.0),
    ("publish", "Publishing", 6.0),
)
DEMO_HOLD = 10.0  # seconds the finished bar stays (green, then red) before the next run
DEMO_FAIL_AT = "variants"  # the second run fails here
VERBS.update({"gate": "Checking the quality gate", "annotate": "Annotating", "publish": "Publishing"})


def demo_sample(elapsed: float, now: float):
    """(SampleView, expected) for the simulation at `elapsed` seconds into its loop: a run that succeeds, a hold, a run
    that fails at DEMO_FAIL_AT, a hold, then again."""
    from .status import SampleView, StageView

    total = sum(s for _, _, s in DEMO_STAGES)
    fail_start = sum(s for n, _, s in DEMO_STAGES[: [n for n, _, _ in DEMO_STAGES].index(DEMO_FAIL_AT)])
    fail_end = fail_start + 0.4 * dict((n, s) for n, _, s in DEMO_STAGES)[DEMO_FAIL_AT]
    loop = total + DEMO_HOLD + fail_end + DEMO_HOLD
    t = elapsed % loop
    failing = t >= total + DEMO_HOLD
    if failing:
        t -= total + DEMO_HOLD
    stages, start, state = [], 0.0, "running"
    for name, label, seconds in DEMO_STAGES:
        end = start + seconds
        if failing and name == DEMO_FAIL_AT and t >= fail_end:
            stages.append(StageView(name, label, "failed", None))
            state = "failed"
        elif state == "failed":
            stages.append(StageView(name, label, "pending", None))
        elif t >= end:
            stages.append(StageView(name, label, "done", seconds, now - t + end))
        elif t >= start:
            stages.append(StageView(name, label, "running", None))
        else:
            stages.append(StageView(name, label, "pending", None))
        start = end
    if state != "failed" and all(s.state == "done" for s in stages):
        state = "succeeded"
    expected = {n: s for n, _, s in DEMO_STAGES}
    return SampleView("DEMO", state, stages, run={"started": now - t}), expected
