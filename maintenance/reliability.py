"""Maintenance › Reliability: how the machines hold up and whether maintenance keeps up.

- MTBF (mean time between failures): the machine's running time in the period ÷ the repair requests raised on it
  (requests not opened by the PM schedule). Running time is the minutes its readings say it ran.
- MTTR (mean time to repair): from when a repair request was raised to when it was completed, on average.
- Repeat failures: the same kind of request on the same machine 3 or more times in the period.
- PM compliance: of the planned maintenance done in the period (day-based tasks), how much was done on time.
- Coming up: planned maintenance falling due in the next 7, 14 and 30 days (the PM schedule's estimates).
- Parts: what was used on each machine in the period, and the parts used most.
"""
from collections import defaultdict
from datetime import timedelta

from django.db.models import Count, Sum

from hmi import pm
from hmi.models import PMTask, PpmReading, Ticket

from .models import InventoryMove

REPEAT = 3



def done(t):
    """When a request was completed: closed_at (set once, at completion). Requests closed before it existed were
    given it from their completion note (migration hmi 00xx_ticket_closed_at)."""
    return t.closed_at or (t.updated if t.status == "Closed" else None)

def build(now, days=90, machine=""):
    a = now - timedelta(days=days)
    reps = Ticket.objects.filter(t__gte=a, pm__isnull=True)
    if machine:
        reps = reps.filter(machine=machine)
    run = {r["line"]: r["n"] for r in PpmReading.objects.filter(t__gte=a, run=True).values("line").annotate(n=Count("id"))}
    by = defaultdict(list)
    for t in reps:
        by[t.machine].append(t)
    rows = []
    for m, ts in sorted(by.items()):
        fixed = [(done(t) - t.t).total_seconds() / 3600 for t in ts if t.status == "Closed" and done(t)]
        mins = run.get(m)
        rows.append({"machine": m, "failures": len(ts), "open": sum(1 for t in ts if t.status != "Closed"),
                     "runH": round(mins / 60, 1) if mins else None,
                     "mtbf": round(mins / 60 / len(ts), 1) if mins and ts else None,
                     "mttr": round(sum(fixed) / len(fixed), 1) if fixed else None,
                     "high": sum(1 for t in ts if t.priority == "High"), "locked": sum(1 for t in ts if t.lockout_t)})
    kinds = defaultdict(list)
    for t in reps:
        kinds[(t.machine, t.type)].append(t)
    repeats = sorted(({"machine": m, "type": k, "n": len(v), "last": max(x.t for x in v)} for (m, k), v in kinds.items() if len(v) >= REPEAT),
                     key=lambda x: -x["n"])
    on_time = late = 0
    for x in pm.history(since=a):
        task = x.pm
        if not task or task.unit != "days" or (machine and x.machine != machine):
            continue
        if not done(x):
            continue
        prev = Ticket.objects.filter(pm=task, status="Closed", closed_at__lt=done(x)).order_by("-closed_at").first()
        start = done(prev) if prev else task.created
        if (done(x) - start).total_seconds() / 86400 - task.every > 0.5:
            late += 1
        else:
            on_time += 1
    coming = {7: [], 14: [], 30: []}
    tasks = PMTask.objects.filter(active=True)
    if machine:
        tasks = tasks.filter(machine=machine)
    for task in tasks:
        st = pm.status(task, now)
        when = now if st["state"] in ("due", "open") else st["eta"]
        if when is None:
            continue
        for d in (7, 14, 30):
            if when <= now + timedelta(days=d):
                coming[d].append({"task": task, "when": when, "state": st["state"]})
                break
    used = InventoryMove.objects.filter(kind="used", t__gte=a)
    if machine:
        used = used.filter(machine=machine)
    parts = list(used.values("item__name", "item__part_no", "item__unit").annotate(n=Sum("qty"), times=Count("id")).order_by("-n")[:15])
    per_machine = list(used.values("machine").annotate(n=Count("id"), q=Sum("qty")).order_by("-n")[:20])
    total = on_time + late
    return {"rows": rows, "repeats": repeats, "pm": {"onTime": on_time, "late": late, "pct": round(100 * on_time / total) if total else None},
            "coming": coming, "comingN": sum(len(v) for v in coming.values()), "parts": parts, "perMachine": per_machine,
            "failures": sum(r["failures"] for r in rows), "repeatAt": REPEAT}
