"""Operator Kiosk engine: every change an operator or shift lead makes, plus the per-minute machine tick.

All plant logic lives here. Screens send a command name and parameters to /api/hmi/cmd; the view
calls `execute()` inside one transaction and sends back a fresh snapshot.
"""
import json
import logging
import math
import re
from datetime import timedelta

from django.conf import settings as dj_settings
from django.db import transaction
from django.db.models import F
from django.db.models.functions import Greatest

from plant import netsafety
from plant import perms
from plant.models import MO, PALETTE, CostItem, Machine, PartSpec, Person, PlantSettings, Shift, ShiftBreak, part_key, DEFAULT_CONFIG
from plant.util import MIN, Catalog, default_ops, from_ms, iso_date, ms, now, part_spec, shift_at

from . import integrations
from .models import (PlannedTime, AuditLog, ChangeoverCache, Downtime, Event, LineState, MoRun, Notice, PpmReading, QCCheck, RollTag,
                     SavedQuery, ShiftNote, ShiftRun, Ticket, MachineError, ToolingCache)

log = logging.getLogger("plant")


TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


class Need(Exception):
    """A rule was not met. The message is shown to the operator as-is."""


def need(cond, msg):
    if not cond:
        raise Need(msg)


class Ctx:
    def __init__(self, user=None, line=None, t=None, client_ip=""):
        self.user = user
        self.line = line
        self.now = t or now()
        self.client_ip = client_ip

    @property
    def by(self):
        return self.user.name if self.user else "system"


def fmt_t(dt):
    from plant.util import local
    d = local(dt)
    h = d.hour % 12 or 12
    return f"{h}:{d.minute:02d} {'pm' if d.hour >= 12 else 'am'}"


# ------------------------------------------------------------------ records & notices
def event(kind, data):
    st = PlantSettings.load()
    if st.settings.get("webhook"):
        Event.objects.create(t=now(), kind=kind, data=json.loads(json.dumps(data, default=str)))


OPERATOR_KINDS = ("schedule", "break", "maint", "qc", "info")   # what the operator notification bar shows


def notify(text, kind="schedule", t=None, lines=None, go=""):
    """A notice. lines: machine names it is for (None / empty = every line). go: the operator page with more."""
    Notice.objects.create(t=t or now(), text=text[:300], kind=kind, lines=sorted({str(x) for x in (lines or []) if x}), go=go[:20])
    ids = list(Notice.objects.values_list("id", flat=True)[200:])
    if ids:
        Notice.objects.filter(id__in=ids).delete()


def audit(ctx, what):
    AuditLog.objects.create(t=ctx.now, by=ctx.by, what=what)
    event("config_change", {"what": what, "by": ctx.by})


def settings_obj():
    return PlantSettings.load()


def config():
    return settings_obj().config


def rules():
    return config()["rules"]


# ------------------------------------------------------------------ lookups
def line_of(name):
    return Machine.objects.filter(name=name, stage="s1").first() if name else None


def active_run(machine):
    return ShiftRun.objects.filter(machine=machine, active=True).select_related("mo").first()


def shift_of(p, ctx):
    name = p.get("line") or ctx.line
    m = line_of(name)
    need(m, "Pick which machine this screen is for.")
    sh = ShiftRun.objects.select_for_update().filter(machine=m, active=True).first()
    need(sh, f"No shift is running on {m.name}.")
    return sh


def open_mo(v):
    return MO.objects.filter(number=str(v).strip(), status="OPEN").first()


def running_elsewhere(mo, machine):
    return ShiftRun.objects.filter(active=True, mo=mo).exclude(machine=machine).exists()


def spec_for(item):
    return part_spec(item)


def pleats_for(sh):
    if sh.override and sh.override.get("pleats"):
        return float(sh.override["pleats"])
    return float(spec_for(sh.mo.part if sh.mo else "")["pleats"]) or 24


# ------------------------------------------------------------------ machine
def to_number(v):
    """A finite number from a machine reading, or None for blanks, text, NaN and infinity."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def planned_windows(sh, kinds=False):
    """Time the line is not meant to run during this shift run, per the Scheduler's shift: its breaks and lunches,
    and the start-up allowance (Rules > startGrace minutes after the scheduled start). Stops inside these are not
    downtime: they are not logged, need no reason and don't count against uptime."""
    from datetime import datetime as _dt

    from django.utils import timezone as _tz

    from plant.models import Shift
    from plant.util import hm, is_lunch, local
    if not sh or not sh.shift_name:
        return []
    shift = Shift.objects.filter(name=sh.shift_name).prefetch_related("breaks").first()
    if not shift:
        return []
    started = local(sh.start)
    for back in (0, 1):  # the shift occurrence the run belongs to: today's, or yesterday's for a run after midnight
        day = (started - timedelta(days=back)).date()
        s0 = _tz.make_aware(_dt(day.year, day.month, day.day)) + timedelta(minutes=hm(shift.start))
        length = (hm(shift.end) - hm(shift.start)) % 1440 or 1440
        e0 = s0 + timedelta(minutes=length)
        if s0 - timedelta(hours=2) <= sh.start < e0:
            out = []
            grace = rules().get("startGrace", 0) or 0
            if grace:
                out.append((s0, s0 + timedelta(minutes=grace), "Start-up"))
            through = bool(sh.machine and sh.machine.run_lunch)
            for b in shift.breaks.all():
                if through and is_lunch(b.label):
                    continue   # the line runs through lunch, so a stop then is downtime
                bo = (hm(b.start) - hm(shift.start)) % 1440
                bl = (hm(b.end) - hm(b.start)) % 1440
                if bl and bo < length:
                    out.append((s0 + timedelta(minutes=bo), s0 + timedelta(minutes=bo + bl), b.label or "Break"))
            from plant.util import extra_breaks
            for w in extra_breaks(s0, e0):   # breaks an Admin or shift lead added for today
                if w["machines"] is None or sh.machine_id in w["machines"]:
                    out.append((max(w["s"], s0), min(w["e"], e0), w["label"]))
            out.sort()
            return out if kinds else [(a, b) for a, b, _ in out]
    return []


_planned_cache = {}


def planned_for_tick(sh, t):
    """Planned windows for a running shift, looked up at most every 10 minutes, and written to hmi_planned
    (so Grafana can leave the same time out)."""
    hit = _planned_cache.get(sh.id)
    if hit and (t - hit[0]).total_seconds() < 600:
        return hit[1]
    win = planned_windows(sh, kinds=True)
    if not hit or hit[1] != win:
        PlannedTime.objects.filter(shift=sh).delete()
        PlannedTime.objects.bulk_create([PlannedTime(shift=sh, line=sh.line, start=a, end=b, kind=k[:60]) for a, b, k in win])
    if len(_planned_cache) > 200:
        _planned_cache.clear()
    _planned_cache[sh.id] = (t, win)
    return win


def minus_planned(start, end, windows):
    """Pieces of [start, end) outside the planned windows."""
    pieces = [(start, end)]
    for a, b in windows:
        nxt = []
        for s, e in pieces:
            if b <= s or a >= e:
                nxt.append((s, e))
            else:
                if s < a:
                    nxt.append((s, a))
                if b < e:
                    nxt.append((b, e))
        pieces = nxt
    return [(s, e) for s, e in pieces if (e - s).total_seconds() >= 60]


def add_down(sh, start, end, reason=None):
    """Log a stop as downtime. Planned time (breaks, lunch, start-up) inside it is left out; what remains is
    logged only if it adds up to the Rules > minDown minutes (changeovers are always logged)."""
    pieces = [(start, end)] if reason == "Changeover" else minus_planned(start, end, planned_windows(sh))
    total = sum((e - s).total_seconds() for s, e in pieces) / 60
    if not pieces or (reason != "Changeover" and total < rules()["minDown"]):
        return None
    d = None
    for s, e in pieces:
        d = Downtime.objects.create(shift=sh, line=sh.line, mo=sh.mo.number if sh.mo else "", start_time=s, end_time=e,
                                    minutes=max(1, round((e - s).total_seconds() / 60)), reason=reason)
        event("downtime", {"line": d.line, "start": s, "end": e, "minutes": d.minutes, "reason": reason})
    return d


def tick(machine, sample, t):
    """One minute of machine data. sample = {"ppm": .., "count": .., "recipe": .., "run": True/False} or None when
    the feed is offline. "run" is the machine's run signal (High/Low) when it has one; without it the line counts
    as stopped at or below the stop speed."""
    ls, _ = LineState.objects.get_or_create(machine=machine)
    ls.last_tick = t
    sh = ShiftRun.objects.select_for_update().filter(machine=machine, active=True).select_related("mo").first()
    if not sh:
        ls.save(update_fields=["last_tick"])
        return
    ppm_raw = to_number(sample.get("ppm")) if sample else None
    if ppm_raw is None:
        if ls.gap == 0 and sh.stop_start:
            # The feed just went quiet during a stop. Count the stop only up to the last real reading, so
            # time with no machine data never shows up as downtime.
            last_real = t - timedelta(minutes=1)
            if (last_real - sh.stop_start).total_seconds() / 60 >= rules()["minDown"]:
                add_down(sh, sh.stop_start, last_real)
            sh.stop_start = None
            sh.save(update_fields=["stop_start"])
        ls.gap += 1
        ls.save(update_fields=["last_tick", "gap"])
        return
    ls.gap = 0
    ls.save(update_fields=["last_tick", "gap"])
    co = sh.state == "changeover"
    r = rules()
    ppm = 0 if co else max(0, round(ppm_raw))
    planned = any(a < t <= b for a, b, _ in planned_for_tick(sh, t))
    signal = sample.get("run")
    running = (signal if isinstance(signal, bool) else ppm > r["stopPpm"]) and not co
    recipe = str(sample.get("recipe") or "")[:80]
    reading = PpmReading.objects.create(machine=machine, line=machine.name, shift=sh, t=t, ppm=ppm, planned=planned, run=running,
                                        recipe=recipe)
    units = 0
    cnt = to_number(sample.get("count"))
    if cnt is not None and cnt >= 0:
        c = int(cnt)
        if not co and sh.last_count is not None and c >= sh.last_count:
            units = c - sh.last_count
        sh.last_count = c
    elif not co:
        sh.acc += ppm
        per = pleats_for(sh)
        units = int(sh.acc // per)
        sh.acc -= units * per
    if units > 0:
        reading.units = units
        reading.save(update_fields=["units"])
        sh.count += units
        if sh.mo:
            MO.objects.filter(pk=sh.mo_id).update(produced=F("produced") + units)
            sh.mo_count += units
    if not co:
        if not running:
            if not sh.stop_start:
                sh.stop_start = t - timedelta(minutes=1)
            sh.state = "stopped"
        else:
            if sh.stop_start:
                end = t - timedelta(minutes=1)  # the last stopped minute
                if (end - sh.stop_start).total_seconds() / 60 >= r["minDown"]:
                    add_down(sh, sh.stop_start, end)
                sh.stop_start = None
            sh.state = "running"
            if sh.qc_after_co and not sh.qc_due_at:   # first running minute after a changeover: QC is due now
                sh.qc_due_at = t - timedelta(minutes=1)
    sh.save(update_fields=["count", "mo_count", "acc", "last_count", "stop_start", "state", "qc_due_at"])


def close_stop(sh, t):
    if sh.stop_start:
        if (t - sh.stop_start).total_seconds() / 60 >= rules()["minDown"]:
            add_down(sh, sh.stop_start, t)
        sh.stop_start = None


def shift_stats(sh):
    if not sh:
        return {"mins": 0, "run": 0, "down": 0, "uptime": 100}
    win = planned_windows(sh)
    n = r = 0
    for t, running in PpmReading.objects.filter(shift=sh).values_list("t", "run"):
        if not running and any(a < t <= b for a, b in win):
            continue  # stopped for a planned break or start-up: not downtime
        n += 1
        r += bool(running)
    return {"mins": n, "run": r, "down": n - r, "uptime": round(r / n * 100) if n else 100}


def rename_machine_records(old, new):
    """Carry a machine's records over to its new name. Used by the kiosk Admin page and the Scheduler."""
    if not old or old == new:
        return
    for model in (ShiftRun, PpmReading, Downtime, QCCheck, RollTag, ShiftNote, MoRun, MachineError):
        model.objects.filter(line=old).update(line=new)
    Ticket.objects.filter(machine=old).update(machine=new)
    Ticket.objects.filter(line=old).update(line=new)
    for c in ChangeoverCache.objects.filter(machine=old):
        if ChangeoverCache.objects.filter(part=c.part, machine=new).exists():
            c.delete()
        else:
            c.machine = new
            c.save(update_fields=["machine"])
    from labor.sync import rename_machine
    rename_machine(old, new)   # the Labor Budget's line keeps its crew


# ------------------------------------------------------------------ commands
CMD, ROLE = {}, {}


def cmd(name, role="user"):
    def deco(fn):
        CMD[name], ROLE[name] = fn, role
        return fn
    return deco


def refresh_schedule(by):
    from scheduler.engine import refresh_floor_queue
    refresh_floor_queue(by=by)


@cmd("startShift")
def start_shift(p, ctx):
    m = line_of(p.get("line") or ctx.line)
    need(m, "Pick which machine this screen is for.")
    m = Machine.objects.select_for_update().get(pk=m.pk)  # two screens can't start a shift on one machine at once
    need(m.active, f"{m.name} is out of service.")
    need(ctx.user.trained.filter(pk=m.pk).exists(), f"You are not trained on {m.name}. Pick another machine or ask your shift lead.")
    cur = active_run(m)
    need(not cur, f"{m.name} already has a shift running since {fmt_t(cur.start) if cur else ''}. End it first.")
    safety = p.get("safety") or []
    need(isinstance(safety, list) and len(safety) == len(config()["safety"]) and all(safety),
         "Every safety item must be checked before the line runs.")
    mo = None
    if p.get("mo"):
        mo = open_mo(p["mo"])
        need(mo, f"MO {p['mo']} is not an open order. Check the number or pick one from the schedule.")
        need(not mo.line_id or mo.line_id == m.id, f"MO {mo.number} is scheduled on {mo.line.name if mo.line else ''}, not {m.name}.")
        need(not running_elsewhere(mo, m), f"MO {mo.number} is already running on another machine.")
    w = shift_at(ms(ctx.now))
    sh = ShiftRun.objects.create(machine=m, line=m.name, person=ctx.user, user_name=ctx.user.name, shift_name=(w or {}).get("name", ""),
                                 start=ctx.now, mo=mo, mo_start=ctx.now, state="running", issues=str(p.get("issues") or "").strip(),
                                 safety=safety, pm=p.get("pm") if isinstance(p.get("pm"), list) else [], qc_last=ctx.now)
    if mo and not mo.line_id:
        mo.line, mo.seq = m, 0
        mo.save(update_fields=["line", "seq"])
    event("shift_start", {"line": m.name, "mo": mo.number if mo else None, "user": ctx.user.name, "issues": sh.issues})
    return {"msg": f"Shift started on {m.name} at {fmt_t(ctx.now)}"}


@cmd("endShift")
def end_shift(p, ctx):
    sh = shift_of(p, ctx)
    if sh.mo:
        MoRun.objects.create(shift=sh, line=sh.line, mo=sh.mo.number, item=sh.mo.part, qty=sh.mo_count, start_time=sh.mo_start or sh.start,
                             end_time=ctx.now)
    close_stop(sh, ctx.now)
    if sh.state == "changeover" and sh.co and sh.co.get("start"):
        add_down(sh, from_ms(sh.co["start"]), ctx.now, "Changeover")
    sh.end_issues = str(p.get("issues") or "").strip()
    st = shift_stats(sh)
    sh.active, sh.end, sh.mo, sh.state, sh.co = False, ctx.now, None, "idle", None
    sh.uptime, sh.down_mins = st["uptime"], st["down"]
    sh.save()
    event("shift_end", {"line": sh.line, "count": sh.count, "uptime": st["uptime"], "down": st["down"]})
    return {"msg": f"Shift ended on {sh.line}"}


@cmd("coOpen")
def co_open(p, ctx):
    sh = shift_of(p, ctx)
    if not sh.co:
        sh.co = {"step": 1, "start": None, "oldMo": sh.mo_id, "newMo": None, "done": [], "steps": None, "settings": [], "src": "admin"}
        sh.save(update_fields=["co"])


@cmd("coCancel")
def co_cancel(p, ctx):
    sh = shift_of(p, ctx)
    need(not sh.co or sh.co.get("step") == 1, "This changeover has started. Finish it to keep running.")
    sh.co = None
    sh.save(update_fields=["co"])


@cmd("coEnd")
def co_end(p, ctx):
    sh = shift_of(p, ctx)
    co = sh.co
    need(co and co.get("step") == 1, "Open the changeover first.")
    mo = MO.objects.filter(pk=co.get("oldMo")).first()
    if mo:
        q, sc = to_number(p.get("qty") or 0), to_number(p.get("scrap") or 0)
        need(q is not None and 0 <= q <= 10_000_000, "Enter the final count for this run as a whole number.")
        need(sc is not None and 0 <= sc <= 10_000_000, "Enter scrap as a whole number (0 if none).")
        q, sc = int(q), int(sc)
        mo.produced += q - sh.mo_count
        mo.scrap += sc
        if p.get("close"):
            mo.status = "CLOSE"
            mo.line, mo.seq = None, 0
        mo.save(update_fields=["produced", "scrap", "status", "line", "seq"])
        MoRun.objects.create(shift=sh, line=sh.line, mo=mo.number, item=mo.part, qty=q, scrap=sc, start_time=sh.mo_start or sh.start,
                             end_time=ctx.now)
        event("mo_end", {"mo": mo.number, "qty": q, "scrap": sc, "closed": mo.status == "CLOSE"})
    close_stop(sh, ctx.now)
    sh.mo, sh.state, sh.override = None, "changeover", None
    co.update({"start": ms(ctx.now), "step": 2})
    sh.co = co
    sh.save()
    if mo and mo.status == "CLOSE":
        refresh_schedule("closing an MO")


def load_changeover_steps(sh, co):
    """Steps and settings for the new part: asset database, else the last saved copy, else the standard checklist."""
    st = settings_obj()
    params = co_params(sh, co)
    co.update({"steps": None, "settings": [], "src": "admin", "err": ""})
    if not (st.settings.get("useDb") and integrations.assets_ready()):
        return
    q = SavedQuery.objects.filter(name="changeover_steps").first()
    qs = SavedQuery.objects.filter(name="asset_settings").first()
    if not q:
        return
    try:
        rows = integrations.run_saved(q.sql, params)["rows"]
        steps = norm_steps(rows)
        sets = norm_settings(integrations.run_saved(qs.sql, params)["rows"]) if qs else []
        if steps:
            co.update({"steps": steps, "settings": sets, "src": "db"})
            ChangeoverCache.objects.update_or_create(part=params["part"], machine=sh.line,
                                                     defaults={"steps": steps, "settings": sets, "t": now()})
        else:
            co["src"] = "admin-empty"
    except Exception as e:  # database unreachable or query failed
        log.warning("changeover lookup failed: %s", e)
        cache = ChangeoverCache.objects.filter(part=params["part"], machine=sh.line).first()
        if cache:
            co.update({"steps": cache.steps, "settings": cache.settings, "src": "cache", "cacheT": ms(cache.t)})
        else:
            co["src"] = "admin-offline"


def co_params(sh, co):
    m = MO.objects.filter(pk=co.get("newMo")).first() if co and co.get("newMo") else sh.mo
    old = MO.objects.filter(pk=co.get("oldMo")).first() if co and co.get("oldMo") else None
    return {"part": part_key(m.part) if m else "", "machine": sh.line, "from_part": part_key(old.part) if old else "",
            "mo": m.number if m else "", "line_code": sh.machine.code}


def norm_steps(rows):
    out = []
    lc = [({str(k).lower(): v for k, v in r.items()}, i) for i, r in enumerate(rows or [])]
    if all(o.get("seq") is not None for o, _ in lc):
        lc.sort(key=lambda x: (float(x[0]["seq"]) if str(x[0]["seq"]).replace(".", "", 1).isdigit() else 0, x[1]))
    for o, _ in lc:
        text = str(o.get("step") or o.get("instruction") or o.get("text") or "").strip()
        if text:
            out.append({"text": text, "detail": str(o.get("detail") or ""), "setting": str(o.get("setting") or ""),
                        "value": "" if o.get("value") is None else str(o["value"]), "unit": str(o.get("unit") or ""),
                        "asset": str(o.get("asset") or ""), "image": str(o.get("image") or "")})
    return out


def norm_settings(rows):
    out = []
    for r in rows or []:
        o = {str(k).lower(): v for k, v in r.items()}
        name = str(o.get("name") or o.get("setting") or "").strip()
        if name:
            out.append({"name": name, "value": "" if o.get("value") is None else str(o["value"]), "unit": str(o.get("unit") or ""),
                        "asset": str(o.get("asset") or "")})
    return out


@cmd("coPick")
def co_pick(p, ctx):
    sh = shift_of(p, ctx)
    co = sh.co
    need(co and co.get("step", 0) >= 2, "End the current MO first.")
    m = open_mo(p.get("mo") or "")
    need(m, "That MO is not open. Check the number or pick one from the list.")
    need(not m.line_id or m.line_id == sh.machine_id, f"MO {m.number} is scheduled on {m.line.name if m.line else ''}. Ask your shift lead to move it first.")
    need(not running_elsewhere(m, sh.machine), f"MO {m.number} is already running on another machine.")
    co.update({"newMo": m.id, "step": 3, "done": []})
    load_changeover_steps(sh, co)
    sh.co = co
    sh.save(update_fields=["co"])


@cmd("coReload")
def co_reload(p, ctx):
    sh = shift_of(p, ctx)
    need(sh.co and sh.co.get("step") == 3, "No changeover in progress.")
    co = sh.co
    load_changeover_steps(sh, co)
    co["done"] = []
    sh.co = co
    sh.save(update_fields=["co"])


@cmd("coStep")
def co_step(p, ctx):
    sh = shift_of(p, ctx)
    co = sh.co
    need(co and co.get("step") == 3, "No changeover in progress.")
    i = int(p.get("i", -1))
    done = set(co.get("done") or [])
    if p.get("checked"):
        done.add(i)
    else:
        done.discard(i)
    co["done"] = sorted(done)
    sh.co = co
    sh.save(update_fields=["co"])


@cmd("coFinish")
def co_finish(p, ctx):
    sh = shift_of(p, ctx)
    co = sh.co
    need(co and co.get("step") == 3, "Pick the next MO first.")
    n = len(co.get("steps") or config()["coSteps"])
    need(len(co.get("done") or []) >= n, f"Finish all {n} steps first ({len(co.get('done') or [])} done).")
    m = MO.objects.select_for_update().filter(pk=co["newMo"], status="OPEN").first()
    need(m, "That MO was closed or removed while you were setting up. Pick the next MO again.")
    need(not running_elsewhere(m, sh.machine), f"MO {m.number} started running on another machine. Pick a different MO.")
    if not m.line_id:
        m.line, m.seq = sh.machine, 0
        m.save(update_fields=["line", "seq"])
    add_down(sh, from_ms(co["start"]), ctx.now, "Changeover")
    sh.mo, sh.mo_start, sh.mo_count, sh.state, sh.co = m, ctx.now, 0, "running", None
    sh.qc_after_co, sh.qc_due_at = True, None   # a QC check is due as soon as the machine runs on the new MO
    sh.save()
    event("mo_start", {"line": sh.line, "mo": m.number, "item": m.part, "steps_from": co.get("src", "admin")})
    return {"msg": f"Running MO {m.number} · {m.part}"}


def _qc_values(sh, p):
    """The stage's QC items (Admin › QC checks), the values submitted for them, and the part's spec."""
    from . import qc
    item = sh.mo.part if sh.mo else (MO.objects.get(pk=sh.co["newMo"]).part if sh.co and sh.co.get("newMo") else "")
    P = spec_for(item)
    items = qc.items_for(sh.machine.stage if sh.machine else "s1")
    try:
        vals = qc.parse(items, p)
    except ValueError as e:
        raise Need(str(e))
    ok, fails = qc.judge(items, vals, P)
    cols = {k: vals.get(k) for k in ("len", "wid", "ph", "sbl", "sbr")}
    cols.update(glue=vals.get("glue", ""), cut=vals.get("cut", ""), scrap=int(vals.get("scrap") or 0))
    return P, vals, ok, fails, cols


@cmd("qcSave")
def qc_save(p, ctx):
    from . import qc
    sh = shift_of(p, ctx)
    P, vals, ok, fails, cols = _qc_values(sh, p)
    due = qc.due_at(sh, settings_obj().settings, ctx.now)
    late = qc.late_by(sh, settings_obj().settings, rules(), ctx.now)
    reason = str(p.get("lateReason") or "").strip()
    need(not late or reason, f"This check is {late} min late. Say why it wasn't done on time.")
    q = QCCheck.objects.create(shift=sh, t=ctx.now, line=sh.line, mo=sh.mo.number if sh.mo else "", item=P["key"], by=ctx.by, passed=ok,
                               values=vals, fails=fails, due_at=due, late_reason=reason[:500] if late else "", review="" if ok else "pending", **cols)
    if sh.mo and cols["scrap"]:
        MO.objects.filter(pk=sh.mo_id).update(scrap=F("scrap") + cols["scrap"])
    sh.qc_last, sh.qc_after_co, sh.qc_due_at = ctx.now, False, None
    sh.save(update_fields=["qc_last", "qc_after_co", "qc_due_at"])
    event("qc", {"line": sh.line, "item": P["key"], "pass": ok, **vals})
    return {"msg": "QC passed and saved" if ok else "QC out of spec — a supervisor needs to approve or disapprove it",
            "data": {"pass": ok, "id": q.id}}


@cmd("qcEdit", "qc.edit")
def qc_edit(p, ctx):
    """A supervisor corrects a finished QC check (Supervisor override)."""
    q = QCCheck.objects.filter(pk=p.get("id")).select_related("shift", "shift__machine", "shift__mo").first()
    need(q and q.shift, "That QC check is no longer on file.")
    from . import qc
    items = qc.items_for(q.shift.machine.stage if q.shift.machine else "s1")
    try:
        vals = qc.parse(items, p)
    except ValueError as e:
        raise Need(str(e))
    P = spec_for(q.item)
    ok, fails = qc.judge(items, vals, P)
    old_scrap = q.scrap
    for k in ("len", "wid", "ph", "sbl", "sbr"):
        setattr(q, k, vals.get(k))
    q.glue, q.cut, q.scrap = vals.get("glue", ""), vals.get("cut", ""), int(vals.get("scrap") or 0)
    q.values, q.fails, q.passed = vals, fails, ok
    if ok and q.review == "pending":
        q.review = ""
    elif not ok and not q.review:
        q.review = "pending"
    q.edited_by, q.edited_at = ctx.by, ctx.now
    q.save()
    if q.mo and q.scrap != old_scrap:
        MO.objects.filter(number=q.mo).update(scrap=F("scrap") + (q.scrap - old_scrap))
    audit(ctx, f"QC check {q.line} {fmt_t(q.t)} changed ({'pass' if ok else 'out of spec'})")
    return {"msg": "QC check changed"}


@cmd("qcReview", "qc.approve")
def qc_review(p, ctx):
    """A supervisor approves an out-of-spec QC check, or disapproves it and takes scrapped filters off the count."""
    q = QCCheck.objects.select_for_update().filter(pk=p.get("id")).first()
    need(q, "That QC check is no longer on file.")
    need(not q.passed, "That check passed; there's nothing to approve.")
    d = p.get("decision")
    need(d in ("approved", "disapproved"), "Approve or disapprove.")
    n = 0
    if d == "disapproved":
        n = to_number(p.get("scrap"))
        need(n is not None and n >= 0 and n == int(n) and n <= 1_000_000, "How many filters to scrap? (a whole number, 0 for none)")
        n = int(n) - q.scrapped   # a second decision only scraps the difference
    sh = q.shift
    if n:
        if sh:
            sh.count = max(0, sh.count - n)
            if sh.mo and sh.mo.number == q.mo:
                sh.mo_count = max(0, sh.mo_count - n)
            sh.save(update_fields=["count", "mo_count"])
        MO.objects.filter(number=q.mo).update(produced=Greatest(F("produced") - n, 0), scrap=F("scrap") + n)
    q.review, q.reviewed_by, q.reviewed_at = d, ctx.by, ctx.now
    q.scrapped = q.scrapped + n
    q.save(update_fields=["review", "reviewed_by", "reviewed_at", "scrapped"])
    audit(ctx, f"QC check {q.line} {fmt_t(q.t)} {d}" + (f", {q.scrapped} scrapped from the count" if q.scrapped else ""))
    notify(f"QC check at {fmt_t(q.t)} {d} by {ctx.by}" + (f": {q.scrapped} filters scrapped from the count" if q.scrapped else ""),
           "qc", lines=[q.line], go="qc-log")
    return {"msg": "Approved" if d == "approved" else f"Disapproved · {q.scrapped} scrapped from the count"}


@cmd("rollAdd")
def roll_add(p, ctx):
    sh = shift_of(p, ctx)
    rid = str(p.get("roll") or "").strip()
    need(rid, "Scan or type the roll ID.")
    feet = None
    if str(p.get("feet") or "").strip():
        feet = to_number(p.get("feet"))
        need(feet is not None and 0 <= feet <= 1_000_000, "Footage must be a number of feet.")
        feet = int(feet)
    need(len(rid) <= 80, "That roll ID is too long (80 characters at most).")
    RollTag.objects.create(shift=sh, t=ctx.now, line=sh.line, roll=rid, material=str(p.get("material") or "")[:120], lot=str(p.get("lot") or "")[:80],
                           feet=feet, mo=sh.mo.number if sh.mo else "", count_at=sh.count, by=ctx.by)
    event("roll", {"line": sh.line, "roll": rid})
    return {"msg": f"Roll {rid} added"}


@cmd("ticketAdd")
def ticket_add(p, ctx):
    m = line_of(ctx.line)
    sh = active_run(m) if m else None
    machine = str(p.get("machine") or "")
    need(machine, "Pick the machine.")
    need(len(machine) <= 60, "Pick the machine from the list.")
    t = Ticket.objects.create(shift=sh, t=ctx.now, line=(ctx.line or "")[:60], by=ctx.by, machine=machine, type=str(p.get("type") or "")[:80],
                              priority=p.get("priority") if p.get("priority") in ("Low", "Medium", "High") else "Medium",
                              notes=str(p.get("notes") or "").strip())
    event("maintenance", {"id": t.id, "machine": machine, "type": t.type, "priority": t.priority})
    return {"msg": "Maintenance ticket sent", "data": {"id": t.id}}


@cmd("ticketStatus", "tickets.work")
def ticket_status(p, ctx):
    from .models import TicketNote
    t = Ticket.objects.select_for_update().filter(pk=p.get("id")).first()
    need(t, "Ticket not found.")
    need(t.status != "Closed", "That request is already completed.")
    note = str(p.get("note") or "").strip()
    need(len(note) <= 4000, "Keep the repair note under 4,000 characters.")
    t.status = "In progress" if t.status == "Open" and not p.get("close") else "Closed"
    t.updated, t.updated_by = ctx.now, ctx.by
    t.save()
    TicketNote.objects.create(ticket=t, t=ctx.now, by=ctx.by, kind="closed" if t.status == "Closed" else "started",
                              text=note or ("Completed" if t.status == "Closed" else "Started work"))
    event("maintenance_status", {"id": t.id, "status": t.status})
    notify((f"Maintenance completed on {t.machine} · {t.type} by {ctx.by}" + (f": {note}" if note else "")) if t.status == "Closed"
           else f"Maintenance started on {t.machine} · {t.type} ({ctx.by})", "maint", lines=[t.line or t.machine, t.machine], go="maintenance")
    return {"msg": "Marked complete" if t.status == "Closed" else "Work started"}


@cmd("ticketNote", "tickets.work")
def ticket_note(p, ctx):
    from .models import TicketNote
    t = Ticket.objects.filter(pk=p.get("id")).first()
    need(t, "Ticket not found.")
    text = str(p.get("text") or "").strip()
    need(text, "Write the repair note first.")
    need(len(text) <= 4000, "Keep the repair note under 4,000 characters.")
    TicketNote.objects.create(ticket=t, t=ctx.now, by=ctx.by, text=text)
    if t.status == "Open":
        t.status = "In progress"
    t.updated, t.updated_by = ctx.now, ctx.by
    t.save(update_fields=["status", "updated", "updated_by"])
    event("maintenance_note", {"id": t.id})
    notify(f"Repair note on {t.machine} · {t.type} from {ctx.by}: {text}", "maint", lines=[t.line or t.machine, t.machine], go="maintenance")
    return {"msg": "Repair note added"}


@cmd("noteAdd")
def note_add(p, ctx):
    text = str(p.get("text") or "").strip()
    need(text, "Write the note first.")
    m = line_of(ctx.line)
    ShiftNote.objects.create(shift=active_run(m) if m else None, t=ctx.now, line=ctx.line or "", by=ctx.by, text=text)
    event("note", {"line": ctx.line, "text": text})


@cmd("downtimeSet")
def downtime_set(p, ctx):
    d = Downtime.objects.filter(pk=p.get("id")).first()
    need(d, "That stop is no longer on file.")
    if "reason" in p:
        reason = str(p["reason"] or "").strip()[:120] or None
        lead = perms.can(ctx.user, "downtime.reason")   # Admin › Permissions
        # Operators pick from the list (Admin > Downtime reasons) and can't change a reason someone else entered.
        # Maintenance and shift leads can type their own reason or change one already entered.
        if not lead:
            need(not reason or reason in config()["reasons"], "Pick a reason from the list. Maintenance or a shift lead can enter a different one.")
            need(not d.reason or d.by == ctx.by or not reason, f"{d.by or 'Someone'} already entered a reason. Maintenance or a shift lead can change it.")
            need(reason or not d.reason or d.by == ctx.by, "Maintenance or a shift lead can clear a reason.")
        if d.reason and reason != d.reason and lead:
            d.notes = (d.notes + " " if d.notes else "") + f"[{ctx.by} changed the reason from {d.reason}]"
        d.reason, d.by = reason, ctx.by
    if "notes" in p:
        d.notes = str(p.get("notes") or "")
    d.save()
    event("downtime_update", {"id": d.id, "reason": d.reason, "notes": d.notes})


@cmd("ackNotices", "none")
def ack_notices(p, ctx):
    """Mark notices read on this machine's screen (other machines still see theirs). With no machine, for everyone."""
    from .models import NoticeRead
    line = str(p.get("line") or ctx.line or "")
    qs = Notice.objects.all()
    if p.get("id"):
        qs = qs.filter(pk=p["id"])
    else:
        qs = qs.filter(t__gte=ctx.now - timedelta(days=14))
    if not line:
        qs.update(read=True)
        return
    for n in qs:
        if not n.lines or line in n.lines:
            NoticeRead.objects.get_or_create(notice=n, line=line, defaults={"by": ctx.by, "t": ctx.now})


@cmd("getDrawing")
def get_drawing(p, ctx):
    m = line_of(ctx.line)
    sh = active_run(m) if m else None
    key = part_key(p.get("part"))
    q = SavedQuery.objects.filter(name="part_spec").first()
    msg = None
    if q and integrations.assets_ready():
        try:
            rows = integrations.run_saved(q.sql, {"part": key, "machine": ctx.line or "", "from_part": "", "mo": "", "line_code": ""})["rows"]
            if rows:
                o = {str(k).lower(): v for k, v in rows[0].items()}
                cur = spec_for(key)
                vals = {f: float(o[f]) for f in ("pleats", "len", "wid", "ph", "sb", "tol") if o.get(f) not in (None, "")}
                PartSpec.objects.update_or_create(key=key, defaults={**{k: cur[k] for k in ("pleats", "len", "wid", "ph", "sb", "tol")}, **vals,
                                                                     "rev": str(o.get("rev") or cur["rev"]), "media": str(o.get("media") or cur["media"])})
                msg = f"Drawing rev {o.get('rev') or cur['rev']} loaded from the asset database ({fmt_t(ctx.now)})."
        except Exception as e:
            msg = f"Asset database not reachable ({e}). Showing the drawing on file."
    if sh:
        sh.draw_checked = ctx.now
        sh.save(update_fields=["draw_checked"])
    return {"msg": msg or f"Rev {spec_for(key)['rev']} is current (checked {fmt_t(ctx.now)})."}


@cmd("overrideSet", "runs.override")
def override_set(p, ctx):
    sh = shift_of(p, ctx)
    o = {}
    for k in ("ppm", "pleats", "ph", "glue"):
        try:
            v = float(p.get(k))
            if v > 0:
                o[k] = v
        except (TypeError, ValueError):
            pass
    if p.get("why"):
        o["why"] = str(p["why"])
    sh.override = dict(o, by=ctx.by) if o else None
    sh.save(update_fields=["override"])
    event("setting_override", {"line": sh.line, **o})
    audit(ctx, f"Temporary settings on {sh.line}: {json.dumps(o)}")
    return {"msg": "Setting applied to this MO"}


@cmd("overrideClear", "runs.override")
def override_clear(p, ctx):
    sh = shift_of(p, ctx)
    sh.override = None
    sh.save(update_fields=["override"])
    audit(ctx, f"Cleared temporary settings on {sh.line}")


# ---------------------------------------------------------- orders from S2K
def valid_date(s):
    from datetime import date
    try:
        return date.fromisoformat(s) if s and re.match(r"^\d{4}-\d{2}-\d{2}$", s) else None
    except ValueError:
        return None


def import_mos(ctx, rows, source="s2k"):
    cat = Catalog()
    added = 0
    skipped = 0
    for r in rows or []:
        if not isinstance(r, dict) or not r.get("mo"):
            continue
        number, part, qty = str(r["mo"]).strip(), str(r.get("item") or "").strip(), to_number(r.get("qty"))
        if not number or not part or len(number) > 40 or len(part) > 40 or qty is None or qty < 1:
            skipped += 1  # a bad row is skipped; the rest still come in
            continue
        if MO.objects.filter(number=number).exists():
            continue
        item = (cat.matches(part) or [None])[0]
        due = str(r.get("due") or "")[:10] or None
        MO.objects.create(number=number, part=part, qty=int(qty),
                          due=valid_date(due), process=str(r.get("process") or "")[:12],
                          mfg=str(r.get("mfg") or "MFGOUT")[:12], hot=bool(r.get("hot")), ops=default_ops(item), source=source)
        added += 1
    if added:
        notify(f"{added} new MO{'s' if added > 1 else ''} from S2K are waiting in the Scheduler", "planner")
        audit(ctx, f"Imported {added} MOs")
    if skipped:
        log.warning("MO import skipped %s rows without a usable MO number, item or quantity", skipped)
        notify(f"{skipped} MO row{'s' if skipped > 1 else ''} from S2K skipped: missing MO number, item or quantity", "planner")
    return added


@cmd("importMOs", "schedule.edit")
def cmd_import(p, ctx):
    n = import_mos(ctx, p.get("list"), "import")
    return {"msg": f"{n} new MO{'s' if n != 1 else ''} added to the Scheduler" if n else "No new MOs found"}


def s2k_rows():
    """Open MOs from S2K: from the read-only S2K database (saved query) or from the export URL."""
    s = settings_obj().settings
    if s.get("s2kSource") == "db":
        q = SavedQuery.objects.filter(name="s2k_mos").first()
        if not q:
            raise integrations.ConnError("Save the S2K query in Admin › Connections › S2K first.")
        rows, truncated = integrations.s2k_db_rows(q.sql)
        if truncated:
            log.warning("S2K query returned more than 5000 rows; only the first 5000 were read")
        return rows
    if not s.get("s2kUrl"):
        raise integrations.ConnError("Set the S2K export URL in Admin › Connections first.")
    rows = integrations.get_json(s["s2kUrl"])
    if not isinstance(rows, list):
        raise integrations.ConnError("S2K did not send a list of MOs.")
    return rows


def s2k_on(s):
    return bool(s.get("s2kUrl")) if s.get("s2kSource") != "db" else integrations.s2k_db_ready()


@cmd("s2kPull", "lead")
def s2k_pull(p, ctx):
    try:
        rows = s2k_rows()
    except integrations.ConnError as e:
        raise Need(str(e))
    n = import_mos(ctx, rows)
    integrations.set_conn("s2k", True, f"{len(rows)} rows · {n} new")
    return {"msg": f"{n} new MOs from S2K" if n else "No new MOs in S2K"}


# ---------------------------------------------------------- people
def apply_people(ctx, rows):
    need(isinstance(rows, list), "The people list must be a JSON list of people.")
    add = upd = 0
    for r in rows:
        if not isinstance(r, dict) or not r.get("id") or not r.get("name"):
            continue
        u = Person.objects.filter(ext_id=str(r["id"])).first()
        r = dict(r, name=str(r["name"])[:120])
        if u:
            u.name = r["name"]
            if r.get("role") in ("operator", "maintenance", "lead", "admin"):
                u.role = r["role"]
            if r.get("pin") and not Person.objects.filter(pin__iexact=str(r["pin"])).exclude(pk=u.pk).exists():
                u.pin = str(r["pin"])[:12]
            u.save()
            upd += 1
        else:
            u = Person.objects.create(ext_id=str(r["id"]), name=r["name"], role=r.get("role") if r.get("role") in ("operator", "maintenance", "lead", "admin") else "operator",
                                      pin=str(r.get("pin") or "")[:12] if r.get("pin") and not Person.objects.filter(pin__iexact=str(r["pin"])[:12]).exists() else "",
                                      source="list")
            add += 1
        u.trained.set(Machine.objects.filter(name__in=r.get("trained") or []))
    if add:
        audit(ctx, f"People list: {add} added, {upd} updated")
    return f"{add} added, {upd} updated"


@cmd("peoplePull", "team.import")
def people_pull(p, ctx):
    url = settings_obj().settings.get("peopleUrl")
    need(url, "Set the people list URL in Admin › Connections first.")
    msg = apply_people(ctx, integrations.get_json(url))
    integrations.set_conn("people", True, msg)
    return {"msg": "People list: " + msg}


def apply_paycom(ctx, employees, stale=False):
    st = settings_obj()
    P = st.settings["paycom"]

    def inc(v, f, exact=False):
        lst = [x.strip().lower() for x in str(f or "").split(",") if x.strip()]
        v = " ".join(str(v or "").lower().split())
        return not lst or any((x == v) if exact else (x in v) for x in lst)

    if not isinstance(employees, list):
        raise Need("Paycom did not send an employee list. Nobody was changed.")
    lst = [e for e in employees if isinstance(e, dict) and e.get("code") and inc(e.get("department"), P.get("dept")) and inc(e.get("position"), P.get("position"), bool(P.get("positionExact")))]
    known = Person.objects.filter(source="paycom", active=True).count()
    if known and (not lst or len(lst) < known * 0.5):
        P["msg"] = (f"Paycom sent {len(lst)} matching people but {known} are active here, so nothing was changed. "
                    "Check the department and position filters, or Paycom itself.")
        st.save(update_fields=["settings"])
        raise Need(P["msg"])
    seen, add, upd, off, back, no_pin = set(), 0, 0, 0, 0, 0
    for e in lst:
        seen.add(e["code"])
        u = Person.objects.filter(eecode=e["code"]).first()
        if u:
            was = u.active
            u.name, u.dept, u.position, u.active, u.left_list = (str(e["name"])[:120], str(e.get("department") or "")[:120],
                                                                 str(e.get("position") or "")[:120], bool(e.get("active")), False)
            u.save()
            off += was and not u.active
            back += (not was) and u.active
            upd += 1
            continue
        if not e.get("active"):
            continue
        pin = e.get("badge") if P.get("pinFrom") == "badge" else e["code"] if P.get("pinFrom") == "code" else ""
        ok = bool(pin) and not Person.objects.filter(pin__iexact=str(pin)).exists()
        no_pin += not ok
        Person.objects.create(name=str(e["name"])[:120], pin=str(pin)[:12] if ok else "", role="operator", eecode=str(e["code"])[:40], source="paycom",
                              dept=str(e.get("department") or "")[:120], position=str(e.get("position") or "")[:120])
        add += 1
    for u in Person.objects.filter(source="paycom", active=True).exclude(eecode__in=seen):
        u.active, u.left_list = False, True
        u.save()
        off += 1
    P["last"] = ms(ctx.now)
    P["msg"] = (f"{len(lst)} matched · {add} added · {upd} checked · {off} set inactive" + (f" · {back} back to active" if back else "") +
                (f" · {no_pin} need a PIN" if no_pin else "") + (" · Paycom unreachable, used the last list" if stale else ""))
    st.save(update_fields=["settings"])
    if add or off or back:
        audit(ctx, "Paycom import: " + P["msg"])
        if add:
            notify(f"{add} new operator{'s' if add > 1 else ''} imported from Paycom — set their training", "team")
    return P["msg"]


@cmd("paycomImport", "team.import")
def paycom_import(p, ctx):
    need(integrations.paycom_ready(), "Paycom is not set up. Add PAYCOM_SID and PAYCOM_TOKEN to the server .env.")
    return {"msg": "Paycom: " + apply_paycom(ctx, integrations.paycom_employees())}


@cmd("paycomPositions", "connections.edit")
def paycom_positions(p, ctx):
    """Every position title Paycom has for active employees (in the department filter), with how many people hold it."""
    from collections import Counter
    need(integrations.paycom_ready(), "Paycom is not set up. Add PAYCOM_SID and PAYCOM_TOKEN to the server .env.")
    P = settings_obj().settings.get("paycom") or {}
    depts = [x.strip().lower() for x in str(P.get("dept") or "").split(",") if x.strip()]
    c = Counter()
    for e in integrations.paycom_employees():
        if not e.get("active") or (depts and not any(d in str(e.get("department") or "").lower() for d in depts)):
            continue
        c[" ".join(str(e.get("position") or "").split()) or "(no position)"] += 1
    return {"data": sorted(c.items(), key=lambda x: (-x[1], x[0].lower()))}


@cmd("paycomPositionsSet", "connections.edit")
def paycom_positions_set(p, ctx):
    pos = [" ".join(str(x).split()) for x in (p.get("positions") or []) if str(x).strip() and str(x) != "(no position)"]
    need(all("," not in x and len(x) <= 120 for x in pos), "A position title can't contain a comma.")
    st = settings_obj()
    P = st.settings.setdefault("paycom", {})
    P["position"], P["positionExact"] = ", ".join(pos), bool(pos)
    st.save(update_fields=["settings"])
    audit(ctx, "Paycom import limited to positions: " + (", ".join(pos) if pos else "all positions"))
    return {"msg": f"Importing {len(pos)} position{'s' if len(pos) != 1 else ''}" if pos else "Importing every position"}


@cmd("paycomFields", "connections.edit")
def paycom_fields(p, ctx):
    need(integrations.paycom_ready(), "Paycom is not set up. Add PAYCOM_SID and PAYCOM_TOKEN to the server .env.")
    return {"data": integrations.paycom_fields()}


@cmd("userAdd", "team.manage")
def user_add(p, ctx):
    name, pin = str(p.get("name") or "").strip(), str(p.get("pin") or "").strip()
    need(name and pin, "Name and PIN are both needed.")
    need(re.match(r"^[A-Za-z0-9]{3,12}$", pin), "PINs are 3–12 letters or digits.")
    need(not Person.objects.filter(pin__iexact=pin).exists(), "That PIN is already used.")
    role = p.get("role") if p.get("role") in ("operator", "maintenance", "lead", "admin") else "operator"
    need(role != "admin" or perms.can(ctx.user, "permissions.manage"), "Only someone who manages permissions can add an Admin.")
    Person.objects.create(name=name, pin=pin, role=role)
    audit(ctx, f"Added {name}")


@cmd("userSet", "team.manage")
def user_set(p, ctx):
    u = Person.objects.filter(pk=p.get("id")).first()
    need(u, "Person not found.")
    f, v = p.get("f"), p.get("v")
    need(u.role != "admin" or perms.can(ctx.user, "permissions.manage") or f == "train", "Only someone who manages permissions can change an Admin's login.")
    if f == "name":
        v = " ".join(str(v or "").split())
        need(v, "Type a name.")
        need(len(v) <= 120, "Names can be up to 120 characters.")
        if v != u.name:
            audit(ctx, f"Renamed {u.name} to {v}")
            u.name = v
    elif f == "role":
        need(v in ("operator", "maintenance", "lead", "admin"), "Unknown role.")
        need(v != "admin" or perms.can(ctx.user, "permissions.manage"), "Only someone who manages permissions can make someone an Admin.")
        need(not (u.role == "admin" and v != "admin" and Person.objects.filter(role="admin", active=True).count() < 2),
             "This is the only Admin. Make someone else an Admin first.")
        u.role = v
        audit(ctx, f"{u.name} role set to {dict(Person.ROLES)[v].lower() if v != 'admin' else 'Admin'}")
    elif f == "train":
        m = Machine.objects.filter(name=(v or {}).get("line")).first()
        need(m, "Unknown machine.")
        (u.trained.add if v.get("on") else u.trained.remove)(m)
        audit(ctx, f"{u.name} {'trained' if v.get('on') else 'untrained'} on {m.name}")
    elif f == "pin":
        v = str(v or "").strip()
        need(not v or re.match(r"^[A-Za-z0-9]{3,12}$", v), "PINs are 3–12 letters or digits.")
        need(not v or not Person.objects.filter(pin__iexact=v).exclude(pk=u.pk).exists(), "Someone else already uses that PIN.")
        u.pin = v
        audit(ctx, f"PIN {'set' if v else 'cleared'} for {u.name}")
    elif f == "active":
        need(u.pk != ctx.user.pk, "You cannot deactivate yourself.")
        need(v or u.role != "admin" or Person.objects.filter(role="admin", active=True).count() > 1,
             "This is the only Admin. Make someone else an Admin first.")
        u.active = bool(v)
        audit(ctx, f"{u.name} set {'active' if u.active else 'inactive'}")
    else:
        raise Need("Unknown field.")
    u.save()


@cmd("extraBreakAdd", "lists.edit")
def extra_break_add(p, ctx):
    """Schedule an extra break: at a time of day, for some minutes, on one day or every day up to a date, on every
    line or only some. Runs pause through it, stops in it aren't downtime, and the lines' operators are told."""
    from datetime import date as _date

    from plant.models import ExtraBreak
    from plant.util import local

    def day(v, label):
        try:
            return _date.fromisoformat(str(v or "").strip())
        except ValueError:
            raise Need(f"Pick the {label}.")
    d = day(p.get("day"), "day")
    until = day(p.get("until"), "last day") if str(p.get("until") or "").strip() else None
    today = local(ctx.now).date()
    need(d >= today, "Pick today or a later day.")
    need(not until or until >= d, "The last day can't be before the first day.")
    need(not until or (until - d).days <= 92, "Repeat it for up to three months at a time.")
    start = str(p.get("start") or "").strip()
    need(re.match(r"^([01]\d|2[0-3]):[0-5]\d$", start), "Pick the start time.")
    mins = to_number(p.get("minutes"))
    need(mins is not None and mins == int(mins) and 1 <= mins <= 240, "Minutes must be a whole number from 1 to 240.")
    label = " ".join(str(p.get("label") or "").split())[:60] or "Break"
    names = [str(x) for x in (p.get("machines") or []) if str(x).strip()]
    ms_ = list(Machine.objects.filter(name__in=names))
    need(len(ms_) == len(set(names)), "One of those machines isn't on the list any more.")
    for y in ExtraBreak.objects.filter(day=d, until=until, start=start, minutes=int(mins)).prefetch_related("machines"):
        need({m.pk for m in y.machines.all()} != {m.pk for m in ms_}, "That break is already scheduled.")
    x = ExtraBreak.objects.create(day=d, until=until, start=start, minutes=int(mins), label=label, by=ctx.by, t=ctx.now)
    x.machines.set(ms_)
    from suite.templatetags.suite import clock
    from datetime import datetime as _dt
    s0 = _dt(2000, 1, 1, int(start[:2]), int(start[3:]))
    when = (f"{clock(s0)}–{clock(s0 + timedelta(minutes=int(mins)))}" +
            (" today" if d == today and not until else f" on {d:%a} {d.month}/{d.day}" if not until
             else f" every day {d.month}/{d.day}–{until.month}/{until.day}"))
    lines = [m.name for m in ms_] or list(Machine.objects.filter(active=True, stage="s1").values_list("name", flat=True))
    audit(ctx, f"Extra break \"{label}\" {when}, {int(mins)} min, " + (", ".join(m.name for m in ms_) if ms_ else "every line"))
    notify(f"{label}: {when}, added by {ctx.by}", "break", lines=[m.name for m in ms_] or None, go="schedule")
    refresh_schedule(f"{ctx.by} (extra break)")
    return {"msg": f"{label} scheduled {when}", "data": {"id": x.pk, "lines": lines}}


@cmd("extraBreakDel", "lists.edit")
def extra_break_del(p, ctx):
    from plant.models import ExtraBreak
    x = ExtraBreak.objects.filter(pk=p.get("id")).prefetch_related("machines").first()
    need(x, "That break is already gone.")
    names = [m.name for m in x.machines.all()]
    audit(ctx, f"Removed extra break \"{x.label}\" {x.start} on {x.day}")
    from suite.templatetags.suite import clock
    from datetime import datetime as _dt
    at = clock(_dt(2000, 1, 1, int(x.start[:2]), int(x.start[3:5])))
    notify(f"{x.label} at {at} on {x.day:%a} {x.day.month}/{x.day.day} was cancelled by {ctx.by}", "break", lines=names or None, go="schedule")
    x.delete()
    refresh_schedule(f"{ctx.by} (extra break removed)")
    return {"msg": "Break removed"}


@cmd("personSave", "team.manage")
def person_save(p, ctx):
    """Everything from a person's Edit dialog at once: only the fields that changed are written, and if any of them
    is refused nothing is saved."""
    u = Person.objects.select_for_update().filter(pk=p.get("id")).first()
    need(u, "Person not found.")
    changed = 0
    for f in ("name", "role", "pin"):
        if f in p and str(p.get(f) or "").strip() != str(getattr(u, f) or "").strip():
            user_set({"id": u.pk, "f": f, "v": p.get(f)}, ctx)
            u.refresh_from_db()
            changed += 1
    if "active" in p and u.source != "paycom" and bool(p.get("active")) != u.active:
        user_set({"id": u.pk, "f": "active", "v": bool(p.get("active"))}, ctx)
        u.refresh_from_db()
        changed += 1
    if isinstance(p.get("trained"), list):
        want = {str(x) for x in p["trained"]}
        have = set(u.trained.values_list("name", flat=True))
        for m in Machine.objects.filter(active=True):
            if (m.name in want) != (m.name in have):
                user_set({"id": u.pk, "f": "train", "v": {"line": m.name, "on": m.name in want}}, ctx)
                changed += 1
    return {"msg": f"Saved {u.name}" if changed else "No changes to save"}


# ---------------------------------------------------------- admin lists, machines, shifts, parts, rules
LISTS = ["reasons", "safety", "pm", "coSteps", "issueTypes", "equipment"]


def _list(k):
    need(k in LISTS, "Unknown list.")
    st = settings_obj()
    return st, st.config[k]


@cmd("listAdd", "lists.edit")
def list_add(p, ctx):
    st, L = _list(p.get("k"))
    v = str(p.get("v") or "").strip()
    need(v, "Type the new item.")
    need(v.lower() not in [x.lower() for x in L], f'"{v}" is already on the list.')
    L.append(v)
    st.save(update_fields=["config"])
    audit(ctx, f'Added "{v}" to {p["k"]}')


@cmd("listDel", "lists.edit")
def list_del(p, ctx):
    st, L = _list(p.get("k"))
    need(len(L) > 1, "Keep at least one item on this list.")
    v = L.pop(int(p.get("i")))
    st.save(update_fields=["config"])
    audit(ctx, f'Removed "{v}" from {p["k"]}')
    return {"msg": f'Removed "{v}"'}


@cmd("listMove", "lists.edit")
def list_move(p, ctx):
    st, L = _list(p.get("k"))
    i = int(p.get("i"))
    j = i + int(p.get("d"))
    need(0 <= j < len(L), "Cannot move further.")
    L[i], L[j] = L[j], L[i]
    st.save(update_fields=["config"])
    audit(ctx, f"Reordered {p['k']}")


@cmd("listSet", "lists.edit")
def list_set(p, ctx):
    st, L = _list(p.get("k"))
    i, v = int(p.get("i")), str(p.get("v") or "").strip()
    need(v, "An item cannot be blank. Use ✕ to remove it.")
    if L[i] != v:
        old, L[i] = L[i], v
        st.save(update_fields=["config"])
        audit(ctx, f'Renamed "{old}" to "{v}" in {p["k"]}')


def labor_lock(what):
    """Once the Labor Budget has been saved it is the master of lines and shifts: change them there."""
    from labor.sync import master_on
    need(not master_on(), f"{what} come from the Labor Budget now. Change them there (Labor Budget in the top bar); "
                          "Admin and the Scheduler follow it when it's saved.")


@cmd("lineAdd", "machines.edit")
def line_add(p, ctx):
    labor_lock("Lines")
    n = Machine.objects.filter(stage="s1").count() + 1
    while Machine.objects.filter(name=f"Machine {n}").exists():
        n += 1
    m = Machine.objects.create(name=f"Machine {n}", code=f"M{n}", stage="s1", speed_mode="fixed", ppm=200,
                               color=PALETTE[Machine.objects.count() % len(PALETTE)], feed_type="none",
                               sort=Machine.objects.count())
    m.shifts.set(Shift.objects.all())
    audit(ctx, f"Added machine {m.name}")
    return {"data": {"id": m.name}}


SOURCE_KEYS = {"db", "table", "orderCol", "keyCol", "keyVal", "maxAge"}
# prefix of the Admin field -> (Machine attribute, extra keys it takes, columns cleared when the table changes)
SOURCES = {"feed.src.": ("feed_src", set(), ()),
           "uptime.": ("uptime_src", {"type", "field", "runWhen"}, ("field",)),
           "err.": ("error_src", {"type", "field", "textField", "timeCol", "codeCol", "textCol", "ok"}, ("timeCol", "codeCol", "textCol"))}


def _set_source(m, f, v):
    """Admin > Machine data: one setting of the machine's table (feed.src.<key>), its run signal (uptime.<key>) or
    its errors and alarms (err.<key>). Table and column names are only stored here; they are checked against the
    database's own list whenever they are read."""
    from . import machinedata
    pre = next(x for x in SOURCES if f.startswith(x))
    attr, extra, cols = SOURCES[pre]
    key = f[len(pre):]
    cur = dict(getattr(m, attr) or {})
    need(key in SOURCE_KEYS | extra, "Unknown field.")
    clear = {c: "" for c in ("orderCol", "keyCol", *cols)}
    if key == "db":
        need(v in machinedata.TABLE_DBS, "Pick the machine data database or the asset database.")
        if cur.get("db") != v:
            cur.update(table="", **clear)
    elif key == "type":
        need(v in ("", "feed", "db"), "Unknown source.")
        if v == "db":
            cur.setdefault("db", "machines")
    elif key == "runWhen":
        need(v in ("high", "low"), "Pick High or Low.")
    elif key == "maxAge":
        try:
            v = max(0.0, min(1440.0, float(v or 0)))
        except (TypeError, ValueError):
            raise Need("Type the minutes as a number (0 = never).")
    else:
        v = str(v or "").strip()
        need(len(v) <= (200 if key == "ok" else 80 if key in ("keyVal", "field", "textField") else 64), "That is too long.")
        if key == "table" and cur.get("table") != v:
            cur.update(**clear)
    cur[key] = v
    setattr(m, attr, cur)


@cmd("lineFeedTest", "machines.edit")
def line_feed_test(p, ctx):
    """Admin > Machine data > Test: the newest row of the machine's table, run signal or alarm log, and what the
    suite reads from it. Changes nothing."""
    from . import machinedata, machineerrors
    m = line_of(p.get("id"))
    need(m, "Machine not found.")
    which = p.get("which") if p.get("which") in ("uptime", "err") else "feed"
    try:
        if which == "feed":
            need(m.feed_type == "db", "Test reads a database table; set the source to Database table first.")
        elif which == "uptime":
            need((m.uptime_src or {}).get("type") == "db", "Set the run signal to come from a database table first.")
        else:
            need((m.error_src or {}).get("type") == "db", "Set errors to come from an alarm log table first.")
            return {"data": {"rows": machineerrors.preview(m)}}
        return {"data": machinedata.preview(m, which)}
    except integrations.ConnError as e:
        raise Need(str(e))
    except Need:
        raise
    except Exception as e:  # noqa: BLE001
        raise Need(f"Couldn't read it: {str(e)[:200]}")


@cmd("machineTables", "machines.edit")
def machine_tables(p, ctx):
    """Tables and their columns, for the machine-data pickers."""
    from . import machinedata
    db = p.get("db") or "machines"
    need(db in machinedata.TABLE_DBS, "Pick the machine data database or the asset database.")
    integrations.liveconfig.refresh()
    need(db in integrations.connections.databases, f"{integrations.DB_NAMES[db]} is not set up yet. Add it under Databases and accounts above.")
    try:
        return {"data": {"db": db, "tables": machinedata.tables(db)}}
    except integrations.ConnError as e:
        raise Need(str(e))


@cmd("lineSet", "machines.edit")
def line_set(p, ctx):
    m = line_of(p.get("id"))
    need(m, "Machine not found.")
    busy = bool(active_run(m))
    f, v = p.get("f"), p.get("v")
    if f == "id":
        labor_lock("Line names")
        v = str(v or "").strip()
        need(v, "A machine needs a name.")
        need(len(v) <= 60, "Machine names can be up to 60 characters.")
        if v == m.name:
            return
        need(not Machine.objects.filter(name=v).exists(), f"{v} already exists.")
        old = m.name
        m.name = v
        m.save()
        rename_machine_records(old, v)
        audit(ctx, f"Renamed machine {old} to {v}")
        return
    if f == "code":
        m.code = str(v or "").strip().upper()
        need(len(m.code) <= 12, "Process codes can be up to 12 characters.")
    elif f == "rated":
        need(float(v or 0) > 0, "Rated speed must be above 0.")
        m.ppm = float(v)
    elif f == "color":
        need(re.match(r"^#[0-9a-fA-F]{6}$", str(v or "")), "Pick a color.")
        m.color = v
    elif f == "active":
        need(v or not busy, f"A shift is running on {m.name}. End it first.")
        m.active = bool(v)
    elif f == "feed.type":
        need(v in ("demo", "http", "push", "influx", "db", "none"), "Unknown feed type.")
        m.feed_type = v
        if v == "db":
            m.feed_src = {"db": "machines", "maxAge": 3, **(m.feed_src or {})}
        if v == "influx" and not m.feed_query:
            from . import influx
            m.feed_query = influx.default_query(m.name, influx.config()["version"])
    elif f == "feed.url":
        url = str(v or "").strip()
        if url:
            try:
                url = netsafety.check_url(url, f"The machine data address for {m.name}")
            except netsafety.NotPrivate as e:
                raise Need(str(e))
        need(len(url) <= 300, "That address is too long.")
        m.feed_url = url
    elif f == "feed.query":
        from . import influx
        q = str(v or "").strip()
        if q:
            try:
                influx.check_query(q, influx.config()["version"])
            except influx.InfluxError as e:
                raise Need(str(e))
        m.feed_query = q
    elif f == "feed.poll":
        m.feed_poll = max(2, int(float(v or 10)))
    elif f == "feed.ppmField":
        m.feed_ppm_field = str(v or "ppm").strip() or "ppm"
    elif f == "feed.countField":
        m.feed_count_field = str(v or "").strip()[:80]
    elif f == "feed.recipeField":
        m.feed_recipe_field = str(v or "").strip()[:80]
    elif f.startswith(("feed.src.", "uptime.", "err.")):
        _set_source(m, f, v)
    else:
        raise Need("Unknown field.")
    m.save()
    LineState.objects.filter(machine=m).update(feed={})
    audit(ctx, f"{m.name} {f} set to {m.feed_url or '(blank)' if f == 'feed.url' else '(query changed)' if f == 'feed.query' else json.dumps(v)}")
    if f in ("rated", "active"):
        refresh_schedule("a machine change")


@cmd("lineDel", "machines.edit")
def line_del(p, ctx):
    labor_lock("Lines")
    m = line_of(p.get("id"))
    need(m, "Machine not found.")
    need(not active_run(m), f"A shift is running on {m.name}. End it first.")
    name = m.name
    m.delete()
    audit(ctx, f"Removed machine {name}")
    refresh_schedule("removing a machine")


def _shift(i):
    sh = list(Shift.objects.all())
    need(0 <= int(i) < len(sh), "Shift not found.")
    return sh[int(i)]


@cmd("shiftAdd", "lists.edit")
def shift_add(p, ctx):
    labor_lock("Shifts")
    s = Shift.objects.create(name=f"Shift {Shift.objects.count() + 1}", start="23:00", end="06:00", days=[1, 2, 3, 4, 5], sort=Shift.objects.count())
    for m in Machine.objects.all():
        m.shifts.add(s)
    audit(ctx, "Added a shift")


@cmd("shiftSet", "lists.edit")
def shift_set(p, ctx):
    s = _shift(p.get("s"))
    f, v = p.get("f"), str(p.get("v") or "").strip()
    need(f in ("name", "start", "end"), "Unknown field.")
    if f != "name":
        labor_lock("Shift times")
    need(v, "Cannot be blank.")
    if f != "name":
        need(TIME_RE.match(v), "Use a time like 07:00.")
    old = getattr(s, f)
    setattr(s, f, v)
    s.save()
    audit(ctx, f"{s.name} {f} {old} → {v}")
    if f != "name":
        refresh_schedule("a shift change")


@cmd("shiftDel", "lists.edit")
def shift_del(p, ctx):
    labor_lock("Shifts")
    need(Shift.objects.count() > 1, "Keep at least one shift.")
    s = _shift(p.get("s"))
    name = s.name
    s.delete()
    audit(ctx, f"Removed {name}")
    refresh_schedule("a shift change")


def _mins_to_hm(mins):
    mins %= 1440
    return f"{mins // 60:02d}:{mins % 60:02d}"


@cmd("breakAdd", "lists.edit")
def break_add(p, ctx):
    from plant.util import hm
    s = _shift(p.get("s"))
    st = hm(s.start) + 120
    ShiftBreak.objects.create(shift=s, label="Break", start=_mins_to_hm(st), end=_mins_to_hm(st + 10))
    audit(ctx, f"Added a break to {s.name}")
    refresh_schedule("a break change")


@cmd("breakSet", "lists.edit")
def break_set(p, ctx):
    from plant.util import hm
    s = _shift(p.get("s"))
    brs = list(s.breaks.all())
    need(0 <= int(p.get("b", -1)) < len(brs), "Break not found.")
    b = brs[int(p["b"])]
    f, v = p.get("f"), p.get("v")
    if "lunch" in (b.label or "").lower() and f == "mins" or f == "name" and ("lunch" in (b.label or "").lower()) != ("lunch" in str(v or "").lower()):
        labor_lock("Lunch lengths")
    if f == "mins":
        b.end = _mins_to_hm(hm(b.start) + max(1, int(float(v or 0))))
    elif f == "start":
        v = str(v or "").strip()
        need(TIME_RE.match(v), "Use a time like 11:30.")
        ln = (hm(b.end) - hm(b.start)) % 1440
        b.start, b.end = v, _mins_to_hm(hm(v) + ln)
    elif f == "name":
        v = str(v or "").strip()
        need(v, "Cannot be blank.")
        b.label = v
    else:
        raise Need("Unknown field.")
    b.save()
    audit(ctx, f"{s.name}: {b.label} {f} set to {v}")
    refresh_schedule("a break change")


@cmd("breakDel", "lists.edit")
def break_del(p, ctx):
    s = _shift(p.get("s"))
    brs = list(s.breaks.all())
    need(0 <= int(p.get("b", -1)) < len(brs), "Break not found.")
    b = brs[int(p["b"])]
    if "lunch" in (b.label or "").lower():
        labor_lock("Lunches")
    b.delete()
    audit(ctx, f"Removed {b.label} from {s.name}")
    refresh_schedule("a break change")


@cmd("partAdd", "lists.edit")
def part_add(p, ctx):
    k = part_key(p.get("k"))
    need(k, "Type a part number.")
    need(not PartSpec.objects.filter(key=k).exists(), f"{k} is already listed.")
    g = spec_for(k)
    PartSpec.objects.create(key=k, **{f: g[f] for f in ("pleats", "len", "wid", "ph", "sb", "tol", "rev", "media")})
    audit(ctx, f"Added part {k}")


@cmd("partSet", "lists.edit")
def part_set(p, ctx):
    ps = PartSpec.objects.filter(key=p.get("k")).first()
    need(ps, "Part not found.")
    f, v = p.get("f"), p.get("v")
    need(f in ("pleats", "len", "wid", "ph", "sb", "tol", "rev", "media"), "Unknown field.")
    if f in ("rev", "media"):
        v = str(v or "").strip()
    else:
        try:
            v = float(v)
        except (TypeError, ValueError):
            v = 0
        need(v > 0, "Enter a number above 0.")
    setattr(ps, f, v)
    ps.save()
    audit(ctx, f"{ps.key} {f} set to {v}")


@cmd("partDel", "lists.edit")
def part_del(p, ctx):
    ps = PartSpec.objects.filter(key=p.get("k")).first()
    need(ps, "Part not found.")
    ps.delete()
    audit(ctx, f"Removed part {p['k']}")
    return {"msg": f"{p['k']} removed. Open MOs for it use the cost sheet pleat count and estimated specs until it is added back."}


@cmd("ruleSet", "lists.edit")
def rule_set(p, ctx):
    st = settings_obj()
    need(p.get("k") in st.config["rules"], "Unknown rule.")
    try:
        v = max(0, float(p.get("v")))
    except (TypeError, ValueError):
        v = 0
    st.config["rules"][p["k"]] = int(v) if v == int(v) else v
    st.save(update_fields=["config"])
    audit(ctx, f"Rule {p['k']} set to {v:g}")


SETTINGS = {"autoQC": "b", "qcEvery": "n", "s2kUrl": "s", "s2kSource": "s", "s2kEvery": "n", "peopleUrl": "s", "peopleEvery": "n", "webhook": "s", "useDb": "b",
            "grafana.on": "b", "grafana.url": "s", "grafana.uid": "s", "grafana.slug": "s", "grafana.org": "n", "grafana.ppm": "n",
            "grafana.uptime": "n", "grafana.count": "n", "grafana.refresh": "s", "grafana.lineVar": "s",
            "paycom.auto": "b", "paycom.every": "n", "paycom.dept": "s", "paycom.position": "s", "paycom.positionExact": "b", "paycom.pinFrom": "s",
            "kiosk.layout": "s", "kiosk.range": "s", "kiosk.cols": "n", "kiosk.rotate": "n", "kiosk.panels": "j",
            "grafana.report": "j"}


LEAD_SETTINGS = {"autoQC", "qcEvery"}   # floor settings (day-to-day lists); the rest are connection settings


@cmd("settingSet", "user")
def setting_set(p, ctx):
    k = p.get("k")
    t = SETTINGS.get(k)
    need(t, "Unknown setting.")
    need(perms.can(ctx.user, "lists.edit" if k in LEAD_SETTINGS else "connections.edit"),
         f"{ctx.user.name} doesn't have permission to change this setting (Admin › Permissions).")
    v = p.get("v")
    if t == "b":
        v = bool(v)
    elif t == "n":
        try:
            v = max(0, float(v))
        except (TypeError, ValueError):
            v = 0
        v = int(v) if v == int(v) else v
    elif t == "j":
        need(isinstance(v, list), "Expected a list.")
    else:
        v = str("" if v is None else v).strip()
    if k == "qcEvery":
        v = max(5, v or 30)
        if v != settings_obj().settings.get("qcEvery"):
            notify(f"QC checks are now every {v:g} minutes ({ctx.by})", "qc", go="qc-log")
    if k == "paycom.every":
        v = max(15, v or 60)
    if k in ("s2kUrl", "peopleUrl", "webhook", "grafana.url") and v:
        try:
            v = netsafety.check_url(v, {"s2kUrl": "The S2K address", "peopleUrl": "The people list address", "webhook": "The webhook address",
                                        "grafana.url": "The Grafana address"}[k])
        except netsafety.NotPrivate as e:
            raise Need(str(e))
    if k == "s2kSource":
        need(v in ("url", "db"), "Unknown S2K source.")
    if k == "paycom.pinFrom":
        need(v in ("code", "badge", "none"), "Unknown PIN source.")
    if k == "kiosk.layout":
        need(v in ("panels", "dashboard"), "Unknown layout.")
    st = settings_obj()
    o = st.settings
    parts = k.split(".")
    for part in parts[:-1]:
        o = o[part]
    o[parts[-1]] = v
    conn_key = {"s2kUrl": "s2k", "s2kSource": "s2k", "peopleUrl": "people", "webhook": "webhook"}.get(k) or ("grafana" if k.startswith("grafana.") else "paycom" if k.startswith("paycom.") else None)
    if conn_key:
        st.conn.pop(conn_key, None)
    st.save(update_fields=["settings", "conn"])
    audit(ctx, f"Setting {k} set to {'(blank)' if v == '' else v}")


# ------------------------------------------------------------------ connections added after installation
def _db_fields(group, f):
    """The fields typed on the Admin page for a database; a blank password keeps the saved one."""
    from plant import liveconfig
    from plant.envfile import read_env
    prefix, what = liveconfig.DB_GROUPS[group]
    saved = read_env(liveconfig.env_path())
    f = f if isinstance(f, dict) else {}
    out = {"host": str(f.get("host") or "").strip(), "port": str(f.get("port") or "3306").strip(), "name": str(f.get("name") or "").strip(),
           "user": str(f.get("user") or "").strip(), "password": str(f.get("password") or ""), "internet": f.get("internet") in (True, "1", "on", "true"),
           "kind": str(f.get("kind") or "auto") if str(f.get("kind") or "auto") in ("auto", "mysql", "mssql", "postgres") else "auto"}
    if not out["password"] and saved.get(f"{prefix}_HOST"):
        out["password"] = saved.get(f"{prefix}_PASSWORD", "")
    need("\n" not in out["password"] and "\r" not in out["password"] and '"' not in out["password"],
         "That password has a character the settings file can't hold (a line break or a double quote).")
    return prefix, what, out


def _db_test(group, f):
    from django.db import connections

    from plant.dbcheck import test_read_only
    prefix, what, v = _db_fields(group, f)
    db = connections.settings.get("default", {})
    records = (db.get("HOST", ""), str(db.get("PORT", "")), str(db.get("NAME", ""))) if "mysql" in db.get("ENGINE", "") else None
    return prefix, what, v, test_read_only(v["host"], v["port"], v["name"], v["user"], v["password"], what, records=records, internet=v["internet"],
                                           kind=v["kind"])


@cmd("connInfo", "connections.edit")
def conn_info(p, ctx):
    from plant import liveconfig
    return {"data": liveconfig.info()}


def _influx_fields(f):
    """The InfluxDB settings typed on the Admin page; a blank token or password keeps the saved one."""
    from plant import liveconfig
    from plant.envfile import read_env
    from plant.liveconfig import TEXT_RE

    from . import influx
    saved = read_env(liveconfig.env_path())
    f = f if isinstance(f, dict) else {}
    c = {"url": str(f.get("url") or "").strip().rstrip("/"), "version": "1" if str(f.get("version")) == "1" else "2",
         "org": str(f.get("org") or "").strip(), "token": str(f.get("token") or "").strip(), "user": str(f.get("user") or "").strip(),
         "db": str(f.get("db") or "").strip(), "out": str(f.get("out") or "").strip()}
    need(c["url"], "Type the InfluxDB address, for example http://192.168.100.40:8086.")
    try:
        c["url"] = netsafety.check_url(c["url"], "The InfluxDB address").rstrip("/")
    except netsafety.NotPrivate as e:
        raise Need(str(e))
    if not c["token"] and saved.get("INFLUX_URL"):
        c["token"] = saved.get("INFLUX_TOKEN", "")
    for k, label in (("org", "organization"), ("token", "token"), ("user", "user name"), ("db", "database")):
        need(not c[k] or TEXT_RE.match(c[k]), f"That {label} has a character the settings file can't hold.")
    need(not c["out"] or influx.NAME_RE.match(c["out"]), "Use letters, numbers, _ - . or / for the bucket the suite writes to.")
    if c["version"] == "2":
        need(c["org"], "Type the InfluxDB organization (name or ID).")
        need(c["token"], "Paste an InfluxDB API token.")
    return c


@cmd("connTest", "connections.edit")
def conn_test(p, ctx):
    group = p.get("group")
    if group == "influx":
        from . import influx
        try:
            r = influx.test(_influx_fields(p.get("fields")))
        except influx.InfluxError as e:
            r = {"ok": False, "msg": str(e)}
        return {"data": r}
    need(group in ("assets", "s2k", "machines"), "Unknown connection.")
    *_, r = _db_test(group, p.get("fields"))
    return {"data": r}


@cmd("connSave", "connections.edit")
def conn_save(p, ctx):
    from plant import liveconfig
    from plant.liveconfig import TEXT_RE
    group, f = p.get("group"), p.get("fields") if isinstance(p.get("fields"), dict) else {}
    if group in ("assets", "s2k", "machines"):
        prefix, what, v, r = _db_test(group, f)
        need(r["ok"], r["msg"])
        liveconfig.save({f"{prefix}_HOST": v["host"], f"{prefix}_PORT": v["port"], f"{prefix}_NAME": v["name"],
                         f"{prefix}_USER": v["user"], f"{prefix}_PASSWORD": v["password"], f"{prefix}_INTERNET": "1" if v["internet"] else None,
                         f"{prefix}_KIND": r.get("kind") or "mysql"})
        integrations.set_conn(group, True, f"{r.get('tables', 0)} tables (read-only)")
        from plant.dbkinds import KINDS
        audit(ctx, f"{what} connection set to {v['name']} on {v['host']}:{v['port']} as {v['user']} ({KINDS[r.get('kind') or 'mysql']['label']})"
                   + (" (outside the plant network, allowed by this login)" if v["internet"] else ""))
        return {"msg": f"{what} saved. {r['msg']}" + (" Note: this login is allowed to change data; a read-only login is safer." if r.get("writable") else ""),
                "data": liveconfig.info()}
    if group == "paycom":
        sid = str(f.get("sid") or "").strip()
        token = str(f.get("token") or "")
        saved = liveconfig.info()["paycom"]
        need(TEXT_RE.match(sid), "Type the Paycom SID.")
        need(token or saved["hasToken"], "Type the Paycom API token.")
        need(not token or TEXT_RE.match(token), "That token has a character the settings file can't hold.")
        liveconfig.save({"PAYCOM_SID": sid, **({"PAYCOM_TOKEN": token} if token else {})})
        audit(ctx, "Paycom login changed")
        return {"msg": "Paycom login saved. Use Test on the Paycom row to check it.", "data": liveconfig.info()}
    if group == "grafana":
        tok = str(f.get("token") or "").strip()
        need(re.match(r"^[A-Za-z0-9_=.-]{20,300}$", tok), "Paste the Grafana service account token (it starts with glsa_).")
        liveconfig.save({"GRAFANA_TOKEN": tok})
        audit(ctx, "Grafana token changed")
        return {"msg": "Grafana token saved. Reports can now show Grafana panels.", "data": liveconfig.info()}
    if group == "influx":
        from . import influx
        c = _influx_fields(f)
        try:
            r = influx.test(c)
        except influx.InfluxError as e:
            raise Need(f"Not saved: {e}")
        liveconfig.save({"INFLUX_URL": c["url"], "INFLUX_VERSION": c["version"], "INFLUX_ORG": c["org"], "INFLUX_TOKEN": c["token"],
                         "INFLUX_USER": c["user"], "INFLUX_DB": c["db"], "INFLUX_WRITE_BUCKET": c["out"]})
        integrations.set_conn("influx", True, r["msg"])
        PlantSettings.bump()
        st = settings_obj()
        if (st.conn.get("influxOut") or {}).get("bucket") != c["out"]:
            st.conn["influxOut"] = {"bucket": c["out"]}  # a new bucket starts from the last 7 days again
            st.save(update_fields=["conn"])
        audit(ctx, f"InfluxDB connection set to {c['url']}" + (f", writing suite records to {c['out']}" if c["out"] else ""))
        return {"msg": f"InfluxDB saved. {r['msg']}." + (f" Suite records will be copied to {c['out']} every minute." if c["out"] else ""),
                "data": liveconfig.info()}
    if group == "github":
        from plant import ghupdates
        repo = str(f.get("repo") or "").strip() or ghupdates.DEFAULT_REPO
        token = str(f.get("token") or "").strip()
        saved = liveconfig.info()["github"]
        need(ghupdates.REPO_RE.match(repo), "The repository should look like owner/name, e.g. phazercil/Production-Suite.")
        need(token or saved["hasToken"], "Paste a GitHub token that can read the repository.")
        need(not token or re.match(r"^[A-Za-z0-9_]{20,255}$", token), "That doesn't look like a GitHub token (they start with github_pat_ or ghp_).")
        try:   # only save a login that works
            ghupdates.check({"repo": repo, "token": token or dj_settings.SUITE.get("GITHUB_UPDATES_TOKEN", ""), "daily": False})
        except ghupdates.GHError as e:
            raise Need(f"Not saved: {e}")
        liveconfig.save({"GITHUB_UPDATES_REPO": repo, "GITHUB_UPDATES_CHECK": "1" if f.get("daily") else None, **({"GITHUB_UPDATES_TOKEN": token} if token else {})})
        audit(ctx, f"Updates from GitHub set up ({repo}{', checked daily' if f.get('daily') else ''})")
        return {"msg": "Updates from GitHub saved. The token works.", "data": liveconfig.info()}
    if group == "feed":
        key = str(f.get("key") or "").strip() or liveconfig.new_feed_key()
        need(re.match(r"^[A-Za-z0-9_-]{16,128}$", key), "Use 16 to 128 letters, numbers, - or _ for the feed key.")
        liveconfig.save({"FEED_KEY": key})
        audit(ctx, "Machine feed key changed")
        return {"msg": "Feed key saved. Machines that push readings must send it in the X-Feed-Key header.", "data": liveconfig.info(), "key": key}
    raise Need("Unknown connection.")


@cmd("connRemove", "connections.edit")
def conn_remove(p, ctx):
    from plant import liveconfig
    group = p.get("group")
    keys = {"assets": ["ASSET_DB_HOST", "ASSET_DB_PORT", "ASSET_DB_NAME", "ASSET_DB_USER", "ASSET_DB_PASSWORD", "ASSET_DB_INTERNET", "ASSET_DB_KIND"],
            "s2k": ["S2K_DB_HOST", "S2K_DB_PORT", "S2K_DB_NAME", "S2K_DB_USER", "S2K_DB_PASSWORD", "S2K_DB_INTERNET", "S2K_DB_KIND"],
            "machines": ["MACHINE_DB_HOST", "MACHINE_DB_PORT", "MACHINE_DB_NAME", "MACHINE_DB_USER", "MACHINE_DB_PASSWORD", "MACHINE_DB_INTERNET", "MACHINE_DB_KIND"],
            "paycom": ["PAYCOM_SID", "PAYCOM_TOKEN"], "feed": ["FEED_KEY"], "grafana": ["GRAFANA_TOKEN"],
            "github": ["GITHUB_UPDATES_REPO", "GITHUB_UPDATES_TOKEN", "GITHUB_UPDATES_CHECK"],
            "influx": ["INFLUX_URL", "INFLUX_VERSION", "INFLUX_ORG", "INFLUX_TOKEN", "INFLUX_USER", "INFLUX_DB", "INFLUX_WRITE_BUCKET"]}.get(group)
    need(keys, "Unknown connection.")
    liveconfig.save({k: None for k in keys})
    st = settings_obj()
    if group in st.conn or (group == "influx" and "influxOut" in st.conn):
        st.conn.pop(group, None)
        if group == "influx":
            st.conn.pop("influxOut", None)
        st.save(update_fields=["conn"])
    audit(ctx, f"Removed the {group} connection")
    return {"msg": "Connection removed.", "data": liveconfig.info()}


# ------------------------------------------------------------------ QC items per stage (Admin > QC checks)
def _qc_list(st, stage):
    from plant.models import STAGES

    from . import qc
    need(stage in dict(STAGES), "Unknown stage.")
    cur = [{"key": i["key"]} if i["builtin"] else {k: i[k] for k in ("key", "label", "type", "min", "max")} for i in qc.items_for(stage, st.config)]
    return cur


def _qc_store(st, stage, items, ctx, what):
    cfg = dict(st.config)
    cfg["qcItems"] = {**(cfg.get("qcItems") or {}), stage: items}
    st.config = cfg
    st.save(update_fields=["config"])
    audit(ctx, f"QC checks ({stage}): {what}")
    names = list(Machine.objects.filter(stage=stage, active=True).values_list("name", flat=True))
    if names:
        notify(f"QC check items changed by {ctx.by}: {what}", "qc", lines=names, go="qc-log")


@cmd("qcItemAdd", "lists.edit")
def qc_item_add(p, ctx):
    from . import qc
    st = settings_obj()
    stage = p.get("stage")
    items = _qc_list(st, stage)
    key = p.get("key") or ""
    if key in qc.BUILTIN:
        need(all(i["key"] != key for i in items), "That item is already on the list.")
        items.append({"key": key})
        what = qc.BUILTIN[key]["label"]
    else:
        label = str(p.get("label") or "").strip()[:60]
        need(label, "Name the check.")
        typ = p.get("type")
        need(typ in qc.TYPES, "Pick what kind of check it is.")
        lo, hi = (to_number(p.get(k)) if str(p.get(k) or "").strip() else None for k in ("min", "max"))
        need(lo is None or hi is None or lo <= hi, "The lowest allowed value is above the highest.")
        n = 1 + max([int(i["key"][1:]) for i in items if i["key"][1:].isdigit()] + [0])
        items.append({"key": f"c{n}", "label": label, "type": typ, "min": lo if typ == "number" else None, "max": hi if typ == "number" else None})
        what = label
    _qc_store(st, stage, items, ctx, f"added {what}")


@cmd("qcItemDel", "lists.edit")
def qc_item_del(p, ctx):
    st = settings_obj()
    stage = p.get("stage")
    items = _qc_list(st, stage)
    i = int(p.get("i") or -1)
    need(0 <= i < len(items), "That item is no longer on the list.")
    gone = items.pop(i)
    _qc_store(st, stage, items, ctx, f"removed {gone.get('label') or gone['key']}")


@cmd("qcItemMove", "lists.edit")
def qc_item_move(p, ctx):
    st = settings_obj()
    stage = p.get("stage")
    items = _qc_list(st, stage)
    i, d = int(p.get("i") or 0), int(p.get("d") or 0)
    j = i + d
    need(0 <= i < len(items) and 0 <= j < len(items), "Can't move that item further.")
    items[i], items[j] = items[j], items[i]
    _qc_store(st, stage, items, ctx, "reordered")


# ------------------------------------------------------------------ permissions (Admin > Permissions)
def _perm_cfg(st):
    cfg = dict(st.config)
    p = cfg.get("perms") if isinstance(cfg.get("perms"), dict) else {}
    p = {"roles": dict(p.get("roles") or {}), "people": {k: dict(v) for k, v in (p.get("people") or {}).items()}}
    cfg["perms"] = p
    return cfg, p


@cmd("permRole", "permissions.manage")
def perm_role(p, ctx):
    key, role, on = p.get("key"), p.get("role"), bool(p.get("v"))
    need(key in perms.PERMS, "Unknown permission.")
    need(role in dict(perms.ROLES), "Unknown role.")
    need((key, role) not in perms.LOCKED or on, "The Admin role always keeps Manage permissions.")
    st = settings_obj()
    cfg, pc = _perm_cfg(st)
    roles = [r for r, _ in perms.ROLES if (r == role and on) or (r != role and perms.role_has(key, r, cfg["perms"]))]
    pc["roles"][key] = roles
    st.config = cfg
    st.save(update_fields=["config"])
    audit(ctx, f"Permission {perms.PERMS[key]['label']}: {dict(perms.ROLES)[role]} {'allowed' if on else 'not allowed'}")


@cmd("permPerson", "permissions.manage")
def perm_person(p, ctx):
    key, v = p.get("key"), p.get("v") or ""
    u = Person.objects.filter(pk=p.get("id")).first()
    need(u, "Person not found.")
    need(key in perms.PERMS, "Unknown permission.")
    need(v in ("", "allow", "deny"), "Pick allow, deny or follow the role.")
    st = settings_obj()
    cfg, pc = _perm_cfg(st)
    rules = pc["people"].setdefault(str(u.pk), {})
    if v:
        rules[key] = v == "allow"
    else:
        rules.pop(key, None)
    if not rules:
        pc["people"].pop(str(u.pk), None)
    st.config = cfg
    st.save(update_fields=["config"])
    audit(ctx, f"Permission {perms.PERMS[key]['label']} for {u.name}: {'allowed' if v == 'allow' else 'denied' if v == 'deny' else 'follows their role'}")


@cmd("permReset", "permissions.manage")
def perm_reset(p, ctx):
    st = settings_obj()
    cfg = dict(st.config)
    cfg.pop("perms", None)
    st.config = cfg
    st.save(update_fields=["config"])
    audit(ctx, "Permissions reset to the defaults")


# ------------------------------------------------------------------ updates (Admin > Updates)
def _upd():
    from plant import updates
    return updates


def _upd_call(fn, *a):
    up = _upd()
    try:
        fn(*a)
    except up.UpdError as e:
        raise Need(str(e))
    return {"data": up.info()}


@cmd("updInfo", "updates.install")
def upd_info(p, ctx):
    return {"data": _upd().info()}


@cmd("updWhen", "updates.install")
def upd_when(p, ctx):
    r = _upd_call(_upd().schedule, str(p.get("when") or ""), str(p.get("at") or ""), ctx.by)
    audit(ctx, f"Update set to install {p.get('when')} {p.get('at') or ''}".strip())
    return r


@cmd("updCancel", "updates.install")
def upd_cancel(p, ctx):
    r = _upd_call(_upd().cancel, ctx.by)
    audit(ctx, "Waiting update removed")
    return r


@cmd("updGoBack", "updates.install")
def upd_goback(p, ctx):
    r = _upd_call(_upd().request_goback, ctx.by)
    audit(ctx, "Asked to go back to the previous version")
    return {**r, "msg": "Going back to the previous version within a minute. Screens reconnect by themselves."}


@cmd("updFolder", "updates.install")
def upd_folder(p, ctx):
    r = _upd_call(_upd().set_policy, str(p.get("when") or ""))
    audit(ctx, f"Update folder files install: {p.get('when')}")
    return r


# ------------------------------------------------------------------ incoming data sources
def _src_json(x):
    from . import inbound
    return {"id": x.id, "name": x.name, "kind": x.kind, "line": x.line, "mode": x.mode, "url": x.url, "every": x.every,
            "mapping": x.mapping or {}, "enabled": x.enabled, "hasKey": bool(x.key_hash), "lastAt": ms(x.last_at), "lastOk": x.last_ok,
            "lastMsg": x.last_msg, "received": x.received, "sample": x.sample, "fields": inbound.field_paths(x.sample or {})}


@cmd("srcList", "connections.edit")
def src_list(p, ctx):
    from .models import DataSource
    return {"data": [_src_json(x) for x in DataSource.objects.all()]}


@cmd("srcSave", "connections.edit")
def src_save(p, ctx):
    from . import inbound
    from .models import DataSource
    f = p.get("src") if isinstance(p.get("src"), dict) else {}
    x = DataSource.objects.filter(pk=f.get("id")).first() if f.get("id") else DataSource()
    need(x is not None, "That data source no longer exists.")
    name = str(f.get("name") or "").strip()
    need(inbound.NAME_RE.match(name), "Name the source with 2 to 60 letters, numbers, - or _ (it becomes part of its address), for example vision-barbara.")
    need(not DataSource.objects.filter(name__iexact=name).exclude(pk=x.pk).exists(), f"There is already a data source named {name}.")
    kind = f.get("kind") if f.get("kind") in ("qc", "readings") else "qc"
    mode = f.get("mode") if f.get("mode") in ("push", "poll") else "push"
    line = str(f.get("line") or "").strip()
    need(not line or Machine.objects.filter(name=line).exists(), "Pick a line from the list.")
    url = str(f.get("url") or "").strip()
    if mode == "poll":
        need(url, "Type the address to collect the data from.")
        try:
            url = netsafety.check_url(url, "The data source address")
        except netsafety.NotPrivate as e:
            raise Need(str(e))
    try:
        every = max(10, min(86400, int(float(f.get("every") or 60))))
    except (TypeError, ValueError):
        every = 60
    allowed = set(inbound.MEASURES) | {"time", "line", "part", "mo", "passed", "units", "id", "fields"}
    mapping = {}
    for k, v in (f.get("mapping") or {}).items() if isinstance(f.get("mapping"), dict) else []:
        if k not in allowed:
            continue
        if k == "fields":
            mapping[k] = [str(i).strip()[:120] for i in (v if isinstance(v, list) else str(v).split(",")) if str(i).strip()][:50]
        elif str(v or "").strip():
            mapping[k] = str(v).strip()[:120]
    if mapping.get("units") not in (None, "in", "mm"):
        mapping.pop("units")
    new = not x.pk
    x.name, x.kind, x.mode, x.line, x.url, x.every, x.mapping = name, kind, mode, line, url if mode == "poll" else "", every, mapping
    x.enabled = f.get("enabled") is not False
    key = None
    if mode == "push" and not x.key_hash:
        key = inbound.new_key()
        x.key_hash = inbound.key_hash(key)
    x.next_poll = None
    x.save()
    audit(ctx, f"{'Added' if new else 'Changed'} data source {name} ({kind}, {mode})")
    return {"msg": f"Data source {name} saved.", "data": _src_json(x), "key": key}


@cmd("srcNewKey", "connections.edit")
def src_new_key(p, ctx):
    from . import inbound
    from .models import DataSource
    x = DataSource.objects.filter(pk=p.get("id")).first()
    need(x, "That data source no longer exists.")
    key = inbound.new_key()
    x.key_hash = inbound.key_hash(key)
    x.save(update_fields=["key_hash"])
    audit(ctx, f"New key for data source {x.name}")
    return {"msg": "New key made. The old key stops working now.", "key": key, "data": _src_json(x)}


@cmd("srcDelete", "connections.edit")
def src_delete(p, ctx):
    from .models import DataSource
    x = DataSource.objects.filter(pk=p.get("id")).first()
    need(x, "That data source no longer exists.")
    name = x.name
    x.delete()  # data it already sent is kept (marked with the source's name)
    audit(ctx, f"Removed data source {name}")
    return {"msg": f"Data source {name} removed. The data it already sent is kept."}


@cmd("srcTest", "connections.edit")
def src_test(p, ctx):
    """Collect once without storing (poll), or show what the last data received would be read as (push)."""
    from . import inbound, integrations
    from .models import DataSource
    x = DataSource.objects.filter(pk=p.get("id")).first()
    need(x, "Save the data source first.")
    if x.mode == "poll":
        try:
            data = integrations.get_json(x.url)
        except integrations.ConnError as e:
            raise Need(str(e))
        items = data if isinstance(data, list) else (data.get("items") or data.get("data") or data.get("results") or [data]) if isinstance(data, dict) else []
        sample = items[-1] if items and isinstance(items[-1], dict) else None
        if sample:
            x.sample = {k: v for k, v in list(sample.items())[:60]}
            x.save(update_fields=["sample"])
        return {"data": {"count": len(items), "preview": inbound.preview(x, items[-10:]), "src": _src_json(x)}}
    need(x.sample, "Nothing received yet. Send a test from the other system, then try again.")
    return {"data": {"count": 1, "preview": inbound.preview(x, x.sample), "src": _src_json(x)}}


# ------------------------------------------------------------------ downtime alerts
def _contact_json(c):
    return {"id": c.id, "name": c.name, "email": c.email, "phone": c.phone, "lines": c.lines or [], "downAfter": c.down_after,
            "notifyBack": c.notify_back, "active": c.active}


@cmd("alertInfo", "alerts.edit")
def alert_info(p, ctx):
    from plant import liveconfig

    from .models import AlertContact, AlertLog
    info = liveconfig.info()
    return {"data": {"contacts": [_contact_json(c) for c in AlertContact.objects.all()], "smtp": info["smtp"], "sms": info["sms"],
                     "log": [{"t": ms(x.t), "kind": x.kind, "line": x.line, "contact": x.contact, "channel": x.channel, "to": x.to, "ok": x.ok,
                              "msg": x.msg} for x in AlertLog.objects.exclude(channel="none")[:40]]}}


@cmd("alertContactSave", "alerts.edit")
def alert_contact_save(p, ctx):
    from . import alerts
    from .models import AlertContact
    f = p.get("contact") if isinstance(p.get("contact"), dict) else {}
    c = AlertContact.objects.filter(pk=f.get("id")).first() if f.get("id") else AlertContact()
    need(c is not None, "That person is no longer on the list.")
    name = str(f.get("name") or "").strip()[:120]
    need(name, "Type the person's name.")
    email = str(f.get("email") or "").strip()[:200]
    need(not email or alerts.EMAIL_RE.match(email), f"{email} doesn't look like an email address.")
    try:
        phone = alerts.phone_e164(f.get("phone"))
    except alerts.AlertError as e:
        raise Need(str(e))
    need(email or phone, "Enter an email address, a mobile number, or both.")
    names = set(Machine.objects.values_list("name", flat=True))
    lines = [x for x in (f.get("lines") or []) if x in names] if isinstance(f.get("lines"), list) else []
    try:
        after = max(1, min(1440, int(float(f.get("downAfter") or 15))))
    except (TypeError, ValueError):
        after = 15
    c.name, c.email, c.phone, c.lines, c.down_after = name, email, phone, lines, after
    c.notify_back, c.active = f.get("notifyBack") is not False, f.get("active") is not False
    c.save()
    audit(ctx, f"Downtime alerts: saved {name}")
    return {"msg": f"{name} saved.", "data": _contact_json(c)}


@cmd("alertContactDelete", "alerts.edit")
def alert_contact_delete(p, ctx):
    from .models import AlertContact
    c = AlertContact.objects.filter(pk=p.get("id")).first()
    need(c, "That person is no longer on the list.")
    c.delete()
    audit(ctx, f"Downtime alerts: removed {c.name}")
    return {"msg": f"{c.name} removed."}


@cmd("alertTest", "alerts.edit")
def alert_test(p, ctx):
    from . import alerts
    from .models import AlertContact
    c = AlertContact.objects.filter(pk=p.get("id")).first()
    need(c, "Save the person first.")
    res = alerts.deliver(c.name, c.email, c.phone, "Production Suite test", f"Test message from Production Suite, sent by {ctx.by}. "
                         "Downtime alerts will look like this.", "test")
    need(res, "This person has no email or mobile number.")
    bad = [r for r in res if not r["ok"]]
    if bad:
        raise Need("; ".join(f"{r['channel']} to {r['to']}: {r['msg']}" for r in bad))
    return {"msg": "Test sent: " + ", ".join(f"{r['channel']} to {r['to']}" for r in res)}


@cmd("alertConfigSave", "alerts.edit")
def alert_config_save(p, ctx):
    from plant import liveconfig
    from plant.liveconfig import TEXT_RE

    from . import alerts
    group = p.get("group")
    f = p.get("fields") if isinstance(p.get("fields"), dict) else {}
    if group == "smtp":
        host = str(f.get("host") or "").strip()
        need(re.match(r"^[A-Za-z0-9._-]{1,253}$", host), "Type the mail server's name or address.")
        port = str(f.get("port") or "25").strip()
        need(port.isdigit() and 0 < int(port) < 65536, "Use a port number like 25, 587 or 465.")
        sec = f.get("security") if f.get("security") in ("starttls", "ssl", "none") else "starttls"
        frm = str(f.get("from") or "").strip()
        need(alerts.EMAIL_RE.match(frm), "Type the address messages come from, e.g. plant-alerts@yourcompany.com.")
        user = str(f.get("user") or "").strip()
        pw = str(f.get("password") or "")
        need(not user or TEXT_RE.match(user), "That user name has a character the settings file can't hold.")
        need(not pw or TEXT_RE.match(pw), "That password has a character the settings file can't hold (a line break or a double quote).")
        internet = bool(f.get("internet"))
        if not internet:
            try:
                netsafety.check_host(host, "The mail server")
            except netsafety.NotPrivate as e:
                raise Need(f"{e} If your mail server is a cloud service (for example Office 365), tick \"the mail server is on the internet\".")
        changes = {"ALERT_SMTP_HOST": host, "ALERT_SMTP_PORT": port, "ALERT_SMTP_SECURITY": sec, "ALERT_FROM": frm,
                   "ALERT_SMTP_USER": user or None, "ALERT_SMTP_INTERNET": "1" if internet else None}
        if pw:
            changes["ALERT_SMTP_PASSWORD"] = pw
        elif not user:
            changes["ALERT_SMTP_PASSWORD"] = None
        liveconfig.save(changes)
        audit(ctx, f"Downtime alerts: mail server set to {host}:{port}" + (" (internet)" if internet else ""))
        return {"msg": "Mail server saved. Use Send a test on a person to check it.", "data": liveconfig.info()}
    if group == "sms":
        sid = str(f.get("sid") or "").strip()
        need(re.match(r"^AC[0-9a-fA-F]{32}$", sid), "The Twilio Account SID starts with AC followed by 32 letters and numbers.")
        tok = str(f.get("token") or "").strip()
        need(tok or liveconfig.info()["sms"]["hasToken"], "Type the Twilio auth token.")
        need(not tok or re.match(r"^[0-9a-zA-Z]{20,64}$", tok), "That doesn't look like a Twilio auth token.")
        try:
            frm = alerts.phone_e164(f.get("from"))
        except alerts.AlertError as e:
            raise Need(str(e))
        need(frm, "Type the Twilio phone number texts come from.")
        liveconfig.save({"TWILIO_SID": sid, "TWILIO_FROM": frm, **({"TWILIO_TOKEN": tok} if tok else {})})
        audit(ctx, "Downtime alerts: Twilio text messages set up")
        return {"msg": "Twilio saved. Use Send a test on a person to check it.", "data": liveconfig.info()}
    if group in ("smtp-remove", "sms-remove"):
        keys = (["ALERT_SMTP_HOST", "ALERT_SMTP_PORT", "ALERT_SMTP_SECURITY", "ALERT_SMTP_USER", "ALERT_SMTP_PASSWORD", "ALERT_FROM", "ALERT_SMTP_INTERNET"]
                if group == "smtp-remove" else ["TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM"])
        liveconfig.save({k: None for k in keys})
        audit(ctx, f"Downtime alerts: {'mail server' if group == 'smtp-remove' else 'Twilio'} removed")
        return {"msg": "Removed.", "data": liveconfig.info()}
    raise Need("Unknown setting.")


@cmd("networkSet", "network.edit")
def network_set(p, ctx):
    """Change the plant network: the addresses screens may connect from, and that the server may connect to."""
    raw = p.get("allowed")
    need(isinstance(raw, list) and all(isinstance(x, str) for x in raw), "Expected a list of network ranges.")
    try:
        nets = netsafety.parse_networks(raw)
    except netsafety.BadNetwork as e:
        raise Need(str(e))
    # Never let a save lock out the screen making it (the server itself is always allowed, so it can fix anything).
    if not netsafety.is_loopback(ctx.client_ip):
        need(netsafety.is_allowed_ip(ctx.client_ip, nets),
             f"This device ({ctx.client_ip or 'unknown address'}) is not inside those ranges, so saving would lock it out. "
             "Include its range, or make this change on the server itself.")
    old = [str(n) for n in netsafety.current()]
    netsafety.save(nets, by=ctx.by, sync=False)
    new = [str(n) for n in nets]
    transaction.on_commit(netsafety.invalidate)
    transaction.on_commit(lambda: netsafety.sync_and_record(nets))
    audit(ctx, f"Plant network changed from {', '.join(old)} to {', '.join(new)}")
    return {"msg": "Plant network saved: " + ", ".join(new)}


@cmd("networkFirewallSync", "network.edit")
def network_firewall_sync(p, ctx):
    transaction.on_commit(lambda: netsafety.sync_and_record())
    return {"msg": "Checking Windows Firewall…"}


@cmd("configImport", "backup.manage")
def config_import(p, ctx):
    o = p.get("data") or {}
    need(isinstance(o.get("config"), dict) and isinstance(o.get("lines"), list), "Settings text is missing the machines or config section.")
    st = settings_obj()
    cfg = dict(o["config"])
    for k in LISTS:
        if isinstance(cfg.get(k), list):
            st.config[k] = cfg[k]
    if isinstance(cfg.get("rules"), dict):
        st.config["rules"].update(cfg["rules"])
    st.save(update_fields=["config"])
    for k, v in (cfg.get("parts") or {}).items():
        PartSpec.objects.update_or_create(key=part_key(k), defaults={f: v[f] for f in ("pleats", "len", "wid", "ph", "sb", "tol", "rev", "media") if f in v})
    for ln in o["lines"]:
        m = Machine.objects.filter(name=ln.get("id")).first()
        if m:
            m.code, m.ppm, m.color = ln.get("code", m.code), ln.get("rated", m.ppm), ln.get("color", m.color)
            m.save()
    for u in o.get("users") or []:
        ex = Person.objects.filter(name=u.get("name")).first()
        if not ex:
            ex = Person.objects.create(name=u.get("name"), role=u.get("role") or "operator", pin="")
        ex.trained.set(Machine.objects.filter(name__in=u.get("trained") or []))
    audit(ctx, "Loaded settings from another station")
    return {"msg": "Settings loaded"}


@cmd("configReset", "backup.manage")
def config_reset(p, ctx):
    st = settings_obj()
    rules_ = DEFAULT_CONFIG["rules"]
    st.config = {**{k: list(DEFAULT_CONFIG[k]) for k in LISTS}, "rules": dict(rules_)}
    st.save(update_fields=["config"])
    audit(ctx, "Restored default lists and rules")


@cmd("dataReset", "backup.manage")
def data_reset(p, ctx):
    if p.get("mode") == "demo":
        from plant.demo import seed_demo
        seed_demo(reset=True)
        return {"msg": "Demo data loaded", "relogin": True}
    for model in (ShiftRun, PpmReading, Downtime, QCCheck, RollTag, Ticket, ShiftNote, MoRun, Notice, Event, ChangeoverCache, ToolingCache, MachineError):
        model.objects.all().delete()
    MO.objects.all().delete()
    Machine.objects.filter(feed_type="demo").update(feed_type="none")
    LineState.objects.all().delete()
    st = settings_obj()
    st.demo = False
    st.save(update_fields=["demo"])
    audit(ctx, "Cleared MOs and records and went live")
    return {"msg": "Records cleared"}


# ---------------------------------------------------------- connections & asset database
@cmd("testConn", "connections.edit")
def test_conn(p, ctx):
    ok, msg = integrations.test(p.get("k"))
    return {"data": {"ok": ok, "msg": msg}}


@cmd("testAll", "connections.edit")
def test_all(p, ctx):
    keys = ["store", "grafana", "assets", "paycom", "s2k", "people", "webhook"] + [f"feed:{m.name}" for m in Machine.objects.filter(stage="s1")]
    res = [integrations.test(k) for k in keys]
    return {"msg": f"{sum(1 for ok, _ in res if ok)} of {len(res)} connections OK"}


QUERY_DB = {"changeover_steps": "assets", "asset_settings": "assets", "part_spec": "assets", "part_tooling": "assets", "s2k_mos": "s2k"}


def _which_db(p):
    db = p.get("db") or "assets"
    need(db in integrations.DB_NAMES, "Unknown database.")
    need(db in integrations.connections.databases, f"{integrations.DB_NAMES[db]} is not set up on the server. Run the installer again to add it.")
    return db


@cmd("dbSchema", "connections.edit")
def db_schema(p, ctx):
    try:
        return {"data": integrations.schema(_which_db(p))}
    except integrations.ConnError as e:
        raise Need(str(e))


@cmd("dbPreview", "connections.edit")
def db_preview(p, ctx):
    try:
        return {"data": integrations.preview(str(p.get("table") or ""), _which_db(p))}
    except integrations.ConnError as e:
        raise Need(str(e))


@cmd("dbTest", "connections.edit")
def db_test(p, ctx):
    db = _which_db(p)
    try:
        return {"data": integrations.run_saved(str(p.get("sql") or ""), p.get("params") or {}, limit=500, db=db)}
    except Need:
        raise
    except Exception as e:
        raise Need(str(e))


@cmd("connectorSave", "connections.edit")
def connector_save(p, ctx):
    """Save one group of links from the connector builder: a machine's readings, S2K's open MOs, or a changeover query."""
    from . import connector
    gs = {g["key"]: g for g in connector.groups()}
    g = gs.get(str(p.get("group") or ""))
    need(g, "Unknown data need.")
    db, table = str(p.get("db") or g["db"]), str(p.get("table") or "")
    need(db in g["dbs"], f"{g['title']} needs a column from the {' or '.join(connector.DB_LABEL[d] for d in g['dbs'])}.")
    need(db in connector.ready_dbs(), f"{connector.DB_LABEL[db]} isn't set up yet (Admin › Connections).")
    mp = p.get("map") if isinstance(p.get("map"), dict) else {}
    slots = {s_["k"]: s_ for s_ in g["slots"]}
    mp = {k: str(v) for k, v in mp.items() if k in slots and str(v or "").strip()}
    need(table, "Drag a column in first: the group takes its table from it.")
    try:
        sch = {t["name"]: {c["name"] for c in t["columns"]} for t in integrations.schema(db)["tables"]}
    except integrations.ConnError as e:
        raise Need(str(e))
    need(table in sch, f'There is no table "{table}" in the {connector.DB_LABEL[db]}.')
    bad = [c for c in mp.values() if c not in sch[table]]
    need(not bad, f'The table "{table}" has no column ' + ", ".join(f'"{c}"' for c in bad) + ".")
    miss = [slots[k]["label"] for k in slots if slots[k]["req"] and not mp.get(k)]
    need(not miss, "Still needed: " + ", ".join(miss) + ".")
    if g["kind"] == "machine":
        m = Machine.objects.select_for_update().get(pk=int(g["key"][2:]))
        key_val = str(p.get("keyVal") or "").strip()[:120]
        need(not mp.get("keyCol") or key_val, "Type the value that marks this machine's rows.")
        # one table can log every machine: then each machine takes only its own rows
        others = [o for o in Machine.objects.filter(active=True, feed_type="db").exclude(pk=m.pk)
                  if (o.feed_src or {}).get("db", "machines") == db and (o.feed_src or {}).get("table") == table]
        if others:
            names = ", ".join(o.name for o in others)
            need(mp.get("keyCol"), f"{names} also read{'s' if len(others) == 1 else ''} {table}. Wire \"Which machine\" to the column "
                                   "that names the machine, so each machine takes only its own rows.")
            same = [o.name for o in others if (o.feed_src or {}).get("keyCol") == mp["keyCol"]
                    and str((o.feed_src or {}).get("keyVal", "")).strip().lower() == key_val.lower()]
            need(not same, f"{', '.join(same)} already reads the rows where {mp['keyCol']} = {key_val}. Give each machine its own value.")
        src = dict(m.feed_src or {}) if m.feed_type == "db" else {}
        src.update(db=db, table=table, orderCol=mp["orderCol"], keyCol=mp.get("keyCol", ""), keyVal=key_val if mp.get("keyCol") else "")
        src.setdefault("maxAge", 3)
        m.feed_type, m.feed_src = "db", src
        m.feed_ppm_field, m.feed_count_field, m.feed_recipe_field = mp["ppm"], mp.get("count", ""), mp.get("recipe", "")
        up = dict(m.uptime_src or {})
        if mp.get("run"):
            m.uptime_src = {"type": "feed", "field": mp["run"], "runWhen": up.get("runWhen") or "high"}
        elif up.get("type") == "feed":
            m.uptime_src = {}
        er = dict(m.error_src or {})
        if mp.get("errCode") or mp.get("errText"):
            m.error_src = {"type": "feed", "field": mp.get("errCode", ""), "textField": mp.get("errText", ""), "ok": er.get("ok", "")}
        elif er.get("type") == "feed":
            m.error_src = {}
        m.save()
        LineState.objects.filter(machine=m).update(feed={})
        audit(ctx, f"Connector builder: {m.name} reads {table} in the {connector.DB_LABEL[db]} ("
                   + ", ".join(f"{slots[k]['label'].split(' (')[0]} = {v}" for k, v in mp.items()) + ")")
        return {"msg": f"{m.name} now reads its data from {table}"}
    from . import kadmin
    try:
        if g["kind"] == "s2k":
            fval = str(p.get("fval") or "").strip()
            sql = kadmin.s2k_build_sql({"table": table, **mp, "fval": fval})
            name = "s2k_mos"
        else:
            name = g["key"][2:]
            sql = kadmin.build_codata_sql(name, {"table": table, **mp})
    except ValueError as e:
        raise Need(str(e))
    db_save({"name": name, "sql": sql, "params": {}}, ctx)
    if g["kind"] == "s2k":
        st = settings_obj()
        st.settings["s2kSource"] = "db"
        st.save(update_fields=["settings"])
    return {"msg": f"{g['title']} saved: {table}"}


@cmd("dbSave", "connections.edit")
def db_save(p, ctx):
    name = p.get("name")
    need(name in QUERY_DB, "Unknown query.")
    if not p.get("sql"):
        SavedQuery.objects.filter(name=name).delete()
        audit(ctx, f"Stopped using the {name} query")
        return
    db = _which_db({"db": QUERY_DB[name]})
    try:
        r = integrations.run_saved(p["sql"], p.get("params") or {}, limit=1, db=db)
    except Exception as e:
        raise Need(f"The query failed: {e}")
    if name == "s2k_mos":
        cols = {c.lower() for c in r["columns"]}
        missing = [c for c in ("mo", "item", "qty") if c not in cols]
        need(not missing, "The S2K query needs columns named " + ", ".join(missing) + " (use AS to rename them).")
    SavedQuery.objects.update_or_create(name=name, defaults={"sql": p["sql"], "params": p.get("params") or {}, "saved_by": ctx.by, "saved_at": ctx.now})
    audit(ctx, f"Saved the {name} query")


@cmd("dbQueries", "connections.edit")
def db_queries(p, ctx):
    return {"data": {q.name: {"sql": q.sql, "savedBy": q.saved_by, "savedAt": ms(q.saved_at)} for q in SavedQuery.objects.all()}}


# ------------------------------------------------------------------ dispatcher
def execute(name, p, ctx):
    fn = CMD.get(name)
    if not fn:
        return {"ok": False, "error": f'Unknown action "{name}".'}
    role = ROLE[name]
    if role != "none" and not ctx.user:
        return {"ok": False, "error": "Log in first.", "login": True}
    if role in perms.PERMS and not perms.can(ctx.user, role):   # Admin › Permissions
        what = perms.PERMS[role]["label"]
        return {"ok": False, "error": f"{ctx.user.name} doesn't have permission for this ({what}). Log in as someone who does.",
                "admin": True, "perm": role}
    try:
        with transaction.atomic():
            r = fn(p or {}, ctx) or {}
        PlantSettings.bump()
        return {"ok": True, **r}
    except Need as e:
        return {"ok": False, "error": str(e)}
    except integrations.ConnError as e:
        return {"ok": False, "error": str(e)}
    except Exception:
        # Anything unexpected: the transaction above has already rolled back, so nothing half-changed is kept.
        log.exception("command %s failed", name)
        return {"ok": False, "error": "The server hit an error and nothing was changed. Try again; if it keeps happening, tell the shift lead."}
