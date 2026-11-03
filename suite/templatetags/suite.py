"""Display helpers for the server-built screens."""
from django import template
from django.utils import timezone

register = template.Library()


@register.filter
def clock(dt):
    """2:05 pm (plant time)."""
    if not dt:
        return ""
    t = timezone.localtime(dt) if timezone.is_aware(dt) else dt
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'am' if t.hour < 12 else 'pm'}"


@register.filter
def num(v):
    """1,234 for counts; blanks stay blank."""
    if v is None or v == "":
        return ""
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return v
