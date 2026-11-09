"""The header logo's eruption (base.html .brand, _site.css): decorative, small, CSP-safe, still under reduced motion."""

import re

SITE_CSS = "samples/templates/samples/_site.css"


def _css():
    import os

    from django.conf import settings

    with open(os.path.join(settings.BASE_DIR, SITE_CSS), encoding="utf-8") as fh:
        return fh.read()


def test_the_eruption_overlay_is_decorative_small_and_attribute_only(client, storage):
    body = client.get("/").content.decode()
    brand = re.search(r'<a class="brand".*?</a>', body, re.S).group(0)
    svg = re.search(r'<svg class="erupt".*?</svg>', brand, re.S).group(0)
    assert 'aria-hidden="true"' in svg and 'focusable="false"' in svg  # never read out or focused
    assert "<style" not in svg and "<script" not in svg and "style=" not in svg  # CSP: markup only
    assert len(svg.encode()) <= 1200  # the lockup stays the <img>s; this is only the burst
    assert len(re.findall(r"<img ", brand)) == 4  # the approved lockup, one per theme, unchanged


def test_the_eruption_css_is_small_and_transform_opacity_only():
    css = _css()
    start, end = css.index(".brand{position:relative}"), css.index("@media (prefers-reduced-motion:reduce)")
    block = css[start:end]
    assert len(block.encode()) <= 2 * 1024  # the global sheet budget agreed for it
    frames = "".join(re.findall(r"@keyframes (?:erupt|vent)\{(.*?\})\}", block))
    props = set(re.findall(r"([a-z-]+):", frames))
    assert props <= {"opacity", "transform"}, props  # compositor-only: no layout shift
    # The reduced-motion rule comes after the block and stops every animation in it.
    assert end > start and "animation:none!important" in css[end:]
