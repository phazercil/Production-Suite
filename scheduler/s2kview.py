"""Scheduler › S2K posting: what the floor finished, next to what S2K says, ready to key in or import (it was a
Supervisor tab). S2K is read-only to the suite; see supervisor/s2k.py."""
import csv

from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from hmi.views import current_person, touch
from plant import perms
from plant.models import Machine
from supervisor import s2k
from supervisor.views import _query, filters

VIEW = ("schedule.edit", "section.supervisor")   # the planners and the supervisors
POST = ("schedule.edit", "history.edit")


def _can(me, which):
    return any(perms.can(me, p) for p in which if p in perms.PERMS)


def _gate(request):
    me, note = current_person(request)
    if _can(me, VIEW):
        touch(request)
        return me, None
    msg = note or (f"{me.name} doesn't have permission to see S2K posting (Admin › Permissions › Change the schedule)." if me else None)
    return None, render(request, "supervisor/login.html", {"mode": "scheduler", "note": msg, "perm": "schedule.edit", "title": "S2K posting",
                                                           "blurb": "What the floor finished, lined up against S2K, ready to post. Planners and supervisors."})


def _q(f, **kw):
    return _query({**f, "tab": ""}, **kw)


def page(request):
    me, denied = _gate(request)
    if denied:
        return denied
    f = filters(request.GET)
    R = s2k.rows(f["A"], f["B"], f["line"], only_open=f["result"] == "open")
    if request.GET.get("csv") == "1":
        resp = HttpResponse(content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="s2k-to-post-{f["from"]}-to-{f["to"]}.csv"'
        w = csv.writer(resp)
        for row in s2k.csv_rows(R):
            w.writerow(row)
        return resp
    lines = sorted({m.name for m in Machine.objects.all()} | ({f["line"]} if f["line"] else set()))
    return render(request, "scheduler/s2k.html", {
        "mode": "scheduler", "me": me, "f": f, "S2K": R, "lines": lines,
        "totals": {"MOs": len(R), "To post": sum(x["unposted"] for x in R), "Don't agree": sum(1 for x in R if x["flags"])},
        "csv": _q(f, csv="1"), "canPost": _can(me, POST), "msg": request.GET.get("msg", "")[:200]})


@require_http_methods(["POST"])
def mark(request):
    """Mark these as posted: the rows the CSV holds for these filters."""
    me, denied = _gate(request)
    if denied:
        return denied
    f = filters(request.POST)
    if not _can(me, POST):
        return HttpResponseRedirect("/scheduler/s2k/?" + _q(f, msg="Marking as posted needs permission to change the schedule or correct history."))
    from hmi.engine import Ctx, audit
    n = s2k.mark(s2k.rows(f["A"], f["B"], f["line"]), me.name, timezone.now())
    audit(Ctx(user=me), f"S2K posting: marked {n} rows as posted ({f['from']} – {f['to']}{', ' + f['line'] if f['line'] else ''})")
    return HttpResponseRedirect("/scheduler/s2k/?" + _q(f, msg=f"{n} rows marked as posted in S2K."))
