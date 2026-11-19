"""Charts drawn on the server, so screens only display them.

speed_chart: pleats per minute over the shift, with the rated speed, a line at every hour and a dotted line at every
half hour, and the latest reading. The plot is
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


def speed_chart(points, rated, empty="No readings this shift", label="Pleater speed", start=None, end=None, now=None, marks=(), shade=None):
    """points: [(aware datetime, ppm)], oldest first. Returns safe HTML (str).

    start/end fix the time axis (a whole shift, or a whole day) instead of fitting it to the readings; `now` draws a
    line at the current time; `marks` are [(datetime, label)] lines such as shift starts; `shade` is a (from, to)
    stretch drawn dimmer (the previous shift). Gaps of more than 5 minutes between readings break the line."""
    if start and end:
        points = [(t, v) for t, v in points if start <= t <= end]
    if not points and not (start and end):
        return f'<div class="chartbox empty" role="img" aria-label="{escape(label)}"><span>{escape(empty)}</span></div>'
    t0 = start or points[0][0]
    t1 = end or max(points[-1][0], t0 + timedelta(hours=6))
    span = (t1 - t0).total_seconds() or 1
    top = _nice_top(max([rated or 0] + [p for _, p in points]))

    def X(t):  # 0..1000 across the plot
        return min(1000.0, max(0.0, (t - t0).total_seconds() / span * 1000))

    def Y(v):  # 0..100 down the plot
        return (1 - v / top) * 100
    svg = ['<svg viewBox="0 0 1000 100" preserveAspectRatio="none" aria-hidden="true">']
    if shade and shade[1] > t0:
        svg.append(f'<rect class="c-shade" x="{X(shade[0]):.1f}" y="0" width="{X(shade[1]) - X(shade[0]):.1f}" height="100"/>')
    for i in range(5):
        y = Y(top * i / 4)
        svg.append(f'<line class="c-grid" x1="0" x2="1000" y1="{y:.2f}" y2="{y:.2f}"/>')
    # a line at every hour and a dotted line at every half hour
    tg = timezone.localtime(t0).replace(minute=0, second=0, microsecond=0)
    if tg < t0:
        tg += timedelta(minutes=30)
    while tg < t1:
        if tg > t0:
            cls = "c-hour" if timezone.localtime(tg).minute == 0 else "c-half"
            svg.append(f'<line class="{cls}" x1="{X(tg):.1f}" x2="{X(tg):.1f}" y1="0" y2="100"/>')
        tg += timedelta(minutes=30)
    if rated:
        svg.append(f'<line class="c-rated" x1="0" x2="1000" y1="{Y(rated):.2f}" y2="{Y(rated):.2f}"/>')
    labels = []
    for t, text in marks:
        if t0 < t < t1:
            svg.append(f'<line class="c-mark" x1="{X(t):.1f}" x2="{X(t):.1f}" y1="0" y2="100"/>')
            right = X(t) > 880   # near the right edge: the label goes on the line's left
            labels.append(f'<span class="c-mlab{" r" if right else ""}" style="left:{X(t) / 10:.2f}%">{escape(text)}</span>')
    if now and t0 < now < t1:
        svg.append(f'<line class="c-now" x1="{X(now):.1f}" x2="{X(now):.1f}" y1="0" y2="100"/>')
    runs, cur = [], []
    for t, v in points:   # a gap with no readings (machine off, no shift) is left empty, not joined across
        if cur and (t - cur[-1][0]) > timedelta(minutes=5):
            runs.append(cur)
            cur = []
        cur.append((t, v))
    if cur:
        runs.append(cur)
    for run in runs:
        line = " ".join(f"{X(t):.1f},{Y(v):.2f}" for t, v in run)
        svg.append(f'<polygon class="c-area" points="{X(run[0][0]):.1f},100 {line} {X(run[-1][0]):.1f},100"/>')
        svg.append(f'<polyline class="c-line" points="{line}"/>')
    svg.append("</svg>")
    labels += [f'<span class="c-y" style="top:{Y(top * i / 4):.2f}%">{int(round(top * i / 4))}</span>' for i in range(5)]
    hours = span / 3600
    step = 1 if hours <= 14 else 2 if hours <= 26 else 4
    tk = timezone.localtime(t0).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    while tk.hour % step:
        tk += timedelta(hours=1)
    while tk < t1:
        labels.append(f'<span class="c-x" style="left:{X(tk) / 10:.2f}%">{hour_label(timezone.localtime(tk))}</span>')
        tk += timedelta(hours=step)
    if not points:
        return (f'<div class="chartbox" role="img" aria-label="{escape(label)}: {escape(empty)}">'
                f'<div class="plot">{"".join(svg)}{"".join(labels)}<span class="c-empty">{escape(empty)}</span></div></div>')
    lt, lv = points[-1]
    dot = f'<i class="c-dot" style="left:{X(lt) / 10:.2f}%;top:{Y(lv):.2f}%"></i>'
    return (f'<div class="chartbox" role="img" aria-label="{escape(label)}: {int(lv)} pleats per minute now, rated {int(rated or 0)}">'
            f'<div class="plot">{"".join(svg)}{dot}{"".join(labels)}</div></div>')


def as_points(hist):
    """[{"t": ms, "ppm": n}] from a snapshot -> [(datetime, ppm)]."""
    tz = timezone.get_current_timezone()
    return [(datetime.fromtimestamp(h["t"] / 1000, tz), h["ppm"]) for h in hist]
