"""Maintenance: the section of the suite for the maintenance team.

  PM schedule   planned maintenance by machine: every so many days, run hours or filters made; what is due, due
                soon and open, with an estimate of when each falls due. Tasks are added and changed here.
  Requests      the maintenance requests from the lines (open and recent), with a link to the Maintenance page
                of the Operator Kiosk where repair notes and pictures are added.
  PM history    planned maintenance done, by whom, and whether it was done on time.

People with "Open Maintenance" (Admin › Permissions) only; changing the PM schedule needs "PM schedule", logging
a job done needs "Work maintenance requests". Plant rules are in hmi/pm.py and hmi/engine.py.
"""
from datetime import timedelta
from urllib.parse import urlencode

from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from hmi import pm
from hmi.models import PMTask, Ticket
from hmi.views import current_person, touch
from plant import perms
from plant.models import Machine, PlantSettings
from plant.util import now

PERM = "section.maintenance"
TABS = [("pm", "PM schedule"), ("requests", "Requests"), ("history", "PM history")]
ACTIONS = {"pmSave": "pm", "pmDelete": "pm", "pmDone": "pm", "pmOpenNow": "pm"}
STATE = {"due": ("bad", "DUE"), "open": ("warn", "REQUEST OPEN"), "soon": ("warn", "DUE SOON"), "ok": ("ok", "OK"), "off": ("idle", "PAUSED")}
ORDER = {"due": 0, "open": 1, "soon": 2, "ok": 3, "off": 4}


def _gate(request):
    me, note = current_person(request)
    if perms.can(me, PERM):
        touch(request)
        return me, None
    msg = note or (f"{me.name} doesn't have permission to open Maintenance (Admin › Permissions › Open Maintenance)." if me else None)
    return None, render(request, "supervisor/login.html", {"mode": "maintenance", "note": msg, "title": "Maintenance", "perm": PERM,
                                                           "blurb": "Planned maintenance for every machine, and the requests from the lines."})


def equipment():
    """Machines first (in their order), then the other equipment from Admin › Maintenance lists."""
    ms = list(Machine.objects.order_by("sort", "id").values_list("name", flat=True))
    return ms, [x for x in PlantSettings.load().config.get("equipment", []) if x not in ms]


def schedule(t, machine=""):
    """Every PM task with where it stands, the ones that need doing first."""
    tasks = PMTask.objects.all()
    if machine:
        tasks = tasks.filter(machine=machine)
    opened = {}
    for x in Ticket.objects.filter(pm__isnull=False).exclude(status="Closed").order_by("t"):
        opened.setdefault(x.pm_id, x)
    rows = []
    for task in tasks:
        st = pm.status(task, t, opened.get(task.id))
        rows.append({"task": task, **st, "pill": STATE[st["state"]], "usedTxt": pm.fmt_amount(st["used"], task.unit),
                     "everyTxt": pm.fmt_amount(task.every, task.unit),
                     "leftTxt": pm.fmt_amount(abs(st["left"]), task.unit) + (" over" if st["left"] < 0 else " left")})
    rows.sort(key=lambda r: (ORDER[r["state"]], -r["pct"], r["task"].machine, r["task"].title))
    return rows


@ensure_csrf_cookie
@require_http_methods(["GET", "POST"])
def page(request):
    from hmi import engine
    me, denied = _gate(request)
    if denied:
        return denied
    q = request.GET if request.method == "GET" else request.POST
    tab = q.get("tab") if q.get("tab") in dict(TABS) else "pm"
    machine = str(q.get("m") or "")[:60]   # the machine the lists are narrowed to ("machine" is a task's own)
    err, form = "", None
    if request.method == "POST":
        a = request.POST.get("action") or ""
        if a in ACTIONS:
            p = {k: request.POST.get(k) for k in request.POST if k not in ("csrfmiddlewaretoken", "action")}
            if a == "pmSave":
                p["active"] = request.POST.get("active") == "1"
            r = engine.execute(a, p, engine.Ctx(user=me))
            if r.get("ok"):
                keep = {"tab": ACTIONS[a], "msg": r.get("msg") or "Saved."}
                if machine:
                    keep["m"] = machine
                return HttpResponseRedirect("/maintenance/?" + urlencode(keep))
            err, tab = r.get("error") or "That didn't save.", ACTIONS[a]
            form = p if a == "pmSave" else None
    t = now()
    machines, other = equipment()
    ctx = {"mode": "maintenance", "me": me, "tab": tabs_ctx(tab, machine), "tabs": TABS, "err": err, "msg": request.GET.get("msg", "")[:200],
           "machine": machine, "machines": machines, "other": other, "units": PMTask.UNITS, "t": t,
           "canEdit": perms.can(me, "pm.edit"), "canWork": perms.can(me, "tickets.work"),
           "form": {**{k: "" for k in ("machine", "title", "every", "unit", "priority", "steps")}, **(form or {})}}
    if tab == "pm":
        rows = schedule(t, machine)
        ctx.update(rows=rows, edit=_int(request.GET.get("edit")),
                   counts={k: sum(1 for r in rows if r["state"] == k) for k in ("due", "open", "soon")})
    elif tab == "requests":
        show = request.GET.get("show") if request.GET.get("show") in ("open", "done") else "open"
        qs = Ticket.objects.select_related("pm")
        qs = qs.exclude(status="Closed") if show == "open" else qs.filter(status="Closed", updated__gte=t - timedelta(days=30))
        if machine:
            qs = qs.filter(machine=machine)
        order = {"High": 0, "Medium": 1, "Low": 2}
        reqs = list(qs.order_by("-t")[:300])
        if show == "open":
            reqs.sort(key=lambda x: (order.get(x.priority, 1), x.t))
        ctx.update(reqs=[{"t": x, "age": round((t - x.t).total_seconds() / 60)} for x in reqs], show=show)
    else:
        rows = []
        for x in pm.history(since=t - timedelta(days=365)):
            task = x.pm
            late = None
            prev = Ticket.objects.filter(pm=task, status="Closed", updated__lt=x.updated).order_by("-updated").first() if task else None
            if task and task.unit == "days":
                start = prev.updated if prev else task.created
                late = (x.updated - start).total_seconds() / 86400 - task.every
            rows.append({"t": x, "task": task, "late": late, "waited": round((x.updated - x.t).total_seconds() / 60) if x.updated else None,
                         "note": (x.repairs.filter(kind="closed").order_by("-t").values_list("text", flat=True).first() or "")})
        if machine:
            rows = [r for r in rows if r["t"].machine == machine]
        ctx.update(hist=rows)
    return render(request, "maintenance/page.html", ctx)


def tabs_ctx(tab, machine):
    return {"on": tab, "q": {k: urlencode({"tab": k, **({"m": machine} if machine else {})}) for k, _ in TABS}}


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
