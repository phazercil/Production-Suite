"""Maintenance as a work-order system (CMMS): assets and their history, work orders (assigned, due, hours, labor,
cost, failure code / cause / remedy, checklist), meters, calibration, spare-part reservations, maintenance windows
the Scheduler plans around, and corrective actions for failures that keep coming back.

Commands are registered with the engine (checked, logged and rolled back like every other); the read side
(costs, downtime, workload, suggestions, by-asset figures) is used by maintenance/views.py.
"""
from datetime import datetime, time as dtime, timedelta

from django.db.models import Q
from django.utils import timezone

from hmi.engine import audit, cmd, event, need, notify, rules
from hmi.models import Downtime, Ticket, TicketNote
from plant.models import Machine
from plant.util import local

from .actions import _num, machines
from .models import (Asset, AssetEvent, Calibration, InventoryItem, InventoryMove, MaintWindow, Meter, MeterReading, Reservation,
                     TicketLabor)

CA_COUNT = 3     # the same failure this many times …
CA_DAYS = 90     # … in this many days on one machine: suggest a corrective action


def _ticket(p, lock=True):
    qs = Ticket.objects.select_for_update() if lock else Ticket.objects
    t = qs.filter(pk=p.get("id") or p.get("ticket") or 0).first()
    need(t, "That request is no longer on file.")
    return t


def _date(v, what):
    s = str(v or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        need(False, f"{what}: pick a date.")


def _aware(v, what):
    s = str(v or "").strip()
    need(s, f"{what}: pick a date and time.")
    try:
        d = datetime.strptime(s[:16], "%Y-%m-%dT%H:%M")
    except ValueError:
        need(False, f"{what}: pick a date and time.")
    return timezone.make_aware(d)


def failure_key(t):
    """What a failure is, for spotting repeats: its failure code, else the request's issue type."""
    return (t.failure or t.type or "").strip()


# ------------------------------------------------------------------ work orders
@cmd("woSet", "maint.plan")
def wo_set(p, ctx):
    """A request's work-order details: who it's assigned to, when it's due, estimated hours, the asset, failure
    code / cause / remedy and outside costs. Only the fields sent are changed."""
    t = _ticket(p)
    changed = []
    if "assigned" in p:
        who = str(p.get("assigned") or "").strip()[:120]
        if who != t.assigned:
            t.assigned = who
            changed.append(f"assigned to {who}" if who else "unassigned")
            if who:
                notify(f"Maintenance request #{t.id} ({t.machine} · {t.type}) is assigned to {who}", "maint", lines=[t.machine], go="maintenance")
    if "due" in p:
        t.due = _date(p.get("due"), "Due")
        changed.append(f"due {t.due:%b %-d}" if t.due else "no due date")
    if "est_hours" in p:
        v = str(p.get("est_hours") or "").strip()
        t.est_hours = _num(v, "estimated hours") if v else None
    if "asset" in p:
        a = str(p.get("asset") or "")
        t.asset = Asset.objects.filter(pk=a).first() if a.isdigit() else None
        need(not a or t.asset, "That asset is no longer on file.")
    for f in ("failure", "cause", "remedy"):
        if f in p:
            setattr(t, f, str(p.get(f) or "").strip()[:80])
    if "outside_cost" in p:
        v = str(p.get("outside_cost") or "").strip()
        t.outside_cost = _num(v, "outside cost", allow_zero=True) if v else None
    t.updated, t.updated_by = ctx.now, ctx.by
    t.save()
    if changed:
        TicketNote.objects.create(ticket=t, t=ctx.now, by=ctx.by, kind="note", text="Work order: " + ", ".join(changed))
    audit(ctx, f"Maintenance request #{t.id}: work order saved" + (f" ({', '.join(changed)})" if changed else ""))
    return {"msg": "Work order saved."}


@cmd("woCheck", "tickets.work")
def wo_check(p, ctx):
    """Tick (or untick) a checklist step on a request."""
    t = _ticket(p)
    try:
        i = int(p.get("i"))
    except (TypeError, ValueError):
        i = -1
    cl = list(t.checklist or [])
    need(0 <= i < len(cl), "That step is no longer on the checklist.")
    done = str(p.get("done") or "") in ("1", "true", "on")
    cl[i] = {**cl[i], "done": done, "by": ctx.by if done else "", "t": ctx.now.isoformat() if done else ""}
    t.checklist = cl
    t.save(update_fields=["checklist"])
    return {"msg": ("Done: " if done else "Not done: ") + cl[i]["text"]}


@cmd("laborAdd", "tickets.work")
def labor_add(p, ctx):
    t = _ticket(p, lock=False)
    who = str(p.get("who") or ctx.by).strip()[:120]
    need(who, "Who did the work?")
    h = _num(p.get("hours"), "hours")
    need(h <= 24, "Hours: 24 at most for one entry.")
    day = _date(p.get("day"), "Day") or local(ctx.now).date()
    need(day <= local(ctx.now).date(), "The day can't be in the future.")
    TicketLabor.objects.create(ticket=t, who=who, hours=h, day=day, note=str(p.get("note") or "").strip()[:200], t=ctx.now, by=ctx.by)
    audit(ctx, f"Maintenance request #{t.id}: {h:g} h by {who} on {day}")
    return {"msg": f"{h:g} h by {who} recorded."}


@cmd("laborDelete", "tickets.work")
def labor_delete(p, ctx):
    x = TicketLabor.objects.filter(pk=p.get("id") or 0).first()
    need(x, "That entry is no longer on file.")
    audit(ctx, f"Maintenance request #{x.ticket_id}: removed {x.hours:g} h by {x.who} on {x.day}")
    x.delete()
    return {"msg": "Hours removed."}


# ------------------------------------------------------------------ reservations
@cmd("reserve", "tickets.work")
def reserve(p, ctx):
    """Set parts aside for a request: what's free (on hand less reserved) goes down; on hand doesn't, until used."""
    t = _ticket(p, lock=False)
    need(t.status != "Closed", "That request is completed.")
    it = InventoryItem.objects.select_for_update().filter(pk=p.get("item") or 0, active=True).first()
    need(it, "Pick the item.")
    n = _num(p.get("qty"), "quantity")
    free = it.qty - sum(r.qty for r in Reservation.objects.filter(item=it, status="reserved"))
    need(n <= free + 1e-9, f"Only {max(free, 0):g} {it.unit} of {it.name} free ({it.qty:g} on hand, the rest reserved).")
    Reservation.objects.create(item=it, ticket=t, qty=n, t=ctx.now, by=ctx.by)
    TicketNote.objects.create(ticket=t, t=ctx.now, by=ctx.by, kind="note", text=f"Reserved {n:g} × {it.name}")
    audit(ctx, f"Inventory: reserved {n:g} × {it} for request #{t.id}")
    return {"msg": f"{n:g} × {it.name} reserved."}


@cmd("reserveRelease", "tickets.work")
def reserve_release(p, ctx):
    r = Reservation.objects.select_for_update().filter(pk=p.get("id") or 0, status="reserved").first()
    need(r, "That reservation is no longer open.")
    r.status = "released"
    r.save(update_fields=["status"])
    audit(ctx, f"Inventory: released {r.qty:g} × {r.item} (request #{r.ticket_id})")
    return {"msg": "Reservation released."}


def reserve_pm_parts(ticket, task, now, by):
    """A PM request opened: its parts list is reserved for it (as much as is free)."""
    from .models import PMPart
    for b in PMPart.objects.filter(task=task).select_related("item"):
        it = b.item
        free = it.qty - sum(r.qty for r in Reservation.objects.filter(item=it, status="reserved"))
        n = min(b.qty, max(free, 0))
        if n > 0:
            Reservation.objects.create(item=it, ticket=ticket, qty=n, t=now, by=str(by)[:120])


def used_reserved(ticket, item, n):
    """Parts used on a request: its reservation of that item is used up by as much."""
    for r in Reservation.objects.select_for_update().filter(ticket=ticket, item=item, status="reserved").order_by("t"):
        if n <= 0:
            break
        if r.qty <= n + 1e-9:
            n -= r.qty
            r.status = "used"
            r.save(update_fields=["status"])
        else:
            Reservation.objects.filter(pk=r.pk).update(qty=round(r.qty - n, 3))
            Reservation.objects.create(item=item, ticket=ticket, qty=n, status="used", t=r.t, by=r.by)
            n = 0


def on_completed(t, ctx):
    """A request was completed: what's still reserved for it goes back to free; its asset's history notes it."""
    Reservation.objects.filter(ticket=t, status="reserved").update(status="released")
    if t.asset_id:
        AssetEvent.objects.create(asset_id=t.asset_id, kind="note", ticket=t, t=ctx.now, by=ctx.by,
                                  text=f"Request #{t.id} completed: {t.type}" + (f" · {t.failure}" if t.failure else "")
                                  + (f" · cause: {t.cause}" if t.cause else "") + (f" · fix: {t.remedy}" if t.remedy else ""))


def completion_problem(t):
    """Why a request can't be completed yet (None: it can)."""
    left = [c["text"] for c in (t.checklist or []) if not c.get("done")]
    if left:
        return f"Tick off the checklist first ({len(left)} step{'s' if len(left) > 1 else ''} left: {left[0]}{'…' if len(left) > 1 else ''})."
    if not t.pm_id and not t.corrective and int(rules().get("failRequired", 0) or 0) and not t.failure:
        return "Pick the failure code (what failed) before completing it."
    return None


# ------------------------------------------------------------------ assets, meters, calibration
@cmd("assetSave", "assets.edit")
def asset_save(p, ctx):
    pk = str(p.get("id") or "")
    a = Asset.objects.filter(pk=pk).first() if pk.isdigit() else None
    need(a or not pk, "That asset is no longer on file.")
    name = str(p.get("name") or "").strip()[:120]
    need(name, "Name the asset.")
    new = a is None
    a = a or Asset()
    parent = str(p.get("parent") or "")
    a.parent = Asset.objects.filter(pk=parent).first() if parent.isdigit() else None
    if a.parent:
        x, seen = a.parent, set()
        while x:   # not under itself
            need(not a.pk or x.pk != a.pk, "An asset can't be under itself.")
            if x.pk in seen:
                break
            seen.add(x.pk)
            x = x.parent
    machine = str(p.get("machine") or (a.parent.machine if a.parent else "")).strip()[:60]
    need(machine, "Pick the machine or equipment it belongs to.")
    need(machine in machines() or machine == a.machine, "Pick the machine or equipment from the list.")
    a.name, a.machine = name, machine
    a.kind = p.get("kind") if p.get("kind") in dict(Asset.KINDS) else "component"
    for f, n in (("tag", 40), ("make", 80), ("model", 80), ("serial", 80), ("cal_spec", 160)):
        setattr(a, f, str(p.get(f) or "").strip()[:n])
    a.notes = str(p.get("notes") or "").strip()[:4000]
    a.installed = _date(p.get("installed"), "Installed")
    ce = str(p.get("cal_every") or "").strip()
    a.cal_every = int(_num(ce, "calibration interval (days)")) if ce else None
    if "active" in p:
        a.active = str(p.get("active")) in ("1", "true", "on")
    a.updated, a.updated_by = ctx.now, ctx.by
    a.save()
    if new and a.installed:
        AssetEvent.objects.create(asset=a, kind="installed", text="Added to the asset list", t=ctx.now, by=ctx.by)
    audit(ctx, f"Assets: {'added' if new else 'changed'} {a.path()} ({a.machine})")
    return {"msg": f"{a.name} {'added' if new else 'saved'}.", "data": {"id": a.pk}}


@cmd("assetEvent", "tickets.work")
def asset_event(p, ctx):
    a = Asset.objects.filter(pk=p.get("asset") or 0).first()
    need(a, "That asset is no longer on file.")
    kind = p.get("kind") if p.get("kind") in dict(AssetEvent.KINDS) else "note"
    text = str(p.get("text") or "").strip()[:2000]
    need(text or kind != "note", "Say what happened.")
    tid = str(p.get("ticket") or "")
    AssetEvent.objects.create(asset=a, kind=kind, text=text, ticket=Ticket.objects.filter(pk=tid).first() if tid.isdigit() else None,
                              t=ctx.now, by=ctx.by)
    if kind == "replaced":
        a.installed = local(ctx.now).date()
        a.save(update_fields=["installed"])
    audit(ctx, f"Assets: {a.path()} · {dict(AssetEvent.KINDS)[kind]}" + (f": {text}" if text else ""))
    return {"msg": f"{a.name}: {dict(AssetEvent.KINDS)[kind].lower()} recorded."}


@cmd("calRecord", "assets.edit")
def cal_record(p, ctx):
    a = Asset.objects.select_for_update().filter(pk=p.get("asset") or 0).first()
    need(a, "That instrument is no longer on file.")
    res = p.get("result")
    need(res in dict(Calibration.RESULTS), "Pick the result: pass, adjusted or fail.")
    day = _date(p.get("day"), "Date") or local(ctx.now).date()
    need(day <= local(ctx.now).date(), "The date can't be in the future.")
    c = Calibration.objects.create(asset=a, day=day, result=res, t=ctx.now, by=ctx.by,
                                   **{f: str(p.get(f) or "").strip()[:200] for f in ("as_found", "as_left", "standard", "cert")},
                                   notes=str(p.get("notes") or "").strip()[:2000])
    if not a.cal_last or day >= a.cal_last:
        a.cal_last, a.cal_status = day, res
        if res == "fail":
            a.active = False   # out of use until it's repaired or replaced and passes again
        elif not a.active:
            a.active = True
        a.save(update_fields=["cal_last", "cal_status", "active"])
    AssetEvent.objects.create(asset=a, kind="note", t=ctx.now, by=ctx.by, text=f"Calibrated {day:%b %-d, %Y}: {dict(Calibration.RESULTS)[res]}"
                              + (f" · as found {c.as_found}" if c.as_found else "") + (f" · as left {c.as_left}" if c.as_left else ""))
    if res == "fail":
        notify(f"{a.name} ({a.machine}) failed calibration: it's out of use until it passes again", "maint", go="maintenance")
    audit(ctx, f"Calibration: {a.path()} {res} on {day}")
    return {"msg": f"{a.name}: calibration recorded ({dict(Calibration.RESULTS)[res]})."}


@cmd("meterSave", "assets.edit")
def meter_save(p, ctx):
    pk = str(p.get("id") or "")
    m = Meter.objects.filter(pk=pk).first() if pk.isdigit() else None
    need(m or not pk, "That meter is no longer on file.")
    name = str(p.get("name") or "").strip()[:80]
    need(name, "Name the meter (e.g. Knife cycles).")
    machine = str(p.get("machine") or "").strip()[:60]
    need(machine in machines() or (m and machine == m.machine), "Pick the machine or equipment.")
    src = p.get("source") if p.get("source") in dict(Meter.SOURCES) else "manual"
    field = str(p.get("field") or "").strip()[:80]
    need(src == "manual" or (field and Machine.objects.filter(name=machine).exists()), "A meter from the machine's data needs a machine and the field.")
    new = m is None
    m = m or Meter()
    m.name, m.machine, m.source, m.field = name, machine, src, field if src == "field" else ""
    m.unit = str(p.get("unit") or "").strip()[:20] or "cycles"
    a = str(p.get("asset") or "")
    m.asset = Asset.objects.filter(pk=a).first() if a.isdigit() else None
    if "active" in p:
        m.active = str(p.get("active")) in ("1", "true", "on")
    m.save()
    audit(ctx, f"Meters: {'added' if new else 'changed'} {m}")
    return {"msg": f"{m.name} {'added' if new else 'saved'}."}


@cmd("meterRead", "tickets.work")
def meter_read(p, ctx):
    m = Meter.objects.select_for_update().filter(pk=p.get("id") or 0, active=True).first()
    need(m, "That meter is no longer on file.")
    v = _num(p.get("value"), "reading", allow_zero=True)
    need(v >= m.value or str(p.get("reset") or "") in ("1", "on"),
         f"That's below the last reading ({m.value:g}). Tick 'Meter was reset or replaced' if it was.")
    if v < m.value:   # reset: PMs on it count from zero again
        from hmi.models import PMTask
        for task in PMTask.objects.filter(meter=m):
            used = max(0.0, m.value - (task.meter_at if task.meter_at is not None else m.value))
            PMTask.objects.filter(pk=task.pk).update(meter_at=v - used)
    m.value, m.read_at, m.read_by = v, ctx.now, ctx.by
    m.save(update_fields=["value", "read_at", "read_by"])
    MeterReading.objects.create(meter=m, value=v, t=ctx.now, by=ctx.by)
    return {"msg": f"{m.name}: {v:g} {m.unit}."}


def meters_from_sample(machine, sample, t):
    """The ticker: meters read from a machine's data, each minute."""
    for m in Meter.objects.filter(machine=machine.name, source="field", active=True).exclude(field=""):
        try:
            v = float((sample or {}).get(m.field))
        except (TypeError, ValueError):
            continue
        if v != m.value:
            Meter.objects.filter(pk=m.pk).update(value=v, read_at=t, read_by="machine data")
            if not m.read_at or (t - m.read_at) >= timedelta(hours=1):   # an hourly reading is kept, for the rate
                MeterReading.objects.create(meter=m, value=v, t=t, by="machine data")


def calibration_rows(today):
    out = []
    for a in Asset.objects.filter(cal_every__isnull=False).order_by("machine", "name"):
        due = a.cal_due
        st = "never" if not a.cal_last else "fail" if a.cal_status == "fail" else "due" if due and due <= today else \
            "soon" if due and (due - today).days <= 14 else "ok"
        out.append({"a": a, "due": due, "state": st, "last": a.calibrations.first()})
    order = {"fail": 0, "due": 1, "never": 2, "soon": 3, "ok": 4}
    return sorted(out, key=lambda r: (order[r["state"]], r["due"] or today))


# ------------------------------------------------------------------ maintenance windows
@cmd("windowSave", "maint.plan")
def window_save(p, ctx):
    m = Machine.objects.filter(name=str(p.get("machine") or "")).first()
    need(m, "Pick the machine.")
    a, b = _aware(p.get("start"), "Starts"), _aware(p.get("end"), "Ends")
    need(b > a, "It has to end after it starts.")
    need(b - a <= timedelta(days=14), "A window can be 14 days at most.")
    need(b > ctx.now, "That's already over.")
    tid = str(p.get("ticket") or "")
    t = Ticket.objects.filter(pk=tid).first() if tid.isdigit() else None
    w = MaintWindow.objects.create(machine=m, start=a, end=b, ticket=t, note=str(p.get("note") or "").strip()[:160], t=ctx.now, by=ctx.by)
    _replan(f"maintenance window on {m.name}")
    if t:
        TicketNote.objects.create(ticket=t, t=ctx.now, by=ctx.by, kind="note", text=f"Maintenance window {fmt(a)} – {fmt(b)}")
    notify(f"Maintenance window on {m.name}: {fmt(a)} – {fmt(b)}" + (f" ({w.note})" if w.note else "") + ". No production is planned then.",
           "planner", lines=[m.name])
    audit(ctx, f"Maintenance window on {m.name}: {fmt(a)} – {fmt(b)}" + (f" for request #{t.id}" if t else ""))
    return {"msg": f"Window on {m.name} planned: the Scheduler plans around it."}


@cmd("windowCancel", "maint.plan")
def window_cancel(p, ctx):
    w = MaintWindow.objects.filter(pk=p.get("id") or 0, cancelled=False).first()
    need(w, "That window is no longer planned.")
    w.cancelled = True
    w.save(update_fields=["cancelled"])
    _replan(f"maintenance window cancelled on {w.machine.name}")
    audit(ctx, f"Maintenance window on {w.machine.name} cancelled ({fmt(w.start)} – {fmt(w.end)})")
    return {"msg": "Window cancelled."}


def fmt(d):
    return local(d).strftime("%a %b %-d %H:%M")


def _replan(why):
    try:
        from plant.schedule import forget
        forget()
        from hmi.engine import refresh_schedule
        refresh_schedule(why)
    except Exception:  # noqa: BLE001 - the plan catches up on its next refresh
        pass


def windows(start, end):
    """Maintenance windows between two times, for plant.util.extra_breaks: [{s, e, label, machines: {id}}]."""
    out = []
    for w in MaintWindow.objects.filter(cancelled=False, start__lt=end, end__gt=start).select_related("ticket"):
        out.append({"id": f"mw{w.pk}", "s": w.start, "e": w.end, "machines": {w.machine_id}, "by": w.by,
                    "label": "Maintenance" + (f": {w.ticket.type}" if w.ticket else f": {w.note}" if w.note else "")})
    return out


# ------------------------------------------------------------------ corrective actions
def open_ca(machine, key):
    return Ticket.objects.filter(machine=machine, corrective=True).exclude(status="Closed").filter(Q(failure=key) | Q(type=f"Corrective action: {key}"[:80])).first()


def link_to_ca(t):
    """A new request for a failure that already has a corrective action open on that machine: attached to it (the
    corrective action is the fix; this is one more time it happened)."""
    if t.pm_id or t.corrective:
        return None
    ca = open_ca(t.machine, failure_key(t))
    if ca:
        Ticket.objects.filter(pk=t.pk).update(ca=ca)
        TicketNote.objects.create(ticket=ca, t=t.t, by=t.by, kind="note", text=f"Happened again: request #{t.id} ({t.by})")
        notify(f"{t.machine}: {failure_key(t)} again (request #{t.id}). Corrective action #{ca.id} is open for it.", "maint",
               lines=[t.machine], go="maintenance")
    return ca


def suggestions(now, machine=""):
    """Failures that keep coming back on a machine (CA_COUNT times in CA_DAYS days) with no corrective action open:
    each one is a suggestion to fix the cause, not repair it again."""
    r = rules()
    n_min, days = int(r.get("caCount", CA_COUNT) or CA_COUNT), int(r.get("caDays", CA_DAYS) or CA_DAYS)
    qs = Ticket.objects.filter(t__gte=now - timedelta(days=days), pm__isnull=True, corrective=False, ca__isnull=True)
    if machine:
        qs = qs.filter(machine=machine)
    groups = {}
    for t in qs.order_by("t"):
        k = failure_key(t)
        if k:
            groups.setdefault((t.machine, k), []).append(t)
    out = []
    for (m, k), ts in groups.items():
        if len(ts) >= n_min and not open_ca(m, k):
            out.append({"machine": m, "key": k, "n": len(ts), "days": days, "tickets": ts,
                        "down": round(sum(ticket_down(t, now) for t in ts)), "first": ts[0].t, "last": ts[-1].t})
    return sorted(out, key=lambda x: (-x["n"], -x["down"]))


@cmd("caOpen", "maint.plan")
def ca_open(p, ctx):
    """Open a corrective action for a repeating failure: one request to fix the cause, with the repeats attached."""
    machine, key = str(p.get("machine") or "")[:60], str(p.get("key") or "").strip()[:80]
    need(machine and key, "Pick the failure.")
    need(not open_ca(machine, key), f"A corrective action is already open for {key} on {machine}.")
    days = int(rules().get("caDays", CA_DAYS) or CA_DAYS)
    reps = [t for t in Ticket.objects.filter(machine=machine, t__gte=ctx.now - timedelta(days=days), pm__isnull=True, corrective=False)
            if failure_key(t) == key]
    ca = Ticket.objects.create(t=ctx.now, line=machine if Machine.objects.filter(name=machine).exists() else "", by=ctx.by, machine=machine,
                               type=f"Corrective action: {key}"[:80], priority="High", corrective=True, failure=key,
                               assigned=str(p.get("assigned") or "").strip()[:120],
                               notes=(str(p.get("notes") or "").strip() + "\n\n" if p.get("notes") else "")
                               + f"{key} happened {len(reps)} times on {machine} in {days} days: "
                               + ", ".join(f"#{t.id} ({local(t.t):%b %-d})" for t in reps)
                               + ". Find and fix the cause so it stops coming back.")
    Ticket.objects.filter(pk__in=[t.pk for t in reps]).update(ca=ca)
    event("maintenance", {"id": ca.id, "machine": machine, "type": ca.type, "priority": ca.priority, "corrective": True})
    notify(f"Corrective action opened on {machine}: {key} (happened {len(reps)} times)", "maint", lines=[machine], go="maintenance")
    audit(ctx, f"Corrective action #{ca.id} on {machine}: {key} ({len(reps)} repeats attached)")
    return {"msg": f"Corrective action #{ca.id} opened for {key} on {machine}.", "data": {"id": ca.id}}


# ------------------------------------------------------------------ figures
def ticket_down(t, now):
    """Minutes the machine was down while the request was open (its stops in that time, lockout included)."""
    end = t.closed_at or now
    mins = 0.0
    for d in Downtime.objects.filter(line=t.machine, start_time__lt=end, end_time__gt=t.t).only("start_time", "end_time"):
        mins += max(0.0, (min(d.end_time, end) - max(d.start_time, t.t)).total_seconds() / 60)
    return mins


def ticket_cost(t, labor=None, parts=None):
    """Parts (at their cost when used), labor (hours at the maintenance labor rate) and outside costs."""
    rate = float(rules().get("maintRate", 0) or 0)
    labor = list(t.labor.all()) if labor is None else labor
    parts = list(InventoryMove.objects.filter(ticket=t, kind="used").select_related("item")) if parts is None else parts
    hours = sum(x.hours for x in labor)
    p = sum(m.qty * (m.unit_cost if m.unit_cost is not None else (m.item.unit_cost or 0)) for m in parts)
    lab = hours * rate
    return {"hours": round(hours, 2), "parts": round(p, 2), "labor": round(lab, 2), "outside": round(t.outside_cost or 0, 2),
            "total": round(p + lab + (t.outside_cost or 0), 2), "rate": rate}


def workload(now):
    """Each technician's open work: requests, estimated hours, overdue; and hours logged in the last 30 days."""
    today = local(now).date()
    rows = {}
    for t in Ticket.objects.exclude(status="Closed"):
        w = rows.setdefault(t.assigned or "", {"who": t.assigned, "open": 0, "est": 0.0, "overdue": 0, "high": 0, "tickets": [], "logged": 0.0})
        w["open"] += 1
        w["est"] += t.est_hours or 0
        w["overdue"] += 1 if t.due and t.due < today else 0
        w["high"] += 1 if t.priority == "High" else 0
        w["tickets"].append(t)
    for x in TicketLabor.objects.filter(day__gte=today - timedelta(days=30)):
        w = rows.setdefault(x.who, {"who": x.who, "open": 0, "est": 0.0, "overdue": 0, "high": 0, "tickets": [], "logged": 0.0})
        w["logged"] += x.hours
    for w in rows.values():
        w["tickets"].sort(key=lambda t: (t.due or today + timedelta(days=9999), t.t))
    return sorted(rows.values(), key=lambda w: (w["who"] == "", -w["open"]))


def by_asset(now, days, machine=""):
    """Per machine (and asset): requests, downtime while they were open, labor hours, cost and the top failures."""
    a = now - timedelta(days=days)
    qs = Ticket.objects.filter(t__gte=a).select_related("asset").prefetch_related("labor")
    if machine:
        qs = qs.filter(machine=machine)
    used = {}
    for m in InventoryMove.objects.filter(kind="used", ticket__in=qs).select_related("item"):
        used.setdefault(m.ticket_id, []).append(m)
    out = {}
    for t in qs:
        key = (t.machine, t.asset.path() if t.asset else "")
        r = out.setdefault(key, {"machine": t.machine, "asset": key[1], "n": 0, "repairs": 0, "down": 0.0, "hours": 0.0, "cost": 0.0, "fails": {}})
        c = ticket_cost(t, list(t.labor.all()), used.get(t.id, []))
        r["n"] += 1
        r["repairs"] += 0 if t.pm_id else 1
        r["down"] += ticket_down(t, now)
        r["hours"] += c["hours"]
        r["cost"] += c["total"]
        k = failure_key(t)
        if k and not t.pm_id:
            r["fails"][k] = r["fails"].get(k, 0) + 1
    rows = list(out.values())
    for r in rows:
        r["down"] = round(r["down"])
        r["top"] = sorted(r["fails"].items(), key=lambda kv: -kv[1])[:3]
    return sorted(rows, key=lambda r: (-r["down"], -r["cost"]))


def asset_tree(machine=""):
    """The assets as a list in tree order, each with its depth."""
    qs = Asset.objects.all()
    if machine:
        qs = qs.filter(machine=machine)
    kids = {}
    for a in qs:
        kids.setdefault(a.parent_id, []).append(a)
    ids = {a.pk for a in qs}
    out = []

    def walk(pid, depth, seen):
        for a in sorted(kids.get(pid, []), key=lambda x: (x.machine, x.name)):
            if a.pk in seen:
                continue
            out.append({"a": a, "depth": depth})
            walk(a.pk, depth + 1, seen | {a.pk})
    walk(None, 0, set())
    for a in qs:   # under a parent on another machine (filtered out): shown at the top
        if a.parent_id and a.parent_id not in ids:
            out.append({"a": a, "depth": 0})
            walk(a.pk, 1, {a.pk})
    return out


def history(asset):
    """Everything that happened to an asset (and what's under it): requests, parts used, events, calibrations."""
    ids, todo = set(), [asset.pk]
    while todo:
        x = todo.pop()
        if x in ids:
            continue
        ids.add(x)
        todo += list(Asset.objects.filter(parent_id=x).values_list("pk", flat=True))
    rows = []
    for t in Ticket.objects.filter(asset_id__in=ids):
        rows.append({"t": t.t, "kind": "request", "text": f"#{t.id} {t.type}" + (f" · {t.failure}" if t.failure else "")
                     + f" · {t.status_label}", "by": t.by, "ticket": t})
    for m in InventoryMove.objects.filter(Q(asset_id__in=ids) | Q(ticket__asset_id__in=ids), kind="used").select_related("item"):
        rows.append({"t": m.t, "kind": "part", "text": f"Used {m.qty:g} × {m.item}", "by": m.by, "ticket": m.ticket})
    for e in AssetEvent.objects.filter(asset_id__in=ids).select_related("asset"):
        rows.append({"t": e.t, "kind": e.kind, "text": (f"{e.asset.name}: " if e.asset_id != asset.pk else "") + f"{e.get_kind_display()}"
                     + (f": {e.text}" if e.text else ""), "by": e.by, "ticket": e.ticket})
    return sorted(rows, key=lambda r: r["t"], reverse=True)[:300]


def start_of_day(d):
    return timezone.make_aware(datetime.combine(d, dtime(0)))
