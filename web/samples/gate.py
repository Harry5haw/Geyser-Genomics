"""The quality gate's verdict for one sample, as the detail page shows it (#144, #148).

The gate stage stores qc_reports/<id>.gate.json (keys_for "gate_verdict"): "verdict" is "pass" or
"fail" with named "reasons" and per-check results, or null with an "error" when the gate couldn't give
one (not installed, timed out, crashed). The run record's "gate" entry holds the mode the stage ran in
(GATE_MODE: "warn" by default until the user decides, "fail" makes a failed verdict fail the stage).

A failed verdict in warn mode is shown as a warning, not as a failed sample: in that mode the run
itself succeeded. A missing verdict (null) is "not run", never a pass.
"""

import json
from dataclasses import dataclass, field

from geyser_web.pipeline import ObjectNotFound

MAX_VERDICT_BYTES = 1024 * 1024  # a verdict is a few KB; anything bigger isn't one

# state -> (badge text, CSS class)
STATES = {
    "pass": ("Passed", "succeeded"),
    "warn": ("Warnings", "warn"),
    "fail": ("Failed", "failed"),
    "not_run": ("Gate not run", "notrun"),
}


@dataclass
class GateView:
    state: str  # pass | warn | fail | not_run
    mode: str  # warn | fail (as the stage ran; "warn" if unknown)
    reasons: list = field(default_factory=list)
    error: str = None
    failed_checks: list = field(default_factory=list)  # [{name, value, min, max, reason}]
    config: str = None
    gate_version: str = None
    run_name: str = None

    @property
    def label(self) -> str:
        return STATES[self.state][0]

    @property
    def css(self) -> str:
        return STATES[self.state][1]


def read_gate(storage, key: str, record=None):
    """The sample's gate verdict, or None if it has none (no gate stage has run). Never raises: a
    verdict that can't be read shows as "gate not run" with the reason."""
    recorded = (record or {}).get("gate") or {}
    mode = recorded.get("mode") or "warn"
    try:
        with storage.open_read(key) as fh:
            raw = fh.read(MAX_VERDICT_BYTES + 1)
    except ObjectNotFound:
        return None
    except Exception as err:  # noqa: BLE001 - the panel is extra; never fail the sample page over it
        return GateView(state="not_run", mode=mode, error=f"could not read the verdict: {err}")
    if len(raw) > MAX_VERDICT_BYTES:
        return GateView(state="not_run", mode=mode, error="verdict file too large to show")
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except ValueError as err:
        return GateView(state="not_run", mode=mode, error=f"unreadable verdict: {err}")
    mode = data.get("mode") or mode
    common = {
        "mode": mode,
        "config": data.get("config") or recorded.get("config"),
        "gate_version": data.get("gate_version") or recorded.get("gate_version"),
        "run_name": data.get("run_name"),
    }
    verdict = data.get("verdict")
    if verdict == "pass":
        return GateView(state="pass", **common)
    if verdict == "fail":
        failed = [
            {k: c.get(k) for k in ("name", "value", "min", "max", "reason")}
            for c in data.get("checks") or []
            if c.get("status") == "fail"
        ]
        return GateView(
            state="fail" if mode == "fail" else "warn",
            reasons=list(data.get("reasons") or []),
            failed_checks=failed,
            **common,
        )
    return GateView(state="not_run", error=data.get("error") or recorded.get("error") or "no verdict", **common)
