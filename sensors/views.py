"""Sensors & alarms page (/sensors/): every sensor with its live value, adding and changing sensors and their
alarm rules, the alarm log, and the list of connection types. Also POST /api/sensors for pushed readings.

Open with "Sensors & custom alarms" (Admin › Permissions). Reading and the rules are in sensors/engine.py,
the connection types in sensors/kinds.py."""
import json
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.db.models import Q
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_http_methods, require_POST

from hmi.models import AlertContact
from hmi.views import current_person, touch
from plant import perms
from plant.models import Machine

from . import engine, kinds
from .models import Sensor, SensorAlarm, SensorRule

PERM = "sensors.edit"
TABS = [("sensors", "Sensors"), ("log", "Alarm log"), ("types", "Connection types")]
SENSOR_NUMS = ("raw_lo", "raw_hi", "eng_lo", "eng_hi", "offset")
SENSOR_OPT = ("fault_lo", "fault_hi")
RULE_FLAGS = ("latch", "show_kiosk", "show_all", "notify", "notify_clear", "ticket", "webhook", "enabled")


def _gate(request):
    me, note = current_person(request)
    if perms.can(me, PERM):
        touch(request)
        return me, None
    msg = note or (f"{me.name} doesn't have permission for Sensors & alarms (Admin › Permissions › Sensors & custom alarms)." if me else None)
    return None, render(request, "supervisor/login.html", {"mode": "admin", "note": msg, "perm": PERM, "title": "Sensors & alarms",
                                                           "blurb": "Sensors from PLCs, I/O modules, web addresses, databases and more, scaled to "
                                                                    "real units, with your own alarm rules for each machine."})


def _f(v, default=None):
    try:
        x = float(str(v).replace(",", "").strip())
        return x if x == x and abs(x) != float("inf") else default
    except (TypeError, ValueError):
        return default


def _i(v, default=0, lo=0, hi=10 ** 6):
    x = _f(v)
    return default if x is None else int(max(lo, min(hi, x)))


def machines():
    return list(Machine.objects.filter(active=True).order_by("sort", "name").values_list("name", flat=True))


def sensor_form(s=None, p=None):
    """The values the sensor form shows: from the sensor, or what was typed (after a refused save)."""
    if p is not None:
        f = {k: p.get(k, "") for k in ("name", "label", "machine", "dtype", "kind", "every", "raw_unit", "unit", "decimals", "smooth", "on_label",
                                       "off_label", *SENSOR_NUMS, *SENSOR_OPT)}
        f.update({k: p.get(k) == "1" for k in ("scale", "clamp", "invert", "show_kiosk", "enabled")})
        f["unit"] = _unit(p)
        f["cfg"] = {k[0]: kinds.clean_config(k[0], p) for k in kinds.KINDS}
        f["id"] = p.get("id", "")
        return f
    if s is None:
        return {"id": "", "name": "", "label": "", "machine": "", "dtype": "number", "kind": "modbus", "every": 10, "raw_unit": "V", "unit": "mm",
                "scale": True, "raw_lo": 0, "raw_hi": 5, "eng_lo": 0, "eng_hi": 500, "clamp": True, "offset": 0, "decimals": 1, "smooth": 1,
                "fault_lo": "", "fault_hi": "", "on_label": "On", "off_label": "Off", "invert": False, "show_kiosk": True, "enabled": True,
                "cfg": {"modbus": {"port": "502", "unit": "1", "table": "holding", "style": "zero", "format": "uint16", "words": "big"},
                        "db": {"db": "machines", "maxAge": "3"}}}
    f = {k: getattr(s, k) for k in ("name", "label", "machine", "dtype", "kind", "every", "raw_unit", "unit", "decimals", "smooth", "on_label",
                                    "off_label", "scale", "clamp", "invert", "show_kiosk", "enabled", *SENSOR_NUMS)}
    f.update({k: "" if getattr(s, k) is None else getattr(s, k) for k in SENSOR_OPT})
    f["cfg"] = {s.kind: dict(s.config or {})}
    f["id"] = s.pk
    return f


def save_sensor(p, me):
    """Returns (sensor, error)."""
    s = Sensor.objects.filter(pk=_i(p.get("id"), 0)).first() if str(p.get("id") or "").isdigit() else None
    name = engine.slug(p.get("name") or p.get("label"))
    label = str(p.get("label") or "").strip()[:80]
    kind = p.get("kind") if p.get("kind") in kinds.KIND else ""
    if not label:
        return None, "Give the sensor a name people will recognise, e.g. Media roll diameter."
    if not name or not name[0].isalpha():
        return None, "The short id must start with a letter (letters, numbers and _ only), e.g. barb_roll."
    if Sensor.objects.filter(name=name).exclude(pk=s.pk if s else None).exists():
        return None, f'Another sensor already has the short id "{name}".'
    if not kind:
        return None, "Pick how the sensor is connected."
    machine = str(p.get("machine") or "")
    if machine and machine not in machines():
        return None, f"{machine} isn't an active machine."
    cfg = kinds.clean_config(kind, p)
    err = kinds.check_config(kind, cfg, name)
    if err:
        return None, err
    if kind == "feed" and not machine:
        return None, "An extra field of the machine's reading needs the machine picked."
    if kind == "suite" and not machine:
        return None, "Pick the machine the value is for."
    if kind == "calc" and s:
        others = {x.name: x for x in Sensor.objects.exclude(pk=s.pk)}
        missing = [n for n in kinds.formula_names(cfg.get("formula")) if n not in others]
        if missing:
            return None, f"The formula uses {', '.join(missing)}, which aren't sensors."
    elif kind == "calc":
        missing = [n for n in kinds.formula_names(cfg.get("formula")) if not Sensor.objects.filter(name=n).exists()]
        if missing:
            return None, f"The formula uses {', '.join(missing)}, which aren't sensors."
    vals = {k: _f(p.get(k), 0) for k in SENSOR_NUMS}
    scale = p.get("scale") == "1"
    if scale and vals["raw_lo"] == vals["raw_hi"]:
        return None, "The two raw points must be different (e.g. 0 V and 5 V)."
    if scale and vals["eng_lo"] == vals["eng_hi"]:
        return None, "The two scaled points must be different (e.g. 0 mm and 500 mm)."
    s = s or Sensor()
    renamed = s.pk and s.name != name
    old = s.name
    s.name, s.label, s.machine, s.kind, s.config = name, label, machine, kind, cfg
    s.every = _i(p.get("every"), 10, 1, 3600)
    s.dtype = p.get("dtype") if p.get("dtype") in dict(Sensor.TYPES) else "number"
    s.raw_unit, s.unit = str(p.get("raw_unit") or "")[:12], _unit(p)
    s.scale = scale
    for k, v in vals.items():
        setattr(s, k, v)
    for k in SENSOR_OPT:
        setattr(s, k, _f(p.get(k)))
    s.clamp, s.invert = p.get("clamp") == "1", p.get("invert") == "1"
    s.show_kiosk, s.enabled = p.get("show_kiosk") == "1", p.get("enabled") == "1"
    s.decimals, s.smooth = _i(p.get("decimals"), 1, 0, 6), _i(p.get("smooth"), 1, 1, 60)
    s.on_label, s.off_label = (str(p.get("on_label") or "On")[:20], str(p.get("off_label") or "Off")[:20])
    s.updated_by = me.name
    if renamed:
        s.recent, s.value, s.raw = [], None, None
    s.save()
    if renamed:   # keep formulas and history pointing at it
        for x in Sensor.objects.filter(kind="calc"):
            f = (x.config or {}).get("formula", "")
            import re
            nf = re.sub(rf"\b{re.escape(old)}\b", name, f)
            if nf != f:
                x.config = {**x.config, "formula": nf}
                x.save(update_fields=["config"])
    return s, None


def rule_form(r=None, p=None):
    if p is not None:
        f = {k: p.get(k, "") for k in ("name", "op", "a", "b", "delay", "deadband", "clear_delay", "when", "severity", "message", "notify_after",
                                       "repeat", "ticket_type", "ticket_priority")}
        f.update({k: p.get(k) == "1" for k in RULE_FLAGS})
        f["contacts"] = [int(x) for x in p.getlist("contacts") if str(x).isdigit()] if hasattr(p, "getlist") else []
        f["id"] = p.get("rule", "")
        return f
    if r is None:
        return {"id": "", "name": "", "op": "lt", "a": "", "b": "", "delay": 5, "deadband": 0, "clear_delay": 0, "when": "always", "severity": "warn",
                "message": "", "notify_after": 0, "repeat": 0, "ticket_type": "", "ticket_priority": "Medium", "contacts": [],
                "latch": False, "show_kiosk": True, "show_all": False, "notify": False, "notify_clear": False, "ticket": False, "webhook": True, "enabled": True}
    f = {k: getattr(r, k) for k in ("name", "op", "delay", "deadband", "clear_delay", "when", "severity", "message", "notify_after", "repeat",
                                    "ticket_type", "ticket_priority", "contacts", *RULE_FLAGS)}
    f.update(a="" if r.a is None else r.a, b="" if r.b is None else r.b, id=r.pk)
    return f


NEED_A = {"lt", "le", "gt", "ge", "eq", "ne", "in", "out", "rise", "fall", "stuck", "stale"}


def save_rule(s, p):
    r = SensorRule.objects.filter(pk=_i(p.get("rule"), 0), sensor=s).first() if str(p.get("rule") or "").isdigit() else None
    op = p.get("op") if p.get("op") in dict(SensorRule.OPS) else ""
    if not op:
        return None, "Pick the condition."
    a, b = _f(p.get("a")), _f(p.get("b"))
    if op in NEED_A and a is None:
        return None, "Enter the limit." if op not in ("stuck", "stale") else "Enter the minutes."
    if op in ("in", "out") and b is None:
        return None, "Enter both limits of the range."
    if op in ("rise", "fall", "stuck", "stale") and a is not None and a <= 0:
        return None, "Enter a number above 0."
    if op in ("on", "off") and s.dtype != "onoff":
        return None, "Turns on / off is for on / off sensors. For a number, use rises above or falls below."
    if op == "fault" and s.fault_lo is None and s.fault_hi is None:
        return None, "Set the sensor's fault limits first (in the sensor's settings)."
    msg = str(p.get("message") or "").strip()[:300]
    if msg:
        try:
            msg.format_map(engine._Fmt())
        except (ValueError, IndexError):
            return None, "The alarm text has a { or } that isn't part of a {value}-style field."
    notify = p.get("notify") == "1"
    ids = [int(x) for x in p.getlist("contacts") if str(x).isdigit()]
    if notify and not AlertContact.objects.filter(active=True).exists():
        return None, "No one is set up to get messages yet: add people in Admin › Alerts first."
    r = r or SensorRule(sensor=s)
    r.name, r.op, r.a, r.b = str(p.get("name") or "").strip()[:80], op, a, b
    r.delay, r.clear_delay = _i(p.get("delay"), 0, 0, 86400), _i(p.get("clear_delay"), 0, 0, 86400)
    r.deadband = abs(_f(p.get("deadband"), 0))
    r.when = p.get("when") if p.get("when") in dict(SensorRule.WHEN) else "always"
    r.severity = p.get("severity") if p.get("severity") in dict(SensorRule.SEV) else "warn"
    r.message = msg
    for k in RULE_FLAGS:
        setattr(r, k, p.get(k) == "1")
    r.contacts = ids
    r.notify_after, r.repeat = _i(p.get("notify_after"), 0, 0, 10080), _i(p.get("repeat"), 0, 0, 10080)
    r.ticket_type = str(p.get("ticket_type") or "").strip()[:80]
    r.ticket_priority = p.get("ticket_priority") if p.get("ticket_priority") in ("High", "Medium", "Low") else "Medium"
    r.pending_since = r.clearing_since = None
    r.save()
    return r, None


def _unit(p):
    vals = [str(v).strip() for v in (p.getlist("unit") if hasattr(p, "getlist") else [p.get("unit", "")]) if str(v).strip()]
    return vals[0][:12] if vals else ""


def _back(params):
    return HttpResponseRedirect("/sensors/?" + urlencode({k: v for k, v in params.items() if v not in (None, "")}))


@ensure_csrf_cookie
@require_http_methods(["GET", "POST"])
def page(request):
    me, denied = _gate(request)
    if denied:
        return denied
    q = request.GET
    tab = q.get("tab") if q.get("tab") in dict(TABS) else "sensors"
    now = timezone.now()
    ctx = {"mode": "admin", "me": me, "tab": tab, "tabs": TABS, "now": now, "msg": q.get("msg", ""), "err": q.get("err", ""),
           "machines": machines(), "kinds": kinds.all_kinds(), "presets": kinds.PRESETS, "gateway": kinds.GATEWAY,
           "ops": SensorRule.OPS, "whens": SensorRule.WHEN, "sevs": SensorRule.SEV, "types": Sensor.TYPES,
           "contacts": list(AlertContact.objects.filter(active=True)), "feedKey": bool(settings.SUITE.get("FEED_KEY")),
           "origin": request.build_absolute_uri("/").rstrip("/")}
    edit, rule_edit, test = None, None, None
    if request.method == "POST":
        p = request.POST
        a = p.get("action")
        if a in ("save", "test"):
            if a == "test":
                tmp, err = _preview_sensor(p)
                test = engine.test_read(tmp) if tmp else {"ok": False, "error": err}
                edit = sensor_form(p=p)
            else:
                s, err = save_sensor(p, me)
                if s:
                    return _back({"tab": "sensors", "msg": f"Saved {s.label}." + (" Add its alarm rules below." if not s.rules.exists() else ""),
                                  "rules": s.pk})
                edit = sensor_form(p=p)
                ctx["err"] = err
        elif a == "rule":
            s = Sensor.objects.filter(pk=_i(p.get("id"))).first()
            if not s:
                return _back({"err": "That sensor isn't on file."})
            r, err = save_rule(s, p)
            if r:
                return _back({"rules": s.pk, "msg": f"Saved the alarm rule: {s.label} {engine.describe(r, s.unit)}."})
            ctx["err"] = err
            rule_edit = {"sensor": s, "f": rule_form(p=p)}
        else:
            return _back({"err": "Unknown action."})
    if tab == "sensors":
        sens = list(Sensor.objects.prefetch_related("rules"))
        active = {}
        for al in SensorAlarm.objects.filter(end__isnull=True):
            active.setdefault(al.sensor, []).append(al)
        for s in sens:
            s.active_alarms = active.get(s.name, [])
            s.kind_label = kinds.KIND.get(s.kind, ("", s.kind))[1]
            s.age = (now - s.value_t).total_seconds() if s.value_t else None
            for r in s.rules.all():
                r.words = engine.describe(r, s.unit)
        ctx["sensors"] = sens
        ctx["counts"] = {"sensors": len(sens), "alarms": sum(len(v) for v in active.values()), "problems": sum(1 for s in sens if s.enabled and not s.ok)}
        sel = _i(q.get("rules"), 0)
        ctx["sel"] = next((s for s in sens if s.pk == sel), None)
        if ctx["sel"] and not rule_edit:
            rid = q.get("rule")
            if rid == "new":
                rule_edit = {"sensor": ctx["sel"], "f": rule_form()}
            elif str(rid or "").isdigit():
                r = SensorRule.objects.filter(pk=int(rid), sensor=ctx["sel"]).first()
                if r:
                    rule_edit = {"sensor": ctx["sel"], "f": rule_form(r)}
        if rule_edit:
            ctx["sel"] = next((s for s in sens if s.pk == rule_edit["sensor"].pk), rule_edit["sensor"])
        if edit is None:
            if q.get("edit") == "new":
                edit = sensor_form()
                if q.get("machine") in ctx["machines"]:
                    edit["machine"] = q["machine"]
                if q.get("kind") in kinds.KIND:
                    edit["kind"] = q["kind"]
            elif str(q.get("edit") or "").isdigit():
                s = Sensor.objects.filter(pk=int(q["edit"])).first()
                edit = sensor_form(s) if s else None
    elif tab == "log":
        qs = SensorAlarm.objects.all()
        fm, fs = str(q.get("machine") or "")[:60], str(q.get("sensor") or "")[:40]
        if fm:
            qs = qs.filter(machine=fm)
        if fs:
            qs = qs.filter(sensor=fs)
        days = _i(q.get("days"), 7, 1, 365)
        ctx.update(log=list(qs.filter(Q(start__gte=now - timedelta(days=days)) | Q(end__isnull=True))[:500]), fmachine=fm, fsensor=fs, days=days,
                   sensorNames=list(Sensor.objects.values_list("name", "label")))
    ctx.update(edit=edit, ruleEdit=rule_edit, test=test, here=request.get_full_path())
    if edit:
        ctx["editKind"] = edit.get("kind")
    return render(request, "sensors/page.html", ctx)


def _preview_sensor(p):
    """An unsaved sensor from the form, to test the connection and the scaling."""
    kind = p.get("kind") if p.get("kind") in kinds.KIND else ""
    if not kind:
        return None, "Pick how the sensor is connected."
    cfg = kinds.clean_config(kind, p)
    err = kinds.check_config(kind, cfg, engine.slug(p.get("name")))
    if err:
        return None, err
    s = Sensor(name=engine.slug(p.get("name")) or "test", label=p.get("label") or "Test", machine=p.get("machine") or "", kind=kind, config=cfg,
               dtype=p.get("dtype") if p.get("dtype") in dict(Sensor.TYPES) else "number", unit=_unit(p),
               raw_unit=str(p.get("raw_unit") or "")[:12], scale=p.get("scale") == "1", clamp=p.get("clamp") == "1", invert=p.get("invert") == "1",
               decimals=_i(p.get("decimals"), 1, 0, 6), on_label=p.get("on_label") or "On", off_label=p.get("off_label") or "Off",
               fault_lo=_f(p.get("fault_lo")), fault_hi=_f(p.get("fault_hi")), **{k: _f(p.get(k), 0) for k in SENSOR_NUMS})
    return s, None


@require_POST
def do(request, name):
    """Buttons: turn a sensor or rule on / off, remove them, type a reading in, acknowledge an alarm."""
    me, denied = _gate(request)
    if denied:
        return denied
    p = request.POST
    back = {"tab": p.get("tab") or "sensors", "rules": p.get("rules") or ""}
    s = Sensor.objects.filter(pk=_i(p.get("id"))).first()
    if name == "sensorToggle" and s:
        s.enabled = not s.enabled
        s.save(update_fields=["enabled"])
        return _back({**back, "msg": f"{s.label} turned {'on' if s.enabled else 'off'}."})
    if name == "sensorDelete" and s:
        users = [x.label for x in Sensor.objects.filter(kind="calc").exclude(pk=s.pk) if s.name in kinds.formula_names((x.config or {}).get("formula"))]
        if users:
            return _back({**back, "err": f"{', '.join(users)} calculate{'s' if len(users) == 1 else ''} from {s.label}: change that first."})
        label = s.label
        SensorAlarm.objects.filter(sensor=s.name, end__isnull=True).update(end=timezone.now())
        s.delete()
        return _back({"msg": f"Removed {label}. Its alarm log and minute values are kept."})
    if name == "manual" and s:
        if s.kind != "manual":
            return _back({**back, "err": "Only typed-in sensors take a reading here."})
        ok, msg = engine.accept(s, p.get("value"))
        return _back({**back, "msg": f"{s.label}: {s.fmt()}." if ok else "", "err": "" if ok else msg})
    r = SensorRule.objects.filter(pk=_i(p.get("rule"))).select_related("sensor").first()
    if name == "ruleToggle" and r:
        r.enabled = not r.enabled
        r.save(update_fields=["enabled"])
        if not r.enabled and r.alarm_id:
            engine.close_alarm(r, timezone.now())
        return _back({"rules": r.sensor_id, "msg": f"Rule turned {'on' if r.enabled else 'off'}."})
    if name == "ruleDelete" and r:
        sid = r.sensor_id
        if r.alarm_id:
            engine.close_alarm(r, timezone.now())
        r.delete()
        return _back({"rules": sid, "msg": "Alarm rule removed. Its alarms stay in the log."})
    if name == "ack":
        res = engine.ack(_i(p.get("alarm")), me.name)
        return _back({**back, "msg": res.get("msg", ""), "err": res.get("error", "")})
    return _back({**back, "err": "That isn't on file any more."})


@csrf_exempt
@require_POST
def api_push(request):
    """POST /api/sensors with header X-Feed-Key: {"barb_roll": 2.41, "sal_temp": 31} or {"sensor": "barb_roll", "value": 2.41}
    or a list of either. Only sensors set to "Sent to the server" take pushed readings."""
    from plant import liveconfig
    liveconfig.refresh()
    key = settings.SUITE.get("FEED_KEY")
    if not key or request.headers.get("X-Feed-Key") != key:
        return JsonResponse({"ok": False, "error": "Wrong or missing X-Feed-Key (set it in Operator Kiosk › Admin › Connections)."}, status=403)
    if len(request.body) > 200_000:
        return JsonResponse({"ok": False, "error": "Too much at once."}, status=413)
    try:
        payload = json.loads(request.body.decode() or "null")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({"ok": False, "error": "Send JSON."}, status=400)
    if not isinstance(payload, (dict, list)):
        return JsonResponse({"ok": False, "error": 'Send {"sensor_id": value}.'}, status=400)
    res = engine.push(payload)
    return JsonResponse({"ok": res["stored"] > 0 or not (res["unknown"] or res["errors"]), **res}, status=200 if res["stored"] or not res["unknown"] else 404)
