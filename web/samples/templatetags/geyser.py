from datetime import datetime

from django import template
from django.utils import timezone

register = template.Library()


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
