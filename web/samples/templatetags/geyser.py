import re
from datetime import datetime
from functools import lru_cache

from django import template
from django.conf import settings
from django.template.loader import get_template
from django.utils import timezone
from django.utils.safestring import mark_safe

register = template.Library()

_CSS_COMMENT = re.compile(r"/[*].*?[*]/", re.S)


def _minified_css(name: str) -> str:
    """A template-directory CSS file without comments or line breaks (the source keeps both)."""
    source = get_template(name).template.source
    return " ".join(line.strip() for line in _CSS_COMMENT.sub("", source).splitlines() if line.strip())


_cached_css = lru_cache(maxsize=8)(_minified_css)


@register.simple_tag
def inline_css(name):
    """The stylesheet itself, for a <style> element: every page carries its CSS, so the first paint
    waits for no stylesheet request and nothing shifts when one arrives. Read once per process, except
    with DEBUG on, where an edit shows on the next reload."""
    return mark_safe(_minified_css(name) if settings.DEBUG else _cached_css(name))  # noqa: S308 - our own file


@register.simple_tag
def inline_js(name):
    """A small script for an inline <script> (the theme boot, samples/_theme_boot.js): run before the first
    paint without a request. Minified like the CSS, so the file has no // comments. The publisher puts its sha256
    in csp-hashes.json, as for every inline block."""
    return mark_safe(_minified_css(name) if settings.DEBUG else _cached_css(name))  # noqa: S308 - our own file


@register.filter
def duration(seconds):
    """12.3 -> '12.3 s', 125 -> '2 m 05 s'."""
    if seconds is None or seconds == "":
        return "–"
    seconds = float(seconds)
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, secs = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes} m {secs:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} m"


@register.filter
def from_epoch(ts):
    """Unix timestamp -> aware datetime (use with |date or |timesince)."""
    if not ts:
        return None
    return datetime.fromtimestamp(float(ts), tz=timezone.get_current_timezone())


@register.filter
def call_key(variant):
    return (variant.pos, variant.ref, variant.alt)


@register.filter
def status_url(sample_id, static):
    """The web API's run status for a sample on the static site ("" with no API)."""
    return static.status_url(sample_id) if static else ""
