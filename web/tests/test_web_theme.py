"""The three themes' colour tokens (web/samples/templates/samples/_site.css) meet WCAG contrast.

Read from the stylesheet itself: :root is the light theme, and :root[data-theme=dark|contrast] override it. A token
change that drops a pair below its target fails here, not in a review. Targets: 4.5:1 (AA) in light and dark, 7:1
(AAA) in retro and high contrast.
"""

import os
import re

import pytest

CSS = os.path.join(os.path.dirname(__file__), "..", "samples", "templates", "samples", "_site.css")
TARGET = {"light": 4.5, "dark": 4.5, "retro": 7.0, "contrast": 7.0}
# Each theme's lockup wordmark colours (static/samples/geyser-lockup[-dark|-contrast].svg), on the header (--card)
LOGO = {"light": ("#174ea6",), "retro": ("#000000",), "dark": ("#8ab4f8", "#e3e3e3"), "contrast": ("#ffffff",)}


def _block(css: str, selector: str) -> dict:
    body = re.search(re.escape(selector) + r"\{(.*?)\}", css, re.S).group(1)
    return dict(re.findall(r"--([\w-]+):([^;}]+)", body))


def theme(name: str) -> dict:
    with open(CSS, encoding="utf-8") as fh:
        css = fh.read()
    tokens = _block(css, ":root")
    if name != "light":
        tokens.update(_block(css, f":root[data-theme={name}]"))

    def value(token):
        v = tokens[token].strip()
        ref = re.fullmatch(r"var\(--([\w-]+)\)", v)
        return value(ref.group(1)) if ref else v

    return {k: value(k) for k in tokens if re.fullmatch(r"#[0-9a-fA-F]{3,6}|var\(--[\w-]+\)", tokens[k].strip())}


def luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    channels = [int(h[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


THEMES = list(TARGET)
SURFACES = ("bg", "card", "deep", "zebra")
TEXT = ("text", "muted", "link", "done", "run", "fail", "warn", "queue", "pend")


def pairs(t: dict, name: str) -> list:
    """(label, foreground, background) for every text-on-background use of the tokens."""
    out = [(f"--{fg} on --{bg}", t[fg], t[bg]) for fg in TEXT for bg in SURFACES]
    out += [(f"--{s} chip", t[s], t[f"{s}-bg"]) for s in ("done", "run", "fail", "warn", "queue", "pend")]
    out += [("button", t["on-primary"], t["primary"]), ("theme switch, pressed", t["on-primary"], t["primary"])]
    out += [("text on row hover", t["text"], t["hover"]), ("link on row hover", t["link"], t["hover"])]
    out += [("Samples pill", t["run"], t["run-bg"]), ("Samples pill, hover", t["tube-hover-text"], t["tube-hover"])]
    out += [(f"done pill on --stage-{s}", t["on"], t[f"stage-{s}"]) for s in ("qc", "align", "variants")]
    out += [(f"base {b} on --{bg}", t[f"nt-{b}"], t[bg]) for b in "acgt" for bg in ("card", "zebra")]
    out += [("lockup wordmark", colour, t["card"]) for colour in LOGO[name]]
    if "nt-a-bg" in t:  # retro's base chips: a black letter on each tinted fill
        out += [(f"base {b} chip", t[f"nt-{b}"], t[f"nt-{b}-bg"]) for b in "acgt"]
    return out


def test_light_is_light_and_dark_is_dark():
    assert luminance(theme("light")["bg"]) > 0.85 and luminance(theme("dark")["bg"]) < 0.02
    retro, hc = theme("retro"), theme("contrast")
    assert (retro["text"], retro["bg"], retro["card"], retro["deep"]) == ("#000", "#fff", "#fff", "#fff")  # no tints
    assert (hc["text"], hc["bg"], hc["card"], hc["deep"]) == ("#fff", "#000", "#000", "#000")  # white on black
    assert (hc["link"], hc["run"]) == ("#ffd600", "#00e5ff")  # yellow links and focus, cyan accents


@pytest.mark.parametrize("name", THEMES)
def test_every_pair_meets_the_themes_target(name):
    t = theme(name)
    failing = [
        (label, fg, bg, round(contrast(fg, bg), 2))
        for label, fg, bg in pairs(t, name)
        if contrast(fg, bg) < TARGET[name]
    ]
    assert failing == [], f"{name}: below {TARGET[name]}:1"


def test_every_theme_sets_the_same_tokens():
    # A token only one theme defines would fall back to light's value in the others: a light colour on dark.
    with open(CSS, encoding="utf-8") as fh:
        css = fh.read()
    dark = _block(css, ":root[data-theme=dark]")
    colours = {k for k, v in _block(css, ":root").items() if v.startswith("#")} - {"blue", "red", "yellow", "green"}
    assert colours - set(dark) - {"on"} == set()  # dark keeps the brand quartet and the dark label on the pills
    for name in ("retro", "contrast"):
        assert colours - set(_block(css, f":root[data-theme={name}]")) == set(), name


def test_retro_base_chips_are_four_distinct_tints_with_their_own_edges():
    t = theme("retro")
    fills = [t[f"nt-{b}-bg"] for b in "acgt"]
    lines = [t[f"nt-{b}-line"] for b in "acgt"]
    assert len(set(fills)) == 4 and len(set(lines)) == 4
    # each edge stands out from its fill, so a chip reads as a chip
    assert all(contrast(line, fill) >= 3 for line, fill in zip(lines, fills, strict=True))
