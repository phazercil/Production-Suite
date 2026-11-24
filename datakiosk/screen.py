"""What the Data Kiosk wall screen shows, worked out on the server. The page only displays it."""
import time
from urllib.parse import urlencode, quote

from django.db.models import Count, Sum
from django.utils import timezone

from hmi.engine import shift_stats
from hmi.models import LineState, PpmReading, ShiftRun
from plant.models import MO, Machine, PlantSettings
from suite.svg import speed_chart

STATE_PILL = {"running": ("ok", "Running"), "changeover": ("warn", "Changeover"), "stopped": ("bad", "Stopped")}


def grafana(settings):
    g = settings.get("grafana") or {}
    return g if g.get("on") and g.get("url") and g.get("uid") else None


def panel_src(g, k, panel, line):
    q = {"orgId": g.get("org") or 1, "panelId": panel, "from": k.get("range") or "now-12h", "to": "now", "refresh": g.get("refresh") or "10s",
         "theme": "dark"}
    if g.get("lineVar"):
        q["var-" + g["lineVar"]] = line
    return f"{g['url'].rstrip('/')}/d-solo/{quote(g['uid'], safe='')}/{quote(g.get('slug') or '_', safe='')}?{urlencode(q)}"


def dash_src(g, k):
    q = {"orgId": g.get("org") or 1, "from": k.get("range") or "now-12h", "to": "now", "refresh": g.get("refresh") or "10s", "theme": "dark",
         "kiosk": ""}
    return f"{g['url'].rstrip('/')}/d/{quote(g['uid'], safe='')}/{quote(g.get('slug') or '_', safe='')}?{urlencode(q)}"


def machine_named(name):
    """The pleating line /data/<name>/ is about (any letter case), or None."""
    return Machine.objects.filter(stage="s1", name__iexact=str(name or "").strip()).first()


def layout(focus=None):
    """The page's shape: which lines are on screen (rotation), and which panels each one has. `focus`: one machine
    only (/data/<name>/), never rotated and never the whole Grafana dashboard."""
    st = PlantSettings.load()
    s = st.settings
    g, k = grafana(s), s.get("kiosk") or {}
    lines = list(Machine.objects.filter(stage="s1", active=True).order_by("sort", "id"))
    rotate = 0 if focus else int(float(k.get("rotate") or 0))
    show = [focus] if focus else lines
    if rotate > 0 and len(lines) > 1:
        show = [lines[int(time.time() // rotate) % len(lines)]]
    dash = bool(g and k.get("layout") == "dashboard" and not focus)
    if g and not dash:
        panels = [{"title": p.get("title") or "", "panel": p.get("panel")} for p in (k.get("panels") or []) if p.get("panel")]
    else:
        panels = [] if g else [{"title": "Pleater speed · this shift", "builtin": True}]
    cols = max(1, min(int(float(k.get("cols") or 3)), len(panels) or 1))
    bands = [{"name": m.name, "panels": [{**p, "src": panel_src(g, k, p["panel"], m.name)} if not p.get("builtin") else p for p in panels]}
             for m in show]
    return {"grafana": bool(g), "dash": dash, "dashSrc": dash_src(g, k) if dash else "", "bands": bands, "any": bool(lines),
            "rotate": rotate if rotate > 0 and len(lines) > 1 else 0, "minCol": 260 if cols > 2 else 340, "demo": st.demo,
            "planAt": st.plan_at, "focus": focus.name if focus else "", "lines": [m.name for m in lines]}


def line_card(name):
    m = Machine.objects.filter(stage="s1", name=name).first()
    if not m:
        return None
    sh = ShiftRun.objects.filter(machine=m, active=True).select_related("mo").first()
    ls = LineState.objects.filter(machine=m).first()
    feed = (ls.feed if ls else {}) or {}
    on = bool(sh)
    lock = None
    if not on:
        from hmi.kscreen import lockout_for
        lock = lockout_for(m.name) if not m.active else None
        pill = ("bad", "Locked out") if lock else ("bad", "Out of service") if not m.active else ("idle", "Idle")
    else:
        pill = STATE_PILL.get(sh.state, ("idle", sh.state.title()))
    pts = list(PpmReading.objects.filter(shift=sh).order_by("t").values_list("t", "ppm")[:1500]) if on else []
    mo = sh.mo if on and sh.mo_id else None
    nxt = MO.objects.filter(line=m, status="OPEN").exclude(pk=mo.pk if mo else None).order_by("seq").first()
    st = shift_stats(sh) if on else None
    good = max(0, mo.produced - mo.scrap) if mo else 0
    no_data = bool(on and feed.get("t") and not feed.get("ok"))
    if no_data:
        pill = ("warn", "Not reporting")   # not the same as stopped: the machine's data isn't coming in
    perf = oee = None
    if on and m.ppm:
        agg = PpmReading.objects.filter(shift=sh, run=True).aggregate(s=Sum("ppm"), n=Count("id"))
        if agg["n"]:
            perf = min(150.0, 100 * (agg["s"] or 0) / (agg["n"] * m.ppm))
            qual = 100 * good / mo.produced if mo and mo.produced else 100.0 if not (mo and mo.scrap) else None
            if st and qual is not None:
                oee = st["uptime"] * perf * qual / 10000
    return {
        "name": m.name, "color": m.color, "rated": int(m.ppm), "on": on, "pill": pill,
        "speed": pts[-1][1] if pts else 0,
        "mo": {"number": mo.number, "item": mo.part, "good": good, "qty": mo.qty, "pct": min(100, good / mo.qty * 100) if mo.qty else 0} if mo else None,
        "changeover": on and sh.state == "changeover",
        "next": {"number": nxt.number, "item": nxt.part} if nxt else None,
        "count": sh.count if on else None,
        "uptime": st["uptime"] if st else None, "down": st["down"] if st else None,
        "uptimeBad": bool(st and st["uptime"] < 80), "downBad": bool(st and st["down"] > 30),
        "who": sh.user_name if on else "", "since": timezone.localtime(sh.start) if on else None,
        "lockout": f"Maintenance request #{lock.pk}: {lock.type} · {lock.lockout_by}" if lock else "",
        "noData": no_data, "perf": round(perf) if perf is not None else None, "oee": round(oee) if oee is not None else None,
        "oeeBad": bool(oee is not None and oee < 60),
        "chart": speed_chart(pts, m.ppm, label=f"Pleater speed for {m.name}"),
    }
