"""Stations: a tablet screen for the stages after the pleater (End Band, Arts & Crafts, Glue Bead).

Each station picks its line and sees the work the Scheduler has queued for it, in order. For each MO the operator
presses Start, logs how many are done as they go, and Complete when the stage is finished. The Scheduler then plans
what is really left: a started stage runs from now with what's left to do, a completed one leaves the board (and
packaging follows on). Opened with "Open Stations" (Admin › Permissions); the person logged in signs each step.
"""
from datetime import datetime, timedelta
from urllib.parse import urlencode

from django.db import IntegrityError, transaction
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from hmi.engine import audit, event
from hmi.views import current_person, touch
from plant import perms
from plant.models import STAGES, Machine

from .engine import refresh_floor_queue
from .models import Job

PERM = "section.stations"
MID = ("eb", "ac", "gb")


def _gate(request):
    me, note = current_person(request)
    if perms.can(me, PERM):
        touch(request)
        return me, None
    msg = note or (f"{me.name} doesn't have permission to open Stations (Admin › Permissions › Open Stations)." if me else None)
    return None, render(request, "supervisor/login.html", {"mode": "stations", "note": msg, "perm": PERM, "title": "Stations",
                                                           "blurb": "End Band, Arts & Crafts and Glue Bead: start each MO, log how many are done, "
                                                                    "and complete the stage, so the schedule knows what is really left."})


def queue(m):
    from .engine import compute, load_world
    jobs = list(Job.objects.filter(machine=m, op=m.stage, wip_done__isnull=True).select_related("mo").order_by("req_start", "id"))
    try:
        res = compute(load_world())["res"]
    except Exception:  # noqa: BLE001 - the list still shows without the planned times
        res = {}
    out = []
    for j in jobs:
        r = res.get(j.key) or {}
        total = j.mo.stage_qty.get(m.stage) if isinstance(j.mo.stage_qty, dict) and j.mo.stage_qty.get(m.stage) else j.mo.qty
        out.append({"j": j, "mo": j.mo, "total": total, "left": max(0, (total or 0) - (j.wip_qty or 0)),
                    "start": datetime.fromtimestamp(r["start"] / 1000, tz=timezone.get_current_timezone()) if r.get("start") else None,
                    "end": datetime.fromtimestamp(r["end"] / 1000, tz=timezone.get_current_timezone()) if r.get("end") else None,
                    "pct": min(100, round(100 * (j.wip_qty or 0) / total)) if total else 0})
    return out


@ensure_csrf_cookie
@require_GET
def page(request):
    me, denied = _gate(request)
    if denied:
        return denied
    lines = list(Machine.objects.filter(stage__in=MID, active=True).order_by("stage", "sort", "id"))
    name = request.GET.get("line") or request.session.get("stations.line") or (lines[0].name if lines else "")
    m = next((x for x in lines if x.name == name), lines[0] if lines else None)
    if m:
        request.session["stations.line"] = m.name
    done = list(Job.objects.filter(machine=m, wip_done__gte=timezone.now() - timedelta(days=1)).select_related("mo")
                .order_by("-wip_done")[:20]) if m else []
    return render(request, "scheduler/stations.html", {"mode": "stations", "me": me, "lines": lines, "m": m, "stage": dict(STAGES).get(m.stage) if m else "",
                                                       "q": queue(m) if m else [], "done": done, "msg": request.GET.get("msg", ""),
                                                       "err": request.GET.get("err", "")})


class _No(Exception):
    pass


def _act(j, name, post, me, now, stage):
    if name == "start":
        if j.wip_started:
            raise _No(f"MO {j.mo.number} is already started.")
        busy = Job.objects.filter(machine=j.machine, op=j.op, wip_started__isnull=False, wip_done__isnull=True).exclude(pk=j.pk).first()
        if busy:
            raise _No(f"MO {busy.mo.number} is still under way on {j.machine.name}: complete it first.")
        j.wip_started, j.wip_by, j.wip_lane = now, me.name, lane(j)
        j.save(update_fields=["wip_started", "wip_by", "wip_lane"])
        return f"{stage} started on MO {j.mo.number}."
    if name in ("qty", "complete"):
        if not j.wip_started:
            raise _No(f"Start MO {j.mo.number} first.")
        raw = str(post.get("qty") or "").strip()
        if raw:
            try:
                n = float(raw)
            except ValueError:
                raise _No("Type how many as a number.")
            if n < 0 or n > 10_000_000:
                raise _No("That quantity isn't possible.")
            j.wip_qty = (j.wip_qty or 0) + n if post.get("how") == "add" else n   # (the row is locked: no lost update)
        if name == "complete":
            j.wip_done, j.wip_lane = now, None
        j.save(update_fields=["wip_qty", "wip_done", "wip_lane"])
        return f"{stage} on MO {j.mo.number}: {j.wip_qty:,.0f} done" + (" · completed" if name == "complete" else "")
    if name == "undo":
        if not j.wip_done:
            raise _No("That stage isn't completed.")
        if Job.objects.filter(machine=j.machine, op=j.op, wip_started__isnull=False, wip_done__isnull=True).exclude(pk=j.pk).exists():
            raise _No(f"Another MO is under way on {j.machine.name}: complete it before reopening this one.")
        j.wip_done, j.wip_lane = None, lane(j)
        j.save(update_fields=["wip_done", "wip_lane"])
        return f"MO {j.mo.number} is back under way."
    raise _No("Unknown action.")


def lane(j):
    """The key the database keeps unique: one MO under way per station machine and stage."""
    return f"{j.machine_id}:{j.op}"


@require_POST
def do(request, name):
    me, denied = _gate(request)
    if denied:
        return denied
    j = Job.objects.select_related("mo", "machine").filter(pk=request.POST.get("id") or 0, op__in=MID).first()
    back = "/stations/?" + urlencode({"line": j.machine.name if j else request.POST.get("line", "")})

    def go(msg="", err=""):
        return HttpResponseRedirect(back + "&" + urlencode({"msg": msg} if msg else {"err": err}))
    if not j:
        return go(err="That work is no longer on the schedule.")
    now = timezone.now()
    from hmi.engine import Ctx
    ctx = Ctx(user=me)
    stage = dict(STAGES).get(j.op, j.op)
    try:
        with transaction.atomic():
            # one station action at a time per machine: lock the machine, then read the job again under the lock
            Machine.objects.select_for_update().filter(pk=j.machine_id).first()
            j = Job.objects.select_for_update().select_related("mo", "machine").get(pk=j.pk)
            msg = _act(j, name, request.POST, me, now, stage)
    except _No as e:
        return go(err=str(e))
    except IntegrityError:   # the database's one-under-way-per-machine rule (two screens at the same instant)
        return go(err=f"Another MO was just started on {j.machine.name}: complete it first.")
    audit(ctx, f"Stations · {j.machine.name}: {msg}")
    event("stage", {"line": j.machine.name, "op": j.op, "mo": j.mo.number, "qty": j.wip_qty, "done": bool(j.wip_done), "by": me.name})
    from plant.models import PlantSettings
    PlantSettings.bump()
    refresh_floor_queue(by="Stations")
    return go(msg=msg)
