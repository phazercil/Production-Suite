"""Planned (preventive) maintenance: Maintenance › PM schedule.

A PM task is a job on a machine (or other equipment) done every so many days, run hours or filters made:
"Grease the pleater chain every 80 run hours", "Change the glue filter every 25,000 filters", "Check the
compressor drain every 7 days". Run hours are the minutes the machine's own readings say it was running
(hmi_ppm.run); filters made are its counted units. Equipment that isn't a machine with readings can only go by days.

When a task falls due the server opens a maintenance request for it (type "PM · <task>"), so it shows on every
Maintenance page with the other requests, and the people ticked for it in Admin › Alerts are told. Completing that
request starts the count again from then. A task can also be logged done early (Maintenance › PM schedule), which
is kept the same way: a completed request for the task.
"""
from datetime import timedelta

from django.db.models import Count, Sum

from .models import PMTask, PpmReading, Ticket, TicketNote

UNITS = dict(PMTask.UNITS)
SOON = 0.85          # a task is "due soon" once 85% of its interval is used
RATE_DAYS = 14       # how far back to look for how fast a machine uses up run hours / filters


def is_machine(name):
    from plant.models import Machine
    return Machine.objects.filter(name=name).exists()


def start_of(task):
    """Where the count starts: when it was last done, or when it was added."""
    return task.last_done or task.created


def used(task, now, start=None):
    """How much of the interval is used since the task was last done: days, run hours or filters."""
    a = start or start_of(task)
    if task.unit == "days":
        return max(0.0, (now - a).total_seconds() / 86400)
    qs = PpmReading.objects.filter(line=task.machine, t__gte=a, t__lt=now)
    if task.unit == "hours":
        return (qs.filter(run=True).aggregate(n=Count("id"))["n"] or 0) / 60   # one reading per minute
    return float(qs.aggregate(n=Sum("units"))["n"] or 0)


def rate(task, now):
    """Run hours or filters a day, from the last two weeks (None for days, or with no readings)."""
    if task.unit == "days":
        return 1.0
    a = now - timedelta(days=RATE_DAYS)
    r = used(task, now, a) / RATE_DAYS
    return r or None


def status(task, now, open_ticket=None):
    """Everything a PM schedule row shows: used, left, % used, state and an estimate of when it falls due."""
    u = used(task, now)
    every = max(task.every or 0, 0.0001)
    left = every - u
    pct = min(999, round(100 * u / every))
    if open_ticket is not None:
        state = "open"
    elif not task.active:
        state = "off"
    elif left <= 0:
        state = "due"
    elif u >= SOON * every:
        state = "soon"
    else:
        state = "ok"
    eta = None
    if task.active and left > 0:
        r = rate(task, now)
        if r:
            eta = now + timedelta(days=left / r)
    return {"used": u, "left": left, "pct": pct, "state": state, "eta": eta, "ticket": open_ticket}


def fmt_amount(v, unit):
    if unit == "days":
        return f"{v:.1f} days" if abs(v) < 10 and v != int(v) else f"{round(v)} days"
    if unit == "hours":
        return f"{v:.1f} run h" if abs(v) < 10 else f"{round(v):,} run h"
    return f"{round(v):,} filters"


def open_ticket(task):
    return task.tickets.exclude(status="Closed").order_by("-t").first()


def open_request(task, now, by="PM schedule"):
    """Open the maintenance request for a task that has fallen due."""
    from .engine import event, notify
    t = Ticket.objects.create(t=now, line=task.machine if is_machine(task.machine) else "", by=by, machine=task.machine,
                              type=f"PM · {task.title}"[:80], priority=task.priority, pm=task,
                              notes=(f"Planned maintenance, every {fmt_amount(task.every, task.unit)}.\n" + (task.steps or "")).strip())
    event("maintenance", {"id": t.id, "machine": t.machine, "type": t.type, "priority": t.priority, "pm": task.id})
    notify(f"Planned maintenance due on {task.machine}: {task.title}", "maint", lines=[task.machine], go="maintenance")
    return t


def check(now):
    """Called by the ticker: open a request for every active task that has fallen due and has none open.
    Returns the requests opened."""
    opened = []
    busy = set(Ticket.objects.filter(pm__isnull=False).exclude(status="Closed").values_list("pm_id", flat=True))
    for task in PMTask.objects.filter(active=True).exclude(pk__in=busy):
        if used(task, now) >= task.every:
            opened.append(open_request(task, now))
    return opened


def completed(ticket, now, by):
    """A PM request was completed: the task's count starts again."""
    if ticket.pm_id:
        PMTask.objects.filter(pk=ticket.pm_id).update(last_done=now, last_by=str(by)[:120])


def log_done(task, now, by, note=""):
    """Done without waiting for it to fall due (or its open request completed from here): kept as a completed request."""
    t = open_ticket(task)
    if not t:
        t = Ticket.objects.create(t=now, line=task.machine if is_machine(task.machine) else "", by=by, machine=task.machine,
                                  type=f"PM · {task.title}"[:80], priority=task.priority, pm=task, notes=task.steps or "")
    t.status, t.updated, t.updated_by = "Closed", now, by
    t.save(update_fields=["status", "updated", "updated_by"])
    TicketNote.objects.create(ticket=t, t=now, by=by, kind="closed", text=note or "Completed")
    completed(t, now, by)
    return t


def history(task=None, since=None, limit=300):
    """Completed PM requests, newest first, with whether each was done on time."""
    qs = Ticket.objects.filter(pm__isnull=False, status="Closed").select_related("pm")
    if task:
        qs = qs.filter(pm=task)
    if since:
        qs = qs.filter(updated__gte=since)
    return list(qs.order_by("-updated")[:limit])
