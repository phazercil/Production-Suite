"""Supervisor › History: every shift log (each opens its full End of Shift report), QC check, downtime entry and
changeover piece, filtered by line, dates, person, MO and result, with a CSV of each list. Read only. People with
"Open Supervisor" (Admin › Permissions) only."""
import csv
from datetime import date, datetime, timedelta
from urllib.parse import quote, urlencode

from django.db.models import Count, Q, Sum
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods

from cameras.models import Camera
from hmi.models import ChangeoverPiece, Downtime, QCCheck, ShiftRun, Ticket
from hmi.views import current_person, touch
from plant import perms
from plant.models import Machine, PlantSettings
from plant.util import local

PERM = "section.supervisor"
EDIT_PERM = "history.edit"   # Correct history: change, split or delete past records (supervisor/edit.py)
PER_PAGE = 100
TABS = [("status", "Line status"), ("logs", "Shift logs"), ("qc", "QC checks"), ("down", "Downtime"), ("co", "Changeovers"), ("morun", "MO runs"),
        ("events", "Timeline")]
NOBODY = "Nobody logged in"


def _gate(request):
    me, note = current_person(request)
    if perms.can(me, PERM):
        touch(request)
        return me, None
    msg = note or (f"{me.name} doesn't have permission to open Supervisor (Admin › Permissions › Open Supervisor)." if me else None)
    return None, render(request, "supervisor/login.html", {"mode": "supervisor", "note": msg})


def _day(v, default):
    try:
        return date.fromisoformat(str(v))
    except (TypeError, ValueError):
        return default


def filters(q):
    today = timezone.localdate()
    b = _day(q.get("to"), today)
    a = _day(q.get("from"), b - timedelta(days=6))
    if a > b:
        a, b = b, a
    a = max(a, b - timedelta(days=366))   # at most a year at a time
    tab = q.get("tab") if q.get("tab") in dict(TABS) else "status"
    try:
        pg = max(1, int(q.get("page") or 1))
    except ValueError:
        pg = 1
    return {"tab": tab, "line": str(q.get("line") or "")[:60], "from": a, "to": b, "who": str(q.get("who") or "").strip()[:60],
            "mo": str(q.get("mo") or "").strip()[:40], "result": str(q.get("result") or "")[:12], "empty": q.get("empty") == "1", "page": pg,
            "kind": str(q.get("kind") or "")[:12], "q": str(q.get("q") or "").strip()[:80],
            "A": timezone.make_aware(datetime(a.year, a.month, a.day)), "B": timezone.make_aware(datetime(b.year, b.month, b.day)) + timedelta(days=1)}


def _query(f, **change):
    keep = {"tab": f["tab"], "line": f["line"], "from": f["from"].isoformat(), "to": f["to"].isoformat(), "who": f["who"], "mo": f["mo"],
            "result": f["result"], "empty": "1" if f["empty"] else "", "kind": f.get("kind", ""), "q": f.get("q", "")}
    keep.update(change)
    return urlencode({k: v for k, v in keep.items() if v not in ("", None)})


# ------------------------------------------------------------------ the lists
def logs(f):
    qs = ShiftRun.objects.filter(start__gte=f["A"], start__lt=f["B"])
    if f["line"]:
        qs = qs.filter(line=f["line"])
    if f["who"]:
        qs = qs.filter(user_name__icontains=f["who"])
    if f["mo"]:
        qs = qs.filter(Q(mo__number__icontains=f["mo"]) | Q(morun__mo__icontains=f["mo"])).distinct()
    if not f["empty"]:   # left out: time between shifts with nobody logged in and nothing made (nights, weekends);
        # every scheduled shift's log shows, with or without an operator, so it can be looked at and corrected
        from hmi.engine import NO_SHIFT
        qs = qs.exclude(Q(person__isnull=True) & Q(count=0) & Q(shift_name__in=[NO_SHIFT, ""]))
    qs = qs.annotate(qc_n=Count("qccheck", distinct=True), qc_fail=Count("qccheck", filter=Q(qccheck__passed=False), distinct=True))
    tot = qs.aggregate(units=Sum("count"))
    return qs.order_by("-start"), {"Logs": None, "Units": tot["units"] or 0}


def _qc_labels():
    from hmi import qc
    lab = {k: v["label"] for k, v in qc.BUILTIN.items()}
    lab.update({x["key"]: x["label"] for x in qc.features()})
    for items in (PlantSettings.load().config.get("qcItems") or {}).values():
        for it in items if isinstance(items, list) else []:
            if isinstance(it, dict) and it.get("key") and it.get("label"):
                lab.setdefault(it["key"], it["label"])
    return lab


def qc_values(q, labels):
    vals = q.values or {k: getattr(q, k) for k in ("len", "wid", "ph", "sbl", "sbr", "glue", "cut", "scrap") if getattr(q, k) not in (None, "")}
    bad = set(q.fails or [])
    return [{"k": labels.get(k, k), "v": v, "bad": k in bad} for k, v in vals.items() if v not in (None, "")]


def qcs(f):
    qs = QCCheck.objects.filter(t__gte=f["A"], t__lt=f["B"]).select_related("shift", "recheck")
    if f["line"]:
        qs = qs.filter(line=f["line"])
    if f["who"]:
        qs = qs.filter(by__icontains=f["who"])
    if f["mo"]:
        qs = qs.filter(Q(mo__icontains=f["mo"]) | Q(item__icontains=f["mo"]))
    r = f["result"]
    if r == "pass":
        qs = qs.filter(passed=True)
    elif r == "fail":
        qs = qs.filter(passed=False)
    elif r in ("pending", "approved", "disapproved"):
        qs = qs.filter(review=r)
    elif r == "late":
        qs = qs.exclude(late_reason="")
    agg = qs.aggregate(n=Count("id"), fail=Count("id", filter=Q(passed=False)), pend=Count("id", filter=Q(review="pending")), scrap=Sum("scrap"))
    return qs.order_by("-t"), {"Checks": None, "Failed": agg["fail"], "Waiting for review": agg["pend"], "Scrap": agg["scrap"] or 0}


def downs(f):
    qs = Downtime.objects.filter(start_time__gte=f["A"], start_time__lt=f["B"]).select_related("shift")
    if f["line"]:
        qs = qs.filter(line=f["line"])
    if f["who"]:
        qs = qs.filter(Q(shift__user_name__icontains=f["who"]) | Q(by__icontains=f["who"]))
    if f["mo"]:
        qs = qs.filter(mo__icontains=f["mo"])
    if f["result"] == "none":
        qs = qs.filter(Q(reason__isnull=True) | Q(reason=""))
    elif f["result"] == "changeover":
        qs = qs.filter(reason="Changeover")
    agg = qs.aggregate(mins=Sum("minutes"))
    return qs.order_by("-start_time"), {"Stops": None, "Minutes": agg["mins"] or 0}


def cos(f):
    qs = ChangeoverPiece.objects.filter(start__gte=f["A"], start__lt=f["B"])
    if f["line"]:
        qs = qs.filter(line=f["line"])
    if f["who"]:
        qs = qs.filter(by__icontains=f["who"])
    if f["mo"]:
        qs = qs.filter(Q(old_mo__icontains=f["mo"]) | Q(new_mo__icontains=f["mo"]))
    agg = qs.aggregate(mins=Sum("minutes"), n=Count("co_id", distinct=True))
    return qs.order_by("-start"), {"Pieces": None, "Changeovers": agg["n"], "Minutes": agg["mins"] or 0}


def moruns(f):
    from hmi.models import MoRun
    qs = MoRun.objects.filter(start_time__gte=f["A"], start_time__lt=f["B"]).select_related("shift")
    if f["line"]:
        qs = qs.filter(line=f["line"])
    if f["who"]:
        qs = qs.filter(shift__user_name__icontains=f["who"])
    if f["mo"]:
        qs = qs.filter(Q(mo__icontains=f["mo"]) | Q(item__icontains=f["mo"]))
    agg = qs.aggregate(q=Sum("qty"), s=Sum("scrap"))
    return qs.order_by("-start_time"), {"Runs": None, "Pieces": agg["q"] or 0, "Scrap": agg["s"] or 0}


LISTS = {"logs": logs, "qc": qcs, "down": downs, "co": cos, "morun": moruns}


def _who(name):
    return name or NOBODY


def csv_rows(tab, qs):
    fmt = lambda dt: local(dt).strftime("%Y-%m-%d %H:%M") if dt else ""   # noqa: E731
    if tab == "logs":
        yield ["Line", "Shift", "Operator", "Start", "End", "Units", "Uptime %", "Downtime min", "QC checks", "QC failed", "Start issue", "End issue"]
        for x in qs:
            if x.active or x.uptime is None or x.down_mins is None:
                from hmi.engine import shift_stats
                st = shift_stats(x)
                x.uptime, x.down_mins = st["uptime"], st["down"]
            yield [x.line, x.shift_name, _who(x.user_name), fmt(x.start), fmt(x.end) if not x.active else "running", x.count,
                   x.uptime, x.down_mins, x.qc_n, x.qc_fail, x.issues, x.end_issues]
    elif tab == "qc":
        labels = _qc_labels()
        yield ["Time", "Line", "MO", "Item", "By", "Result", "Out of spec", "Values", "Scrap", "Review", "Reviewed by", "Late reason"]
        for q in qs:
            vals = qc_values(q, labels)
            yield [fmt(q.t), q.line, q.mo, q.item, q.by, "Pass" if q.passed else "Fail", "; ".join(v["k"] for v in vals if v["bad"]),
                   "; ".join(f"{v['k']} {v['v']}" for v in vals), q.scrap, q.review, q.reviewed_by, q.late_reason]
    elif tab == "down":
        yield ["Line", "Start", "End", "Minutes", "Reason", "Notes", "MO", "Operator", "Shift"]
        for d in qs:
            yield [d.line, fmt(d.start_time), fmt(d.end_time), d.minutes, d.reason or "Not entered", d.notes, d.mo,
                   _who(d.shift.user_name if d.shift else ""), d.shift.shift_name if d.shift else ""]
    elif tab == "morun":
        yield ["Line", "MO", "Item", "Qty", "Scrap", "Start", "End", "Operator", "Shift"]
        for r in qs:
            yield [r.line, r.mo, r.item, r.qty, r.scrap, fmt(r.start_time), fmt(r.end_time), _who(r.shift.user_name if r.shift else ""),
                   r.shift.shift_name if r.shift else ""]
    else:
        yield ["Changeover", "Line", "From MO", "To MO", "Who", "Shift", "Start", "End", "Minutes", "Steps checked off", "Steps in all", "How it ended", "Note"]
        for c in qs:
            yield [c.co_id[:8], c.line, c.old_mo, c.new_mo, _who(c.by), c.shift_name, fmt(c.start), fmt(c.end), c.minutes, "; ".join(c.steps),
                   c.steps_total, c.ended, c.note]


# ------------------------------------------------------------------ pages
def line_status():
    """Supervisor › Line status: each machine now: running, stopped or in a changeover, who's logged in, the MO and how
    many are made, the speed and today's shift count."""
    from hmi import qc
    from hmi.engine import shift_stats
    from hmi.models import PpmReading
    from plant.models import MO
    now = timezone.now()
    pills = {"running": ("ok", "Running"), "changeover": ("warn", "Changeover"), "stopped": ("bad", "Stopped")}
    cards = []
    for m in Machine.objects.filter(stage="s1").order_by("sort", "id"):
        sh = ShiftRun.objects.filter(machine=m, active=True).select_related("mo").first()
        last = PpmReading.objects.filter(machine=m).order_by("-t").values("t", "ppm").first()
        fresh = bool(last and (now - last["t"]).total_seconds() < 180)
        pill = pills.get(sh.state, ("idle", sh.state.title())) if sh else (("idle", "No shift") if m.active else ("bad", "Out of service"))
        # Running / stopped come from the machine's readings: none for 3 minutes (or never) and the state is only the last
        # one known, so say so instead of showing it running (a changeover is the operator's, not the machine's).
        nodata = bool(sh and sh.state in ("running", "stopped") and not fresh)
        if nodata:
            pill = ("warn", "No machine data")
        mo = sh.mo if sh else None
        nxt = None
        if sh and sh.state == "changeover" and (sh.co or {}).get("newMo"):
            nxt = MO.objects.filter(pk=sh.co["newMo"]).first()
        co_since = None
        if sh and sh.co and sh.co.get("step", 1) >= 2:
            from plant.util import from_ms
            co_since = from_ms(sh.co.get("first") or sh.co.get("start")) if (sh.co.get("first") or sh.co.get("start")) else None
        stats = shift_stats(sh) if sh else None
        made = max(0, mo.produced - mo.scrap) if mo else 0
        target = None
        if sh and sh.machine_id and m.ppm:
            from hmi.engine import pleats_for, rules
            per = pleats_for(sh)
            sched = PpmReading.objects.filter(shift=sh, planned=False).count()   # scheduled minutes so far (breaks left out)
            if per > 0 and sched:
                target = round(sched * m.ppm / per * float(rules().get("eff", 85) or 85) / 100)
        cards.append({"m": m, "sh": sh, "pill": pill, "mo": mo, "next": nxt, "made": made, "qty": mo.qty if mo else 0,
                      "pct": min(100, round(100 * made / mo.qty)) if mo and mo.qty else None,
                      "ppm": last["ppm"] if fresh else None, "since": last["t"] if last else None, "stale": bool(last) and not fresh,
                      "who": (sh.user_name or "") if sh else "", "stats": stats, "recheck": qc.recheck_for(m.name),
                      "co": bool(sh and sh.co and sh.co.get("step", 1) >= 2), "coSince": co_since,
                      "coMin": round((now - co_since).total_seconds() / 60) if co_since else None, "nodata": nodata,
                      "target": target, "behind": bool(target and sh.count < 0.9 * target),
                      "tickets": Ticket.objects.filter(machine=m.name).exclude(status="Closed").count(),
                      "cams": Camera.objects.filter(enabled=True, machines=m).exists()})
    run = sum(1 for c in cards if c["sh"] and c["sh"].state == "running" and not c["nodata"])
    tot = {"Running": run, "Stopped": sum(1 for c in cards if c["sh"] and c["sh"].state == "stopped" and not c["nodata"]),
           "No machine data": sum(1 for c in cards if c["nodata"]),
           "Changeover": sum(1 for c in cards if c["sh"] and c["sh"].state == "changeover"),
           "Made this shift": sum(c["sh"].count for c in cards if c["sh"])}
    if not tot["No machine data"]:
        del tot["No machine data"]
    # what needs someone now, plant-wide (only shown when there is any)
    need_ = {"Failed QC waiting": QCCheck.objects.filter(passed=False, review="pending").count(),
             "High-priority requests": Ticket.objects.exclude(status="Closed").filter(priority="High").count()}
    try:
        from sensors.models import SensorAlarm
        need_["Sensor alarms"] = SensorAlarm.objects.filter(end__isnull=True).count()
    except Exception:  # noqa: BLE001
        pass
    tot.update({k: v for k, v in need_.items() if v})
    return cards, tot


def open_changeovers():
    """Every line with a changeover open now (the order ended), longest-open first: for the Force end buttons."""
    from plant.util import from_ms
    now = timezone.now()
    out = []
    for sh in ShiftRun.objects.filter(active=True, co__isnull=False).select_related("machine"):
        co = sh.co or {}
        if co.get("step", 1) < 2:
            continue
        t = co.get("first") or co.get("start")
        since = from_ms(t) if t else None
        out.append({"sh": sh, "since": since, "min": round((now - since).total_seconds() / 60) if since else None,
                    "who": sh.user_name or "", "picked": bool(co.get("newMo"))})
    return sorted(out, key=lambda x: x["since"] or now)


@ensure_csrf_cookie
@require_GET
def page(request):
    me, denied = _gate(request)
    if denied:
        return denied
    if request.GET.get("tab") == "s2k":   # S2K posting moved to the Scheduler
        q = request.GET.copy()
        q.pop("tab")
        return HttpResponseRedirect("/scheduler/s2k/" + (f"?{q.urlencode()}" if q else ""))
    f = filters(request.GET)
    if f["tab"] == "status":
        cards, totals = line_status()
        return render(request, "supervisor/page.html", {
            "mode": "supervisor", "me": me, "f": f, "tabs": TABS, "cards": cards, "totals": totals, "now": timezone.now(), "openCo": open_changeovers(),
            "tabq": {k: _query(f, tab=k, result="", page="") for k, _ in TABS}, "nobody": NOBODY, "msg": request.GET.get("msg", "")[:200]})
    if f["tab"] == "events":
        from . import timeline
        cut = {}
        evs = timeline.events(f, f["kind"], f["q"], info=cut)
        if request.GET.get("csv") == "1":
            resp = HttpResponse(content_type="text/csv; charset=utf-8")
            resp["Content-Disposition"] = f'attachment; filename="timeline-{f["from"]}-to-{f["to"]}.csv"'
            w = csv.writer(resp)
            for row in timeline.csv_rows(evs):
                w.writerow(row)
            return resp
        n = len(evs)
        first = (f["page"] - 1) * PER_PAGE
        by_kind = {}
        for e in evs:
            by_kind[e["label"]] = by_kind.get(e["label"], 0) + 1
        lines = sorted({m.name for m in Machine.objects.all()} | ({f["line"]} if f["line"] else set()))
        return render(request, "supervisor/page.html", {
            "mode": "supervisor", "me": me, "f": f, "tabs": TABS, "rows": evs[first:first + PER_PAGE], "n": n, "lines": lines, "cut": cut,
            "totals": {"Events": n, **dict(sorted(by_kind.items(), key=lambda kv: -kv[1])[:4])}, "kinds": timeline.KINDS, "openCo": open_changeovers(),
            "first": first + 1 if n else 0, "last": min(n, first + PER_PAGE),
            "prev": _query(f, page=f["page"] - 1) if f["page"] > 1 else None,
            "next": _query(f, page=f["page"] + 1) if first + PER_PAGE < n else None,
            "csv": _query(f, csv="1"), "here": _query(f), "tabq": {k: _query(f, tab=k, result="", page="") for k, _ in TABS},
            "nobody": NOBODY, "canEdit": perms.can(me, EDIT_PERM), "msg": request.GET.get("msg", "")[:200]})
    qs, totals = LISTS[f["tab"]](f)
    if request.GET.get("csv") == "1":
        resp = HttpResponse(content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="{f["tab"]}-{f["from"]}-to-{f["to"]}.csv"'
        w = csv.writer(resp)
        for row in csv_rows(f["tab"], qs.iterator() if hasattr(qs, "iterator") else qs):
            w.writerow(row)
        return resp
    n = qs.count()
    first = (f["page"] - 1) * PER_PAGE
    rows = list(qs[first:first + PER_PAGE])
    totals = {k: (n if v is None else v) for k, v in totals.items()}
    if f["tab"] == "logs":   # logs closed before their figures were kept: work them out
        from hmi.engine import shift_stats
        for x in rows:
            if x.active or x.uptime is None or x.down_mins is None:
                st = shift_stats(x)
                x.uptime, x.down_mins = st["uptime"], st["down"]
    if f["tab"] == "qc":
        labels = _qc_labels()
        for q in rows:
            q.vals = qc_values(q, labels)
    lines = sorted({m.name for m in Machine.objects.all()} | ({f["line"]} if f["line"] else set()))
    co_rep = None
    if f["tab"] == "co":
        from scheduler import cohist
        co_rep = cohist.report(f["A"], f["B"], f["line"])
    return render(request, "supervisor/page.html", {
        "mode": "supervisor", "me": me, "f": f, "tabs": TABS, "rows": rows, "n": n, "totals": totals, "lines": lines, "openCo": open_changeovers(),
        "coRep": co_rep, "reasons": _reasons() if f["tab"] == "down" else [],
        "first": first + 1 if n else 0, "last": min(n, first + PER_PAGE),
        "prev": _query(f, page=f["page"] - 1) if f["page"] > 1 else None,
        "next": _query(f, page=f["page"] + 1) if first + PER_PAGE < n else None,
        "csv": _query(f, csv="1"), "here": _query(f), "backq": quote("/supervisor/?" + _query(f), safe=""), "tabq": {k: _query(f, tab=k, result="", page="") for k, _ in TABS},
        "nobody": NOBODY, "canEdit": perms.can(me, EDIT_PERM), "msg": request.GET.get("msg", "")[:200]})


@ensure_csrf_cookie
@require_GET
def log(request, pk):
    me, denied = _gate(request)
    if denied:
        return denied
    sh = ShiftRun.objects.select_related("mo", "machine").filter(pk=pk).first()
    if not sh:
        raise Http404("No such log")
    from hmi.kscreen import report_for
    P = report_for(sh)
    labels = _qc_labels()
    for q in P["qcList"]:
        q.vals = qc_values(q, labels)
    back = request.GET.get("back") or ""
    back = back if back.startswith("tab=") or not back else ""
    from .models import HistoryEdit
    return render(request, "supervisor/log.html", {"mode": "supervisor", "me": me, "P": P, "back": back, "nobody": NOBODY,
                                                   "canEdit": perms.can(me, EDIT_PERM), "here": f"/supervisor/log/{sh.pk}/" + (f"?back={quote(back)}" if back else ""),
                                                   "msg": request.GET.get("msg", "")[:200],
                                                   "edits": HistoryEdit.objects.filter(kind="log", obj_id=sh.pk)[:50]})


# ------------------------------------------------------------------ corrections
def _local_input(dt):
    return local(dt).strftime("%Y-%m-%dT%H:%M") if dt else ""


def _fields(kind, x):
    """The form for a record: [{name, label, type, value, options?, help?}]."""
    from hmi import engine
    if kind == "downtime":
        reasons = list(engine.config().get("reasons") or [])
        if x.reason and x.reason not in reasons:
            reasons.append(x.reason)
        return [{"name": "start", "label": "Start", "type": "datetime-local", "value": _local_input(x.start_time)},
                {"name": "end", "label": "End", "type": "datetime-local", "value": _local_input(x.end_time)},
                {"name": "reason", "label": "Reason", "type": "select", "value": x.reason or "", "options": [("", "Not entered")] + [(r, r) for r in reasons]},
                {"name": "notes", "label": "Notes", "type": "textarea", "value": x.notes},
                {"name": "mo", "label": "MO", "type": "text", "value": x.mo}]
    if kind == "log":
        from plant.models import Person
        names = list(Person.objects.order_by("name").values_list("name", flat=True))
        if x.user_name and x.user_name not in names:
            names.append(x.user_name)
        out = [{"name": "operator", "label": "Operator", "type": "select", "value": x.user_name or "",
                "options": [("", NOBODY)] + [(n, n) for n in names]},
               {"name": "shift_name", "label": "Shift", "type": "text", "value": x.shift_name},
               {"name": "count", "label": "Units made", "type": "number", "value": x.count},
               {"name": "start", "label": "Start", "type": "datetime-local", "value": _local_input(x.start)}]
        if not x.active:
            out.append({"name": "end", "label": "End", "type": "datetime-local", "value": _local_input(x.end)})
        return out + [{"name": "issues", "label": "Start-of-shift issue", "type": "textarea", "value": x.issues},
                      {"name": "end_issues", "label": "End-of-shift issue", "type": "textarea", "value": x.end_issues}]
    if kind == "qc":
        from hmi import qc
        items = qc.items_for(x.shift.machine.stage if x.shift and x.shift.machine else "s1")
        vals = dict(x.values or {}) or {k: getattr(x, k) for k in ("len", "wid", "ph", "sbl", "sbr", "glue", "cut", "scrap")}   # older checks
        out = []
        for it in items:
            if it["type"] == "check":
                out.append({"name": it["key"], "label": it["label"], "type": "select", "value": vals.get(it["key"], ""),
                            "options": [("Pass", "Pass"), ("Fail", "Fail")]})
            else:
                out.append({"name": it["key"], "label": it["label"] + (" (in)" if it["type"] == "measure" else ""), "type": "number",
                            "step": "any", "value": "" if vals.get(it["key"]) is None else vals.get(it["key"])})
        out.insert(0, {"name": "item", "label": "Part (filter number)", "type": "text", "value": x.item,
                       "help": "Judged against this part's drawing, read fresh from the asset database." if x.item else
                       "Missing: the check was saved with no MO running. Enter the part so it's judged against its drawing."})
        return out + [{"name": "by", "label": "Checked by", "type": "text", "value": x.by},
                      {"name": "review", "label": "Review", "type": "select", "value": x.review,
                       "options": [("", "Not needed"), ("pending", "Waiting for review"), ("approved", "Approved"), ("disapproved", "Disapproved")],
                       "help": "Pass or fail follows the values, the part's drawing and Quality › Tolerances."},
                      {"name": "late_reason", "label": "Late reason", "type": "textarea", "value": x.late_reason}]
    if kind == "co":
        return [{"name": "by", "label": "Who", "type": "text", "value": x.by, "help": "Blank: nobody logged in"},
                {"name": "shift_name", "label": "Shift", "type": "text", "value": x.shift_name},
                {"name": "start", "label": "Start", "type": "datetime-local", "value": _local_input(x.start)},
                {"name": "end", "label": "End", "type": "datetime-local", "value": _local_input(x.end)},
                {"name": "ended", "label": "How it ended", "type": "select", "value": x.ended,
                 "options": [(k, k.capitalize()) for k in ("finished", "passed over", "shift change", "operator change", "picked up", "shift ended")]},
                {"name": "note", "label": "Note", "type": "textarea", "value": x.note}]
    return [{"name": "mo", "label": "MO", "type": "text", "value": x.mo}, {"name": "item", "label": "Item", "type": "text", "value": x.item},
            {"name": "qty", "label": "Qty", "type": "number", "value": x.qty}, {"name": "scrap", "label": "Scrap", "type": "number", "value": x.scrap},
            {"name": "start", "label": "Start", "type": "datetime-local", "value": _local_input(x.start_time)},
            {"name": "end", "label": "End", "type": "datetime-local", "value": _local_input(x.end_time)}]


@ensure_csrf_cookie
@require_http_methods(["GET", "POST"])
def edit(request, kind, pk):
    from hmi import engine

    from . import edit as E
    from .models import HistoryEdit
    me, denied = _gate(request)
    if denied:
        return denied
    if kind not in E.KINDS:
        raise Http404("No such record")
    x = E.KINDS[kind].objects.filter(pk=pk).first()
    back = request.GET.get("back") or request.POST.get("back") or ""
    back = back if back.startswith(("/supervisor/", "/quality/")) else "/supervisor/"
    if not x:
        return HttpResponseRedirect(back)
    can = perms.can(me, EDIT_PERM)
    err = ""
    if request.method == "POST":
        if not can:
            err = "You don't have permission to correct history (Admin › Permissions › Correct history)."
        else:
            a = request.POST.get("action")
            p = {k: request.POST.get(k) for k in request.POST if k not in ("csrfmiddlewaretoken", "action", "back")}
            p.update(kind=kind, id=x.pk)
            if a == "delete" and request.POST.get("sure") != "1":
                r = {"ok": False, "error": "Tick “Yes, delete it” to delete."}
            else:
                r = engine.execute({"save": "histEdit", "delete": "histDelete", "split": "downSplit"}.get(a, "histEdit"), p, engine.Ctx(user=me))
            if r.get("ok"):
                msg = (r.get("msg") or "Saved.")[:200]
                if a == "delete" or (kind == "log" and a == "delete"):
                    sep = "&" if "?" in back else "?"
                    return HttpResponseRedirect(f"{back}{sep}{urlencode({'msg': msg})}")
                return HttpResponseRedirect(f"/supervisor/edit/{kind}/{x.pk}/?{urlencode({'back': back, 'msg': msg})}")
            err = r.get("error") or "That didn't save."
            x = E.KINDS[kind].objects.get(pk=pk)
    sh = x if kind == "log" else getattr(x, "shift", None)
    return render(request, "supervisor/edit.html", {
        "mode": "supervisor", "me": me, "kind": kind, "label": E.LABEL[kind], "x": x, "summary": E.summary(kind, x), "fields": _fields(kind, x),
        "can": can, "err": err, "msg": request.GET.get("msg", "")[:200], "back": back, "sh": sh,
        "canDelete": not (kind == "log" and x.active), "split": kind == "downtime",
        "edits": HistoryEdit.objects.filter(kind=kind, obj_id=x.pk)[:50]})


@ensure_csrf_cookie
@require_http_methods(["GET", "POST"])
def co_force_end(request, pk):
    """Force-end a changeover nobody finished, from Supervisor › Line status. Needs "Supervisor override"."""
    from hmi import engine
    from plant.util import from_ms
    me, denied = _gate(request)
    if denied:
        return denied
    sh = ShiftRun.objects.select_related("mo").filter(pk=pk, active=True).first()
    back = "/supervisor/?tab=status"
    if not sh or not sh.co:
        return HttpResponseRedirect(f"{back}&{urlencode({'msg': 'That changeover has already ended.'})}")
    can = perms.can(me, "floor.override")
    err = ""
    if request.method == "POST":
        r = engine.execute("coForceEnd", {"line": sh.line, "why": request.POST.get("why"), "run": request.POST.get("run") == "1"},
                           engine.Ctx(user=me, line=sh.line))
        if r.get("ok"):
            return HttpResponseRedirect(f"{back}&{urlencode({'msg': r.get('msg', 'Changeover ended.')})}")
        err = r.get("error") or "That didn't save."
    from plant.models import MO
    co = sh.co
    steps = engine.co_steps(co, sh.line)
    done = set(co.get("done") or [])
    start = co.get("first") or co.get("start")
    return render(request, "supervisor/coforce.html", {
        "mode": "supervisor", "me": me, "sh": sh, "co": co, "can": can, "err": err, "back": back,
        "old": MO.objects.filter(pk=co.get("oldMo")).first() if co.get("oldMo") else None,
        "new": MO.objects.filter(pk=co.get("newMo")).first() if co.get("newMo") else None,
        "steps": [{"text": s.get("text", ""), "done": i in done} for i, s in enumerate(steps)],
        "since": from_ms(start) if start else None, "why": request.POST.get("why", "") if err else "",
        "pieces": co.get("pieces") or []})


@ensure_csrf_cookie
@require_http_methods(["GET", "POST"])
def process_out(request, pk):
    """Process Out a failed QC check: how many filters failed the QC (taken off the count) and what the quality issue
    was. From Supervisor › History and Quality › Quality report; doing it needs "Approve out-of-spec QC"."""
    from hmi import engine
    me, _ = current_person(request)
    if not (perms.can(me, PERM) or perms.can(me, "section.quality")):
        return _gate(request)[1]
    touch(request)
    back = request.GET.get("back") or request.POST.get("back") or ""
    back = back if back.startswith(("/supervisor/", "/quality/")) else "/supervisor/?tab=qc"
    q = QCCheck.objects.select_related("shift").filter(pk=pk).first()
    if not q:
        return HttpResponseRedirect(back)
    can = perms.can(me, "qc.approve")
    err = ""
    if request.method == "POST":
        r = engine.execute("qcProcessOut", {"id": q.pk, "qty": request.POST.get("qty"), "issue": request.POST.get("issue"),
                                            "note": request.POST.get("note")}, engine.Ctx(user=me))
        if r.get("ok"):
            sep = "&" if "?" in back else "?"
            return HttpResponseRedirect(f"{back}{sep}{urlencode({'msg': r.get('msg', 'Processed out.')})}")
        err = r.get("error") or "That didn't save."
    issues = PlantSettings.load().config.get("qualityIssues") or []
    labels = _qc_labels()
    return render(request, "supervisor/processout.html", {
        "mode": "supervisor", "me": me, "q": q, "vals": qc_values(q, labels), "issues": issues, "can": can, "err": err, "back": back,
        "form": {"qty": request.POST.get("qty") if err else (q.scrapped or ""), "issue": request.POST.get("issue") if err else q.issue,
                 "note": request.POST.get("note") if err else q.issue_note}})


# ------------------------------------------------------------------ History builder (supervisor/build.py)
def _rows_posted(post):
    from .build import MAX_ROWS
    keys = ("who", "from", "to", "mo", "item", "qty", "scrap")
    return [{k: post.get(f"{k}_{i}", "") for k in keys} for i in range(MAX_ROWS)]


def _builder_ctx(me, rows, msg="", err=""):
    from plant.models import Person

    from .build import MAX_ROWS
    rows = list(rows or [])
    shown = max(3, sum(1 for r in rows if any(str(v).strip() for v in r.values())) + 1)
    rows += [{} for _ in range(MAX_ROWS - len(rows))]
    return {"mode": "supervisor", "me": me, "can": perms.can(me, EDIT_PERM), "msg": msg, "err": err, "nobody": NOBODY,
            "people": list(Person.objects.order_by("name").values_list("name", flat=True)),
            "rows": [{"i": i, "r": r, "hide": i >= shown} for i, r in enumerate(rows[:MAX_ROWS])]}


@ensure_csrf_cookie
@require_GET
def build_list(request):
    """Supervisor › History builder: the logs that need the paper log."""
    from .build import to_fill
    me, denied = _gate(request)
    if denied:
        return denied
    q = dict(request.GET.items())
    q.setdefault("from", (timezone.localdate() - timedelta(days=13)).isoformat())
    f = filters(q)
    want = request.GET.get("need") or ""
    rows = to_fill(f["line"], f["A"], f["B"])
    if want in ("operator", "parts"):
        rows = [x for x in rows if x.need == want]
    lines = sorted({m.name for m in Machine.objects.all()} | ({f["line"]} if f["line"] else set()))
    return render(request, "supervisor/build_list.html", {
        "mode": "supervisor", "me": me, "f": f, "need": want, "rows": rows, "lines": lines, "nobody": NOBODY, "can": perms.can(me, EDIT_PERM),
        "totals": {"Logs": len(rows), "No operator": sum(1 for x in rows if x.need == "operator"),
                   "No parts": sum(1 for x in rows if x.need == "parts"), "Units": sum(x.count for x in rows)},
        "msg": request.GET.get("msg", "")[:200]})


@ensure_csrf_cookie
@require_http_methods(["GET", "POST"])
def build_fill(request, pk):
    """Fill in one log from the paper log."""
    from hmi import engine
    from hmi.models import MoRun

    from .build import hours
    me, denied = _gate(request)
    if denied:
        return denied
    sh = ShiftRun.objects.select_related("mo", "machine").filter(pk=pk).first()
    if not sh:
        raise Http404("No such log")
    err, rows = "", None
    if request.method == "POST":
        rows = _rows_posted(request.POST)
        if not perms.can(me, EDIT_PERM):
            err = "You don't have permission to correct history (Admin › Permissions › Correct history)."
        else:
            r = engine.execute("histFill", {"id": sh.pk, "rows": rows, "why": request.POST.get("why", "")}, engine.Ctx(user=me))
            if r.get("ok"):
                return HttpResponseRedirect(f"/supervisor/build/?{urlencode({'msg': r.get('msg') or 'Saved.'})}")
            err = r.get("error") or "That didn't save."
    if rows is None:   # start with one row for the whole log: its operator and MO, if it has them
        rows = [{"who": sh.user_name, "from": local(sh.start).strftime("%H:%M"), "to": local(sh.end).strftime("%H:%M") if sh.end else "",
                 "mo": sh.mo.number if sh.mo else "", "item": sh.mo.part if sh.mo else "", "qty": "", "scrap": ""}]
    st = engine.shift_stats(sh)
    ctx = _builder_ctx(me, rows, request.GET.get("msg", "")[:200], err)
    ctx.update({"sh": sh, "hours": hours(sh), "st": st, "runs": MoRun.objects.filter(shift=sh).order_by("start_time"),
                "downs": Downtime.objects.filter(shift=sh).order_by("start_time"), "here": f"/supervisor/build/{sh.pk}/"})
    return render(request, "supervisor/build_fill.html", ctx)


@ensure_csrf_cookie
@require_http_methods(["GET", "POST"])
def build_add(request):
    """Add a log by hand for time with no record at all."""
    from hmi import engine
    me, denied = _gate(request)
    if denied:
        return denied
    err, rows = "", [{}]
    form = {"machine": request.GET.get("machine", ""), "day": timezone.localdate().isoformat(), "start": "", "end": "", "shift_name": "", "why": ""}
    if request.method == "POST":
        rows = _rows_posted(request.POST)
        form.update({k: request.POST.get(k, "") for k in form})
        if not perms.can(me, EDIT_PERM):
            err = "You don't have permission to correct history (Admin › Permissions › Correct history)."
        else:
            r = engine.execute("histAdd", {**form, "rows": rows}, engine.Ctx(user=me))
            if r.get("ok"):
                return HttpResponseRedirect(f"/supervisor/build/?{urlencode({'msg': r.get('msg') or 'Saved.'})}")
            err = r.get("error") or "That didn't save."
    ctx = _builder_ctx(me, rows, "", err)
    ctx.update({"form": form, "machines": list(Machine.objects.order_by("sort", "id").values_list("name", flat=True))})
    return render(request, "supervisor/build_add.html", ctx)


def _reasons():
    from hmi import engine
    return list(engine.config().get("reasons") or [])


@require_http_methods(["POST"])
def down_batch(request):
    """Supervisor › Downtime: the ticked stops get one reason (supervisor/edit.py down_batch)."""
    me, denied = _gate(request)
    if denied:
        return denied
    back = request.POST.get("back") or "tab=down"
    if not back.startswith("tab="):
        back = "tab=down"
    from hmi import engine
    name = "downBatchDelete" if request.POST.get("action") == "delete" else "downBatch"
    r = engine.execute(name, {"ids": request.POST.getlist("ids"), "reason": request.POST.get("reason"),
                              "why": request.POST.get("why")}, engine.Ctx(user=me))
    return HttpResponseRedirect("/supervisor/?" + back + "&" + urlencode({"msg": r.get("msg") if r["ok"] else r["error"]}))
