"""Charts drawn on the server, so screens only display them.

speed_chart: pleats per minute over the shift, with the rated speed, hour marks and the latest reading. The plot is
an SVG stretched to its box (lines keep their width at any size) and the labels are ordinary text placed by
percentage, so the chart reads the same on a phone and on a wall screen. Every piece of text is escaped.
"""
import math
from datetime import datetime, timedelta

from django.utils import timezone
from django.utils.html import escape


def _nice_top(v):
    return max(100, int(math.ceil(v * 1.1 / 100.0)) * 100)


def hour_label(t):
    h = t.hour % 12 or 12
    return f"{h}{'am' if t.hour < 12 else 'pm'}"


def speed_chart(points, rated, empty="No readings this shift", label="Pleater speed"):
    """points: [(aware datetime, ppm)], oldest first. Returns safe HTML (str)."""
    if not points:
        return f'<div class="chartbox empty" role="img" aria-label="{escape(label)}"><span>{escape(empty)}</span></div>'
    t0 = points[0][0]
    t1 = max(points[-1][0], t0 + timedelta(hours=6))
    span = (t1 - t0).total_seconds() or 1
    top = _nice_top(max([rated or 0] + [p for _, p in points]))

    def X(t):  # 0..1000 across the plot
        return (t - t0).total_seconds() / span * 1000

    def Y(v):  # 0..100 down the plot
        return (1 - v / top) * 100
    svg = ['<svg viewBox="0 0 1000 100" preserveAspectRatio="none" aria-hidden="true">']
    for i in range(5):
        y = Y(top * i / 4)
        svg.append(f'<line class="c-grid" x1="0" x2="1000" y1="{y:.2f}" y2="{y:.2f}"/>')
    if rated:
        svg.append(f'<line class="c-rated" x1="0" x2="1000" y1="{Y(rated):.2f}" y2="{Y(rated):.2f}"/>')
    line = " ".join(f"{X(t):.1f},{Y(v):.2f}" for t, v in points)
    svg.append(f'<polygon class="c-area" points="{X(points[0][0]):.1f},100 {line} {X(points[-1][0]):.1f},100"/>')
    svg.append(f'<polyline class="c-line" points="{line}"/></svg>')
    labels = [f'<span class="c-y" style="top:{Y(top * i / 4):.2f}%">{int(round(top * i / 4))}</span>' for i in range(5)]
    tk = timezone.localtime(t0).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    while tk < t1:
        labels.append(f'<span class="c-x" style="left:{X(tk) / 10:.2f}%">{hour_label(timezone.localtime(tk))}</span>')
        tk += timedelta(hours=1)
    lt, lv = points[-1]
    dot = f'<i class="c-dot" style="left:{X(lt) / 10:.2f}%;top:{Y(lv):.2f}%"></i>'
    return (f'<div class="chartbox" role="img" aria-label="{escape(label)}: {int(lv)} pleats per minute now, rated {int(rated or 0)}">'
            f'<div class="plot">{"".join(svg)}{dot}{"".join(labels)}</div></div>')


def as_points(hist):
    """[{"t": ms, "ppm": n}] from a snapshot -> [(datetime, ppm)]."""
    tz = timezone.get_current_timezone()
    return [(datetime.fromtimestamp(h["t"] / 1000, tz), h["ppm"]) for h in hist]
