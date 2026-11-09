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
    started = run.get("started")
    elapsed = None
    if started:
        elapsed = max(0.0, now - started - sum(s.duration or 0 for s in stages if s.state == "done"))
    exp = expected.get(cur.name, 60.0)
    share = MIN_SHOWN if elapsed is None else min(CAP, max(MIN_SHOWN, elapsed / exp))
    value = cur.start + share * (cur.end - cur.start)
    later = sum(expected.get(t.name, 60.0) for t in ticks if t.state == "pending")
    seconds_left = None if elapsed is None else round(max(0.0, exp - elapsed) + later)
    index = ticks.index(cur) + 1
    verb = VERBS.get(cur.name, cur.label)
    text = f"{verb}, stage {index} of {len(ticks)}"
    if seconds_left is not None:
        text += f", {_say(seconds_left)}"
    return Progress("running", value, ticks, current=cur.label, seconds_left=seconds_left, valuetext=text)


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
