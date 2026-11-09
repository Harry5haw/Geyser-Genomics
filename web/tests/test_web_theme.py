"""The light theme's colour tokens (web/samples/templates/samples/_site.css :root) meet WCAG AA contrast.

Read from the stylesheet itself, so a token change that drops a pair below 4.5:1 fails here, not in a review.
"""

import os
import re

import pytest

CSS = os.path.join(os.path.dirname(__file__), "..", "samples", "templates", "samples", "_site.css")
AA = 4.5  # normal text; every pair below is text (or a chip's label) on its background


def tokens() -> dict:
    with open(CSS, encoding="utf-8") as fh:
        root = re.search(r":root\{(.*?)\}", fh.read(), re.S).group(1)
    return {name: value for name, value in re.findall(r"--([\w-]+):(#[0-9a-fA-F]{3,6})\b", root)}


def literal(css_class: str) -> str:
    with open(CSS, encoding="utf-8") as fh:
        return re.search(rf"\.{re.escape(css_class)}\{{color:(#[0-9a-fA-F]{{6}})\}}", fh.read()).group(1)


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


T = tokens()
SURFACES = ("bg", "card", "deep", "zebra")
TEXT = ("text", "muted", "link", "done", "run", "fail", "warn", "queue", "pend")


def test_the_theme_is_light():
    assert luminance(T["bg"]) > 0.85 and luminance(T["card"]) > 0.95 and luminance(T["text"]) < 0.02


@pytest.mark.parametrize("fg", TEXT)
@pytest.mark.parametrize("bg", SURFACES)
def test_text_tokens_read_on_every_surface(fg, bg):
    assert contrast(T[fg], T[bg]) >= AA, f"--{fg} {T[fg]} on --{bg} {T[bg]}"


@pytest.mark.parametrize("state", ["done", "run", "fail", "warn", "queue", "pend"])
def test_status_chips_read(state):
    assert contrast(T[state], T[f"{state}-bg"]) >= AA


def test_buttons_and_table_hover():
    assert contrast("#ffffff", T["primary"]) >= AA and contrast("#ffffff", T["primary-hover"]) >= AA
    assert contrast(T["text"], T["hover"]) >= AA and contrast(T["link"], T["hover"]) >= AA


@pytest.mark.parametrize("stage", ["blue", "green", "yellow"])  # the stage colours behind a done pill's label
def test_stage_pills_read(stage):
    assert contrast(T["on"], T[stage]) >= AA


@pytest.mark.parametrize("base", ["nt-A", "nt-C", "nt-G", "nt-T"])
def test_bases_read_on_both_zebra_rows(base):
    colour = literal(base)
    assert contrast(colour, T["card"]) >= AA and contrast(colour, T["zebra"]) >= AA
