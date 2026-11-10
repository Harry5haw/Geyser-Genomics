"""The header logo's eruption (scripts/build_logo_burst.py, geyser.js): generated from the lockups, SMIL only (no
style the hosted CSP would block), wired through data-erupt on the static lockup <img>s, with no inline code."""

import importlib.util
import os
import re

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..", "..")
STATIC = os.path.join(ROOT, "web", "samples", "static", "samples")
TEMPLATES = os.path.join(ROOT, "web", "samples", "templates", "samples")
THEMES = {"light": "", "dark": "-dark", "retro": "-retro", "contrast": "-contrast"}


def _read(*p):
    with open(os.path.join(*p), encoding="utf-8") as fh:
        return fh.read()


def _builder():
    spec = importlib.util.spec_from_file_location(
        "build_logo_burst", os.path.join(ROOT, "scripts", "build_logo_burst.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("theme", THEMES)
def test_the_files_are_the_lockups_split_and_animated(theme):
    # What the script makes from the committed lockups (run it again after build_logo.py).
    b = _builder()
    lockup = _read(STATIC, f"geyser-lockup{THEMES[theme]}.svg")
    empty, erupt = _read(STATIC, f"geyser-empty-{theme}.svg"), _read(STATIC, f"geyser-erupt-{theme}.svg")
    assert empty == b.parts(lockup)[3]
    assert erupt == b.erupting(lockup)
    # The empty geyser: no jets, droplets or helices (and their defs gone).
    assert "jetGrad" not in empty and "<use " not in empty and "helix-" not in empty and "<circle" not in empty
    # The animated one: every part of the lockup, so it holds the full logo at the end.
    jets, drops, uses, _ = b.parts(lockup)
    assert jets in erupt and drops in erupt
    for u in uses:
        assert re.sub(r'transform="translate\([\d.]+, [\d.]+\)\s*', 'transform="', u) in erupt


@pytest.mark.parametrize("theme", THEMES)
def test_the_animation_is_smil_once_and_holds_with_no_style(theme):
    erupt = _read(STATIC, f"geyser-erupt-{theme}.svg")
    # The hosted CSP (style-src hashes) applies inside SVG documents too: no <style>, no style attributes.
    assert "<style" not in erupt and "style=" not in erupt and "<script" not in erupt
    anims = re.findall(r"<(?:animate|animateTransform) [^>]*/>", erupt)
    assert len(anims) == 1 + 2 + 5 * 4  # jets; droplets (fade, grow); each helix (fade, move, spin, size)
    for a in anims:
        assert 'fill="freeze"' in a and "repeatCount" not in a  # once, then hold the last frame
        assert re.search(r'begin="[\d.]+s"', a)  # timed from the image's load
    ends = [
        float(re.search(r'begin="([\d.]+)s"', a).group(1)) + float(re.search(r'dur="([\d.]+)s"', a).group(1))
        for a in anims
    ]
    assert max(ends) <= 1.5
    # Each helix lands exactly in place: its move ends at 0 0, its spin at 0, its size at 1.
    assert len(re.findall(r'type="translate" values="[^"]*;0 0"', erupt)) == 5
    assert len(re.findall(r'type="rotate" values="[^"]*;0"', erupt)) == 5
    assert len(re.findall(r'type="scale" values="[^"]*;1"', erupt)) == 5 + 1  # and the droplets


def test_the_helices_rise_from_the_vent():
    b = _builder()
    moves = [d for _, d in b.offsets(_read(STATIC, "geyser-lockup.svg"))]
    assert moves == [(-4, 176), (60, 253), (-76, 256), (158, 274), (-22, 353)]
    erupt = _read(STATIC, "geyser-erupt-light.svg")
    for dx, dy in moves:
        assert f'type="translate" values="{dx} {dy};0 -40;0 12;0 -4;0 0"' in erupt


def test_the_header_picture_rests_empty_where_it_can_erupt_and_full_elsewhere():
    base = _read(TEMPLATES, "base.html")
    media = "(prefers-reduced-motion: no-preference) and (scripting: enabled)"
    # One <span> holds the four <picture>s: a single flex item in .brand, as the single visible <img> was (an
    # empty <picture> per hidden theme would add a gap each).
    span = re.search(r'<a class="brand" [^>]*><span>((?:<picture>.*?</picture>){4})</span>', base).group(1)
    for theme, suffix in THEMES.items():
        pic = re.search(rf"<picture>(<source [^>]*>)(<img class=\"logo-{theme}\" [^>]*>)</picture>", span)
        source, img = pic.groups()
        # The browser picks the empty geyser at first paint, with no script, where motion is allowed and scripts run.
        assert source == f'<source media="{media}" srcset="{{% static \'samples/geyser-empty-{theme}.svg\' %}}">'
        assert f"{{% static 'samples/geyser-lockup{suffix}.svg' %}}" in img  # no JS / reduced motion: the full logo
        assert f"data-erupt=\"{{% static 'samples/geyser-erupt-{theme}.svg' %}}\"" in img
    # No inline code for it: the CSP hashes of the inline blocks don't change.
    assert "erupt" not in _read(TEMPLATES, "_site.css") and "erupt" not in _read(TEMPLATES, "_theme_boot.js")


def test_geyser_js_only_on_interaction_and_respects_motion_and_storage():
    js = _read(STATIC, "geyser.js")
    block = js[js.index("The header logo's eruption") :]
    assert '"(prefers-reduced-motion: reduce)"' in block
    # Nothing at load or on a timer: page-load measurements (Lighthouse) see the static page.
    assert '"load"' not in block and "setTimeout" not in block and "requestAnimationFrame" not in block
    assert "sessionStorage.getItem(ERUPTED)" in block and "catch (_)" in block
    assert 'querySelectorAll("source").forEach((s) => s.remove())' in block  # the full logo once erupted
