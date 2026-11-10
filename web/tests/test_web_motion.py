"""The header logo's eruption: the lockup <img>s are the empty geyser (cone and wordmark), the inline burst
(_logo_burst.html) the jets, droplets and helices; _theme_boot.js arms it (logo-armed) and erupts it (logo-erupt)."""

import gzip
import importlib.util
import os
import re

from django.conf import settings

TEMPLATES = os.path.join(settings.BASE_DIR, "samples", "templates", "samples")
STATIC = os.path.join(settings.BASE_DIR, "samples", "static", "samples")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def _builder():
    path = os.path.join(settings.BASE_DIR, "..", "scripts", "build_logo_burst.py")
    spec = importlib.util.spec_from_file_location("build_logo_burst", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_page_shows_the_empty_lockups_and_the_burst_over_them(client, storage):
    body = client.get("/").content.decode()
    brand = re.search(r'<a class="brand".*?</a>', body, re.S).group(0)
    assert len(re.findall(r'<img [^>]*src="[^"]*geyser-lockup-empty[^"]*"', brand)) == 4  # one per theme
    svg = re.search(r'<svg class="burst".*?</svg>', brand, re.S).group(0)
    assert 'aria-hidden="true"' in svg and 'focusable="false"' in svg  # decorative: the <img>s carry the name
    assert "<style" not in svg and "<script" not in svg and "style=" not in svg  # CSP: markup only
    assert len(gzip.compress(svg.encode())) <= 1500
    # No eruption class in the HTML: without the boot script the logo is simply full (img + burst at rest).
    assert "logo-" not in re.search(r"<html[^>]*>", body).group(0)


def test_the_burst_and_the_empty_lockups_are_the_lockups_split():
    # The generated files are what the script makes from the committed lockups (rerun it after build_logo.py).
    b = _builder()
    lockups = {
        t: _read(STATIC, f"geyser-lockup{s}.svg")
        for t, s in (("light", ""), ("dark", "-dark"), ("retro", "-retro"), ("contrast", "-contrast"))
    }
    light = lockups["light"]
    assert b.burst(light) in _read(TEMPLATES, "_logo_burst.html")
    art = _read(STATIC, "geyser-burst.svg")
    assert art == b.art(lockups)
    for theme, svg in lockups.items():
        jets, drops, uses, empty = b.parts(svg)
        suffix = "" if theme == "light" else f"-{theme}"
        assert _read(STATIC, f"geyser-lockup-empty{suffix}.svg") == empty
        assert "jetGrad" not in empty and "<use " not in empty and "helix-" not in empty
        # The theme's own jets and jet colours are in the art file, by id.
        grad = re.search(rf'<linearGradient id="jet-{theme}".*?</linearGradient>', art).group(0)
        assert re.findall(r'stop-color="(#[0-9A-Fa-f]{6})"', grad) == b.jet_stops(svg)
        assert f'id="jets-{theme}" fill="url(#jet-{theme})"' in art
        assert len(uses) == 5 and len(re.findall(r"<circle ", drops)) == len(re.findall(r"<circle ", art))
    # The art file draws nothing on its own: everything is in <defs>, used by id from the page.
    assert re.fullmatch(r"<svg [^>]*>(<!--.*?-->)?<defs>.*</defs></svg>\n", art, re.S)
    css = _read(TEMPLATES, "_site.css")
    assert ":root[data-theme=dark] .jt-dark" in css  # one jets <use> per theme, CSS shows the theme's
    # The CSS moves each helix from the vent: its --x/--y are the script's offsets.
    for k, (x, y) in enumerate(b.offsets(light)):
        assert re.search(rf"\.h{k}\{{--x:{x}px;--y:{y}px", css), (k, x, y)


def test_the_eruption_css_is_small_and_transform_opacity_only():
    css = _read(TEMPLATES, "_site.css")
    start, end = css.index(".brand{position:relative}"), css.index("@media (prefers-reduced-motion:reduce)")
    block = css[start:end]
    assert len(block.encode()) <= 2 * 1024  # the global sheet's budget for it
    frames = "".join(re.findall(r"@keyframes (?:jets|drops|pop)\{(.*?\})\}", block))
    props = set(re.findall(r"([a-z-]+):", frames))
    assert props <= {"opacity", "transform"}, props  # compositor-only: no layout shift
    assert "animation:none!important" in css[end:]  # reduced motion: the global rule after it stops everything
    # Only an armed or erupting logo is ever changed: the default (no class) is the full logo.
    assert re.search(r"\.logo-armed [^{]*\{", block) and re.search(r"\.logo-erupt [^{]*\{", block)


def test_the_boot_arms_only_when_motion_is_allowed_and_not_yet_erupted():
    boot = _read(TEMPLATES, "_theme_boot.js")
    assert "sessionStorage.getItem(k)" in boot and '"geyser-erupted"' in boot  # once a session
    assert "prefers-reduced-motion: reduce" in boot  # reduced motion: never armed (the full logo)
    assert 's = "1"' in boot  # storage blocked: treated as erupted (the full logo)
    assert '"(hover: none)"' in boot  # touch screens erupt by themselves: never left empty
    code = re.sub(r"/\*.*?\*/", "", boot, flags=re.S)  # block comments are fine; line comments are not
    assert "//" not in code  # the inliner joins the lines
