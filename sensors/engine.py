"""Sensors: taking a reading, scaling it, the alarm rules and what an alarm does.

Called by the ticker (hmi/ticker.py) every few seconds: `tick(t)` reads the sensors that are due, writes last
minute's values, checks every rule and sends what has to go out. Readings that arrive on their own (pushes,
the machine's own feed, Incoming data, typed in) go through `accept`, which checks that sensor's rules at once."""
import logging
import re
import threading
from datetime import timedelta

from django.db import close_old_connections, transaction
from django.utils import timezone

from . import kinds
from .models import Sensor, SensorAlarm, SensorRule, SensorValue

log = logging.getLogger("plant")
KEEP = 15 * 60          # seconds of readings kept on the sensor for averaging, rates and the minute's min / max
_last_minute = {"t": None}
_busy = threading.Lock()


# ------------------------------------------------------------------ scaling
def scaled(s, raw):
    """The raw reading in real units (s: a Sensor). On / off sensors give 1.0 or 0.0."""
    if raw is None:
        return None
    if s.dtype == "onoff":
        on = raw >= 0.5 if not s.scale else raw >= (s.raw_lo + s.raw_hi) / 2
        return float(on != s.invert)
    v = raw
    if s.scale and s.raw_hi != s.raw_lo:
        v = s.eng_lo + (raw - s.raw_lo) * (s.eng_hi - s.eng_lo) / (s.raw_hi - s.raw_lo)
        if s.clamp:
            lo, hi = sorted((s.eng_lo, s.eng_hi))
            v = min(max(v, lo), hi)
    return v + (s.offset or 0)


def is_fault(s, raw):
    return raw is not None and ((s.fault_lo is not None and raw < s.fault_lo) or (s.fault_hi is not None and raw > s.fault_hi))


def accept(s, raw_in, t=None, save=True, check=True):
    """A new reading for sensor s. Returns (ok, message)."""
    t = t or timezone.now()
    raw = kinds.number(raw_in)
    s.read_t = t
    if raw is None:
        s.ok, s.msg = False, f"Not a number: {str(raw_in)[:40]}"
        if save:
            s.save(update_fields=["read_t", "ok", "msg", "state"])
        return False, s.msg
    s.fault = is_fault(s, raw)
    v = scaled(s, raw)
    now_s = t.timestamp()
    recent = [r for r in (s.recent or []) if now_s - r[0] <= KEEP][-400:]
    n = max(1, min(60, s.smooth or 1))
    if n > 1 and s.dtype == "number":
        last = [r[2] for r in recent[-(n - 1):]] + [raw]
        v = scaled(s, sum(last) / len(last))
    recent.append([round(now_s, 1), v, raw])
    st = dict(s.state or {})
    if s.value is None or v != s.value:
        st["changed"] = now_s
    st.setdefault("changed", now_s)
    s.raw, s.value, s.value_t, s.recent, s.state = raw, v, t, recent, st
    s.ok, s.msg = True, ("Faulty reading (outside the fault limits)" if s.fault else "")
    if save:
        s.save(update_fields=["raw", "value", "value_t", "read_t", "recent", "state", "ok", "msg", "fault"])
    if check and save:
        check_rules(s, t)
    return True, s.msg


def failed(s, msg, t):
    s.read_t, s.ok, s.msg = t, False, str(msg)[:300]
    s.save(update_fields=["read_t", "ok", "msg", "state"])


def test_read(s, t=None):
    """Read once without saving: {"ok", "raw", "value", "text", "error"}."""
    t = t or timezone.now()
    if s.kind not in kinds.SERVER_READS:
        return {"ok": False, "error": "This kind of sensor sends its own readings: send one and it shows here."}
    try:
        raw_in = kinds.read(s, t, values=current_values())
    except kinds.ReadError as e:
        return {"ok": False, "error": str(e)}
    raw = kinds.number(raw_in)
    if raw is None:
        return {"ok": False, "error": f"Got {str(raw_in)[:60]!r}, which isn't a number."}
    v = scaled(s, raw)
    return {"ok": True, "raw": raw, "value": v, "text": s.fmt(v), "fault": is_fault(s, raw)}


def current_values():
    return {x.name: x.value for x in Sensor.objects.filter(enabled=True).only("name", "value")}


# ------------------------------------------------------------------ reading on a timer
BUDGET = 4.0   # seconds of reading per ticker pass; sensors still due wait for the next pass (machines keep counting on time)


def poll(t):
    """Read every sensor the server reads that is due, longest-waiting first (calculated ones last, so they use
    this pass's values). Devices that don't answer cost at most their timeout, and the pass stops after BUDGET."""
    import time
    due = [s for s in Sensor.objects.filter(enabled=True, kind__in=kinds.SERVER_READS)
           if not s.read_t or (t - s.read_t).total_seconds() >= max(1, s.every or 10) - 0.5]
    due.sort(key=lambda s: (s.kind == "calc", s.read_t.timestamp() if s.read_t else 0))
    started = time.monotonic()
    for s in due:
        if time.monotonic() - started > BUDGET and s.kind != "calc":
            continue
        values = current_values() if s.kind == "calc" else None
        try:
            raw = kinds.read(s, t, values)
        except kinds.ReadError as e:
            failed(s, e, t)
            continue
        except Exception as e:  # noqa: BLE001 - one sensor never stops the others
            log.warning("sensor %s: %s", s.name, e)
            failed(s, str(e) or type(e).__name__, t)
            continue
        accept(s, raw, t)


def from_feed(machine, sample, t):
    """The machine's own reading came in (each minute): its extra fields (Sensor kind "feed")."""
    vals = (sample or {}).get("sensors") or {}
    for s in Sensor.objects.filter(enabled=True, kind="feed", machine=machine.name):
        f = (s.config or {}).get("field", "")
        if sample is None:
            failed(s, "No reading from the machine this minute", t)
        elif vals.get(f) is None:
            failed(s, f'No "{f}" field in the machine\'s reading', t)
        else:
            accept(s, vals[f], t)


def feed_fields(machine):
    return [f for f in Sensor.objects.filter(enabled=True, kind="feed", machine=machine.name).values_list("config__field", flat=True) if f]


def from_source(src, items):
    """An Incoming data source delivered records (Sensor kind "source")."""
    from hmi import inbound
    sens = [s for s in Sensor.objects.filter(enabled=True, kind="source") if (s.config or {}).get("source", "").lower() == src.name.lower()]
    if not sens:
        return
    t = timezone.now()
    for s in sens:
        c = s.config or {}
        val = None
        for item in reversed(items if isinstance(items, list) else [items]):
            if not isinstance(item, dict):
                continue
            if c.get("line"):
                ln = inbound.get_path(item, (src.mapping or {}).get("line") or "line") if not src.line else src.line
                if str(ln or "").lower() != c["line"].lower():
                    continue
            val = inbound.get_path(item, c.get("field", ""))
            if val is not None:
                break
        if val is not None:
            accept(s, val, t)


def push(payload, t=None):
    """POST /api/sensors: {"name": value, …}, {"sensor": name, "value": v} or a list of those.
    Returns {"stored": n, "unknown": [names], "errors": [..]}."""
    t = t or timezone.now()
    pairs = []
    items = payload if isinstance(payload, list) else [payload]
    for it in items[:500]:
        if not isinstance(it, dict):
            continue
        if "sensor" in it and "value" in it:
            pairs.append((str(it["sensor"]), it["value"]))
        else:
            pairs += [(str(k), v) for k, v in it.items()]
    byname = {s.name.lower(): s for s in Sensor.objects.filter(enabled=True, kind="push")}
    stored, unknown, errors = 0, [], []
    for name, v in pairs:
        s = byname.get(name.lower())
        if not s:
            unknown.append(name[:40])
            continue
        ok, msg = accept(s, v, t)
        stored += ok
        if not ok:
            errors.append(f"{name}: {msg}")
    return {"stored": stored, "unknown": unknown[:20], "errors": errors[:20]}


# ------------------------------------------------------------------ minute values for Grafana
def write_minute(t):
    """Once per minute: last minute's average, min and max of every sensor (sensor_values)."""
    m = t.replace(second=0, microsecond=0) - timedelta(minutes=1)
    if _last_minute["t"] == m:
        return
    _last_minute["t"] = m
    a, b = m.timestamp(), m.timestamp() + 60
    active = set(SensorAlarm.objects.filter(end__isnull=True).values_list("sensor", flat=True))
    for s in Sensor.objects.filter(enabled=True):
        rows = [r for r in (s.recent or []) if a <= r[0] < b and r[1] is not None]
        if not rows:
            continue
        vs = [r[1] for r in rows]
        SensorValue.objects.get_or_create(sensor=s.name, t=m, defaults={
            "machine": s.machine, "value": sum(vs) / len(vs), "vmin": min(vs), "vmax": max(vs), "raw": rows[-1][2], "n": len(vs),
            "alarm": s.name in active})


# ------------------------------------------------------------------ alarm rules
def machine_state(machine):
    """(a shift is running, the machine is running) for the rule's 'only while' setting."""
    from hmi.models import ShiftRun
    if not machine:
        return False, False
    sh = ShiftRun.objects.filter(active=True, line=machine).only("state").first()
    return bool(sh), bool(sh and sh.state == "running")


def _rate(s, t):
    """Change per minute over (about) the last minute."""
    now_s = t.timestamp()
    rows = [r for r in (s.recent or []) if r[1] is not None and now_s - r[0] <= 75]
    if len(rows) < 2 or rows[-1][0] - rows[0][0] < 5:
        return None
    return (rows[-1][1] - rows[0][1]) / ((rows[-1][0] - rows[0][0]) / 60)


def condition(r, s, t, clearing=False):
    """True / False: does the rule's condition hold now? While clearing, the deadband widens the alarm side."""
    v, a, b = s.value, r.a, r.b
    db = abs(r.deadband or 0) if clearing else 0
    if r.op == "stale":
        return s.value_t is None or (t - s.value_t).total_seconds() >= (a or 0) * 60
    if r.op == "fault":
        return bool(s.fault)
    if r.op == "stuck":
        ch = (s.state or {}).get("changed")
        return s.value is not None and ch is not None and t.timestamp() - ch >= (a or 0) * 60
    if v is None or s.value_t is None:
        return False
    if r.op in ("rise", "fall"):
        rate = _rate(s, t)
        if rate is None or a is None:
            return False
        return rate > a - db if r.op == "rise" else rate < -(a - db)
    if r.op == "on":
        return v >= 0.5
    if r.op == "off":
        return v < 0.5
    if a is None:
        return False
    if r.op == "lt":
        return v < a + db
    if r.op == "le":
        return v <= a + db
    if r.op == "gt":
        return v > a - db
    if r.op == "ge":
        return v >= a - db
    if r.op == "eq":
        return abs(v - a) <= max(db, 1e-9)
    if r.op == "ne":
        return abs(v - a) > max(db, 1e-9) if not clearing else abs(v - a) > 1e-9
    if b is None:
        return False
    lo, hi = sorted((a, b))
    if r.op == "in":
        return lo - db <= v <= hi + db
    if r.op == "out":
        return v < lo + db or v > hi - db
    return False


def describe(r, unit=""):
    """The rule in words: 'falls below 50 mm for 10 s'."""
    u = f" {unit}" if unit else ""
    a = "" if r.a is None else f"{r.a:g}"
    b = "" if r.b is None else f"{r.b:g}"
    op = dict(SensorRule.OPS)[r.op]
    if r.op in ("in", "out"):
        txt = f"{op} {a} and {b}{u}"
    elif r.op in ("on", "off", "fault"):
        txt = op
    elif r.op in ("stuck", "stale"):
        txt = f"{op.split(' (')[0]} {a} min"
    elif r.op in ("rise", "fall"):
        txt = f"{op.split(' (')[0]} {a}{u} per minute"
    else:
        txt = f"{op} {a}{u}"
    return txt + (f" for {r.delay} s" if r.delay else "")


class _Fmt(dict):
    def __missing__(self, k):
        return "{" + k + "}"


def alarm_text(r, s):
    v = s.fmt() if s.value is not None else "no reading"
    d = max(0, min(6, s.decimals))
    lim = lambda x: "" if x is None else f"{x:,.{d}f}".rstrip("0").rstrip(".") if d else f"{x:,.0f}"  # noqa: E731
    vals = _Fmt(value=v, limit=lim(r.a), limit2=lim(r.b), unit=s.unit, sensor=s.label, machine=s.machine or "Plant",
                raw="" if s.raw is None else f"{s.raw:g}{(' ' + s.raw_unit) if s.raw_unit else ''}", name=r.name or s.label)
    if r.message:
        try:
            return r.message.format_map(vals)[:300]
        except (ValueError, IndexError):
            return r.message[:300]
    head = f"{s.machine}: " if s.machine else ""
    return f"{head}{r.name or s.label} — {s.label} {describe(r, s.unit)} (now {v})"[:300]


def open_alarm(r, s, t):
    al = SensorAlarm.objects.create(rule=r, sensor=s.name, label=r.name or s.label, machine=s.machine, severity=r.severity,
                                    text=alarm_text(r, s), start=t, value=s.value, worst=s.value, latch=r.latch, all_screens=r.show_all,
                                    show_kiosk=r.show_kiosk)
    r.alarm, r.pending_since, r.clearing_since = al, None, None
    r.save(update_fields=["alarm", "pending_since", "clearing_since"])
    if r.ticket:
        from hmi.models import Ticket
        tk = Ticket.objects.create(t=t, line=s.machine, by="Sensor alarm", machine=s.machine or "Plant", type=(r.ticket_type or "Sensor alarm")[:80],
                                   priority=r.ticket_priority if r.ticket_priority in ("High", "Medium", "Low") else "Medium",
                                   notes=f"{al.text}\nOpened by the alarm rule \"{r}\" on {s.label} ({s.name}).", updated=t, updated_by="Sensor alarm")
        al.ticket_id = tk.pk
        al.save(update_fields=["ticket_id"])
    if r.webhook:
        from hmi.engine import event
        event("sensor_alarm", {"id": al.pk, "sensor": s.name, "machine": s.machine, "severity": r.severity, "text": al.text, "value": s.value,
                               "start": t.isoformat()})
    if r.show_kiosk:
        from plant.models import PlantSettings
        PlantSettings.bump()
    return al


def close_alarm(r, t):
    al = r.alarm
    if al and al.end is None:
        al.end = t
        al.save(update_fields=["end"])
        if r.webhook:
            from hmi.engine import event
            event("sensor_alarm_clear", {"id": al.pk, "sensor": al.sensor, "machine": al.machine, "end": t.isoformat()})
        if r.notify and r.notify_clear and al.sent:
            _send(r, al, cleared=True)
        from plant.models import PlantSettings
        PlantSettings.bump()
    r.alarm, r.pending_since, r.clearing_since = None, None, None
    r.save(update_fields=["alarm", "pending_since", "clearing_since"])


def check_rule(r, s, t, state=None):
    shift_on, running = state if state is not None else machine_state(s.machine)
    gate = {"always": True, "running": running, "shift": shift_on, "stopped": shift_on and not running, "noshift": not shift_on}.get(r.when, True)
    al = r.alarm if r.alarm_id else None
    if al and al.end is not None:   # closed elsewhere
        al = None
    if al is None:
        if gate and condition(r, s, t):
            if r.pending_since is None:
                r.pending_since = t
                r.save(update_fields=["pending_since"])
            if (t - r.pending_since).total_seconds() >= max(0, r.delay):
                open_alarm(r, s, t)
        elif r.pending_since is not None:
            r.pending_since = None
            r.save(update_fields=["pending_since"])
        return
    if gate and condition(r, s, t, clearing=True):
        if s.value is not None and al.worst is not None:
            worse = s.value < al.worst if r.op in ("lt", "le", "fall") else s.value > al.worst if r.op in ("gt", "ge", "rise") else False
            if worse:
                al.worst = s.value
                al.save(update_fields=["worst"])
        if r.clearing_since is not None:
            r.clearing_since = None
            r.save(update_fields=["clearing_since"])
        return
    if r.clearing_since is None:
        r.clearing_since = t
        r.save(update_fields=["clearing_since"])
    if (t - r.clearing_since).total_seconds() >= max(0, r.clear_delay) or not gate:
        close_alarm(r, t)


def check_rules(s, t):
    state = machine_state(s.machine)
    for r in s.rules.filter(enabled=True).select_related("alarm"):
        try:
            with transaction.atomic():
                check_rule(r, s, t, state)
        except Exception:  # noqa: BLE001 - a broken rule never stops the others
            log.exception("sensor rule %s", r.pk)


def check_all(t):
    """Rules that depend on time passing (delays, no new reading, stuck) are checked every pass too."""
    for s in Sensor.objects.filter(enabled=True).prefetch_related("rules"):
        if any(r.enabled for r in s.rules.all()):
            check_rules(s, t)
    # rules switched off or removed: their alarms end
    for al in SensorAlarm.objects.filter(end__isnull=True).select_related("rule", "rule__sensor"):
        if not al.rule or not al.rule.enabled or not al.rule.sensor.enabled:
            al.end = t
            al.save(update_fields=["end"])


# ------------------------------------------------------------------ messages
def recipients(r, machine):
    from hmi.models import AlertContact
    qs = AlertContact.objects.filter(active=True)
    ids = [int(x) for x in (r.contacts or []) if str(x).isdigit()]
    if ids:
        return list(qs.filter(pk__in=ids))
    return [c for c in qs if not c.lines or not machine or machine in c.lines]


def _send(r, al, cleared=False):
    from hmi import alerts
    people = recipients(r, al.machine)
    if not people:
        return
    where = al.machine or "the plant"
    if cleared:
        subject, body = f"Cleared: {al.label} on {where}", f"{al.text} — cleared at {timezone.localtime(al.end):%I:%M %p}."
    else:
        sev = {"bad": "ALARM", "warn": "Warning", "info": "Notice"}.get(al.severity, "Alarm")
        subject = f"{sev}: {al.label} on {where}"
        body = f"{al.text} Since {timezone.localtime(al.start):%I:%M %p}."
    jobs = [(c.name, c.email, c.phone, subject, body, "sensor", al.machine, f"sensor|{al.pk}|{'clear' if cleared else al.sent}") for c in people]

    def run():
        try:
            for j in jobs:
                alerts.deliver(*j)
        finally:
            close_old_connections()
    threading.Thread(target=run, name="sensor-alerts", daemon=True).start()


def notify_due(t):
    for al in SensorAlarm.objects.filter(end__isnull=True, rule__notify=True).select_related("rule"):
        r = al.rule
        age = (t - al.start).total_seconds() / 60
        if age < (r.notify_after or 0):
            continue
        if al.sent and not (r.repeat and al.sent_t and (t - al.sent_t).total_seconds() >= r.repeat * 60):
            continue
        al.sent += 1
        al.sent_t = t
        al.save(update_fields=["sent", "sent_t"])
        _send(r, al)


def ack(alarm_id, who, t=None):
    t = t or timezone.now()
    al = SensorAlarm.objects.filter(pk=alarm_id).first()
    if not al:
        return {"ok": False, "error": "That alarm isn't on file."}
    if al.acked_t:
        return {"ok": True, "msg": f"Already acknowledged by {al.acked_by}."}
    al.acked_by, al.acked_t = (who or "Someone")[:120], t
    al.save(update_fields=["acked_by", "acked_t"])
    from plant.models import PlantSettings
    PlantSettings.bump()
    return {"ok": True, "msg": "Acknowledged."}


def tick(t=None):
    """The ticker's call, every pass."""
    t = t or timezone.now()
    if not _busy.acquire(blocking=False):
        return
    try:
        poll(t)
        check_all(t)
        notify_due(t)
        write_minute(t)
    finally:
        _busy.release()


def slug(v):
    return re.sub(r"[^a-z0-9_]+", "_", str(v or "").strip().lower()).strip("_")[:40]


def kiosk_alarms(station):
    """Sensor alarms for a machine's kiosk: its own, plant-wide ones and those set to show everywhere."""
    from django.db.models import Q
    qs = SensorAlarm.objects.filter(show_kiosk=True, acked_t__isnull=True).filter(Q(end__isnull=True) | Q(latch=True))
    qs = qs.filter(Q(machine=station or "") | Q(machine="") | Q(all_screens=True)) if station else qs
    return list(qs.order_by("-start")[:30])


def kiosk_values(station):
    return [s for s in Sensor.objects.filter(enabled=True, show_kiosk=True, machine=station)] if station else []
