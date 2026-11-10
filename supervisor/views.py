"""Supervisor › History: every shift log (each opens its full End of Shift report), QC check, downtime entry and
changeover piece, filtered by line, dates, person, MO and result, with a CSV of each list. Read only. People with
"Open Supervisor" (Admin › Permissions) only."""
import csv
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from django.db.models import Count, Q, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET

from hmi.models import ChangeoverPiece, Downtime, QCCheck, ShiftRun
from hmi.views import current_person, touch
from plant import perms
from plant.models import Machine, PlantSettings
from plant.util import local

PERM = "section.supervisor"
PER_PAGE = 100
TABS = [("logs", "Shift logs"), ("qc", "QC checks"), ("down", "Downtime"), ("co", "Changeovers")]
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
    tab = q.get("tab") if q.get("tab") in dict(TABS) else "logs"
    try:
        pg = max(1, int(q.get("page") or 1))
    except ValueError:
        pg = 1
    return {"tab": tab, "line": str(q.get("line") or "")[:60], "from": a, "to": b, "who": str(q.get("who") or "").strip()[:60],
            "mo": str(q.get("mo") or "").strip()[:40], "result": str(q.get("result") or "")[:12], "empty": q.get("empty") == "1", "page": pg,
            "A": timezone.make_aware(datetime(a.year, a.month, a.day)), "B": timezone.make_aware(datetime(b.year, b.month, b.day)) + timedelta(days=1)}


def _query(f, **change):
    keep = {"tab": f["tab"], "line": f["line"], "from": f["from"].isoformat(), "to": f["to"].isoformat(), "who": f["who"], "mo": f["mo"],
            "result": f["result"], "empty": "1" if f["empty"] else ""}
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
    if not f["empty"]:   # the logs kept with nobody logged in and nothing made (nights, weekends) are left out
        qs = qs.exclude(Q(person__isnull=True) & Q(count=0))
    qs = qs.annotate(qc_n=Count("qccheck", distinct=True), qc_fail=Count("qccheck", filter=Q(qccheck__passed=False), distinct=True))
    tot = qs.aggregate(units=Sum("count"))
    return qs.order_by("-start"), {"Logs": None, "Units": tot["units"] or 0}


def _qc_labels():
    from hmi import qc
    lab = {k: v["label"] for k, v in qc.BUILTIN.items()}
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
    qs = QCCheck.objects.filter(t__gte=f["A"], t__lt=f["B"]).select_related("shift")
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


LISTS = {"logs": logs, "qc": qcs, "down": downs, "co": cos}


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
    else:
        yield ["Changeover", "Line", "From MO", "To MO", "Who", "Shift", "Start", "End", "Minutes", "Steps checked off", "Steps in all", "How it ended", "Note"]
        for c in qs:
            yield [c.co_id[:8], c.line, c.old_mo, c.new_mo, _who(c.by), c.shift_name, fmt(c.start), fmt(c.end), c.minutes, "; ".join(c.steps),
                   c.steps_total, c.ended, c.note]


# ------------------------------------------------------------------ pages
@ensure_csrf_cookie
@require_GET
def page(request):
    me, denied = _gate(request)
    if denied:
        return denied
    f = filters(request.GET)
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
    return render(request, "supervisor/page.html", {
        "mode": "supervisor", "me": me, "f": f, "tabs": TABS, "rows": rows, "n": n, "totals": totals, "lines": lines,
        "first": first + 1 if n else 0, "last": min(n, first + PER_PAGE),
        "prev": _query(f, page=f["page"] - 1) if f["page"] > 1 else None,
        "next": _query(f, page=f["page"] + 1) if first + PER_PAGE < n else None,
        "csv": _query(f, csv="1"), "here": _query(f), "tabq": {k: _query(f, tab=k, result="", page="") for k, _ in TABS},
        "nobody": NOBODY})


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
    return render(request, "supervisor/log.html", {"mode": "supervisor", "me": me, "P": P, "back": back if back.startswith("tab=") or not back else "",
                                                   "nobody": NOBODY})
