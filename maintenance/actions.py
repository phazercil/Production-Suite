"""Maintenance commands: a request's status (Maintenance › Requests) and the inventory (Maintenance › Inventory).
Registered with the engine, so they are checked, logged and rolled back like every other command."""
from hmi.engine import audit, cmd, config, event, need, notify
from hmi.models import Ticket, TicketNote
from plant.models import Machine

from .models import InventoryItem, InventoryMove

STATUS = dict(Ticket.STATUSES)


def machines():
    """The machines a request or a part can be for: the lines, then the other equipment (Admin › Maintenance lists)."""
    names = list(Machine.objects.filter(active=True).order_by("sort", "id").values_list("name", flat=True))
    return list(dict.fromkeys(names + [str(x) for x in (config().get("equipment") or []) if str(x).strip()]))


def _num(v, what, allow_zero=False):
    try:
        n = float(str(v).strip())
    except (TypeError, ValueError):
        need(False, f"Type the {what} as a number.")
    need(n == n and abs(n) < 1e7, f"Type the {what} as a number.")
    need(n > 0 or (allow_zero and n == 0), f"The {what} must be more than 0." if not allow_zero else f"The {what} can't be below 0.")
    return round(n, 3)


def _fmt(n):
    return f"{n:g}"


def _qty(n, it):
    """2 × Drive belt, or 3.5 ft of Air hose"""
    return f"{_fmt(n)} × {it}" if it.unit in ("", "each", "ea", "pc", "pcs") else f"{_fmt(n)} {it.unit} of {it}"


@cmd("ticketSet", "tickets.work")
def ticket_set(p, ctx):
    """Set a request's status: Open, Reviewed, In progress, Waiting on parts or Completed (with a note on why)."""
    t = Ticket.objects.select_for_update().filter(pk=p.get("id") or 0).first()
    need(t, "That request is no longer on file.")
    st = str(p.get("status") or "")
    need(st in STATUS, "Pick the status.")
    note = str(p.get("note") or "").strip()
    need(len(note) <= 4000, "Keep the note under 4,000 characters.")
    need(st != t.status or note, f"It is already {STATUS[st]}.")
    old = t.status
    if st != old:
        t.status = st
        t.updated, t.updated_by = ctx.now, ctx.by
        t.save(update_fields=["status", "updated", "updated_by"])
    kind = "closed" if st == "Closed" and old != "Closed" else "started" if st == "In progress" and old != st else "note"
    text = (f"{STATUS[old]} → {STATUS[st]}" if st != old else "") + (": " if st != old and note else "") + note
    TicketNote.objects.create(ticket=t, t=ctx.now, by=ctx.by, kind=kind, text=text)
    if kind == "closed" and t.pm_id:   # planned maintenance done: its count starts again
        from hmi import pm
        pm.completed(t, ctx.now, ctx.by)
    event("maintenance_status", {"id": t.id, "status": t.status})
    if st != old:
        notify(f"Maintenance on {t.machine} · {t.type}: {STATUS[st]} ({ctx.by})" + (f": {note}" if note else ""), "maint",
               lines=[t.line or t.machine, t.machine], go="maintenance")
    audit(ctx, f"Maintenance request #{t.id} ({t.machine} · {t.type}): {text}")
    return {"msg": f"Request on {t.machine}: {STATUS[t.status]}." if st != old else "Note added."}


@cmd("itemSave", "inventory.edit")
def item_save(p, ctx):
    """Add an inventory item, or change one (its stock changes through Add stock and Count, so they are logged)."""
    pk = str(p.get("id") or "")
    it = InventoryItem.objects.select_for_update().filter(pk=pk).first() if pk.isdigit() else None
    need(it or not pk, "That item is no longer on file.")
    name = str(p.get("name") or "").strip()
    need(name, "Type the item's name.")
    need(len(name) <= 120, "Keep the name under 120 characters.")
    part_no = str(p.get("part_no") or "").strip()[:60]
    if part_no:
        dup = InventoryItem.objects.filter(part_no__iexact=part_no, active=True).exclude(pk=it.pk if it else None).first()
        need(not dup, f"{dup} already has part # {part_no}." if dup else "")
    mn = str(p.get("min_qty") or "").strip()
    new = it is None
    it = it or InventoryItem()
    it.name, it.part_no = name, part_no
    it.location = str(p.get("location") or "").strip()[:80]
    it.unit = str(p.get("unit") or "").strip()[:20] or "each"
    it.min_qty = _num(mn, "reorder level", allow_zero=True) if mn else None
    it.notes = str(p.get("notes") or "").strip()[:2000]
    it.updated, it.updated_by = ctx.now, ctx.by
    if new:   # an item's stock only changes through Add stock, Count and Use (so every change is logged)
        start = str(p.get("qty") or "").strip()
        it.qty = _num(start, "quantity on hand", allow_zero=True) if start else 0
    it.save()
    if new and it.qty:
        InventoryMove.objects.create(item=it, kind="count", qty=it.qty, note="Starting stock", t=ctx.now, by=ctx.by)
    audit(ctx, f"Inventory: {'added' if new else 'changed'} {it}")
    return {"msg": f"{it.name} {'added' if new else 'saved'}.", "data": {"id": it.pk}}


@cmd("itemArchive", "inventory.edit")
def item_archive(p, ctx):
    it = InventoryItem.objects.filter(pk=p.get("id") or 0).first()
    need(it, "That item is no longer on file.")
    it.active = p.get("active") in ("1", 1, True, "true")
    it.updated, it.updated_by = ctx.now, ctx.by
    it.save(update_fields=["active", "updated", "updated_by"])
    audit(ctx, f"Inventory: {'brought back' if it.active else 'put away'} {it}")
    return {"msg": f"{it.name} {'is back on the list' if it.active else 'is off the list (its history is kept)'}."}


@cmd("itemStock", "inventory.edit")
def item_stock(p, ctx):
    """Add stock (received), or Count (set what is on the shelf; the difference is logged)."""
    it = InventoryItem.objects.select_for_update().filter(pk=p.get("id") or 0).first()
    need(it, "That item is no longer on file.")
    kind = p.get("kind")
    need(kind in ("added", "count"), "Add stock or count it.")
    note = str(p.get("note") or "").strip()[:200]
    if kind == "added":
        n = _num(p.get("qty"), "quantity received")
        it.qty = round(it.qty + n, 3)
        msg = f"Added {_qty(n, it)}: {_fmt(it.qty)} on hand."
    else:
        n = _num(p.get("qty"), "count", allow_zero=True)
        diff = round(n - it.qty, 3)
        need(diff or note, f"{it.name} already shows {_fmt(n)} on hand.")
        it.qty, n = n, diff
        msg = f"{it.name} counted: {_fmt(it.qty)} on hand ({'+' if diff >= 0 else ''}{_fmt(diff)})."
    it.updated, it.updated_by = ctx.now, ctx.by
    it.save(update_fields=["qty", "updated", "updated_by"])
    InventoryMove.objects.create(item=it, kind=kind, qty=n, note=note, t=ctx.now, by=ctx.by)
    audit(ctx, f"Inventory: {msg}" + (f" ({note})" if note else ""))
    return {"msg": msg}


@cmd("itemUse", "tickets.work")
def item_use(p, ctx):
    """Parts used on a machine: taken off the stock and linked to the machine (and the request, when there is one)."""
    it = InventoryItem.objects.select_for_update().filter(pk=p.get("item") or 0, active=True).first()
    need(it, "Pick the item used.")
    n = _num(p.get("qty"), "quantity used")
    t = None
    if str(p.get("ticket") or "").isdigit():
        t = Ticket.objects.filter(pk=p.get("ticket")).first()
        need(t, "That request is no longer on file.")
    machine = str(p.get("machine") or (t.machine if t else "")).strip()
    need(machine, "Pick the machine it was used on.")
    need(machine in machines() or (t and machine == t.machine) or InventoryMove.objects.filter(machine=machine).exists(),
         "Pick the machine from the list.")
    note = str(p.get("note") or "").strip()[:200]
    it.qty = round(it.qty - n, 3)
    it.updated, it.updated_by = ctx.now, ctx.by
    it.save(update_fields=["qty", "updated", "updated_by"])
    InventoryMove.objects.create(item=it, kind="used", qty=n, machine=machine, ticket=t, note=note, t=ctx.now, by=ctx.by)
    if t:
        TicketNote.objects.create(ticket=t, t=ctx.now, by=ctx.by, kind="note",
                                  text=f"Used {_qty(n, it)}" + (f": {note}" if note else ""))
    audit(ctx, f"Inventory: {_qty(n, it)} used on {machine}" + (f" (request #{t.id})" if t else ""))
    warn = " That's more than was on hand: count it in Inventory." if it.qty < 0 else (
        f" {_fmt(it.qty)} left: time to reorder." if it.low else "")
    return {"msg": f"Used {_qty(n, it)} on {machine}.{warn}"}


@cmd("itemUseUndo", "tickets.work")
def item_use_undo(p, ctx):
    """Take back a use recorded by mistake: the stock goes back up and the request gets a note."""
    mv = InventoryMove.objects.select_related("item", "ticket").filter(pk=p.get("id") or 0, kind="used").first()
    need(mv, "That entry is no longer on file.")
    it = InventoryItem.objects.select_for_update().get(pk=mv.item_id)
    it.qty = round(it.qty + mv.qty, 3)
    it.updated, it.updated_by = ctx.now, ctx.by
    it.save(update_fields=["qty", "updated", "updated_by"])
    if mv.ticket:
        TicketNote.objects.create(ticket=mv.ticket, t=ctx.now, by=ctx.by, kind="note", text=f"Taken back: {_qty(mv.qty, it)}")
    audit(ctx, f"Inventory: took back {_qty(mv.qty, it)} used on {mv.machine}")
    mv.delete()
    return {"msg": f"Taken back: {_qty(mv.qty, it)} ({_fmt(it.qty)} on hand)."}
