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


ICONS = {
    "start": '<path d="M8 5v14l11-7Z"/>', "end": '<rect x="6" y="6" width="12" height="12" rx="1"/>', "co": '<path d="M4 7h13l-3-3M20 17H7l3 3"/>',
    "maint": '<path d="M14 6l4 4-8 8H6v-4zM12 8l4 4"/>', "camera": '<path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/>',
    "draw": '<path d="M5 5h14v14H5zM9 9h6M9 13h6"/>', "qc": '<circle cx="11" cy="11" r="6"/><path d="m20 20-4.5-4.5M8.5 11l2 2 3.5-4"/>',
    "rolls": '<ellipse cx="8" cy="12" rx="3" ry="6"/><path d="M8 6h9a3 6 0 0 1 0 12H8"/>', "notes": '<path d="M6 4h9l3 3v13H6zM9 10h6M9 14h6"/>',
    "down": '<circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/>', "menu": '<path d="M4 7h16M4 12h16M4 17h16"/>',
}


@register.simple_tag
def icon(name):
    from django.utils.safestring import mark_safe
    return mark_safe('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
                     f'stroke-linejoin="round" aria-hidden="true">{ICONS.get(name, "")}</svg>')


@register.filter
def hm(minutes):
    """90 -> 1:30"""
    try:
        m = max(0, int(round(float(minutes))))
    except (TypeError, ValueError):
        return ""
    return f"{m // 60}:{m % 60:02d}"


@register.filter
def day(dt):
    """11/3/2026 (plant date)."""
    if not dt:
        return ""
    t = timezone.localtime(dt) if timezone.is_aware(dt) else dt
    return f"{t.month}/{t.day}/{t.year}"


@register.filter
def plan(t, ref):
    """When something is planned: "2:30 pm" today, "Tue 2:30 pm" this week, else "4/9 2:30 pm"."""
    from hmi.kscreen import plan_label
    if isinstance(t, (int, float)):
        from datetime import datetime
        t = datetime.fromtimestamp(t / 1000, timezone.get_current_timezone())
    return plan_label(t, ref)


@register.filter
def ms(dt):
    """Milliseconds since 1970, for clocks and timers the page ticks."""
    from plant.util import ms as to_ms
    return to_ms(dt) if dt else ""
