"""S2K's MOs for one order, as the Scheduler sees them.

S2K keeps one MO per stage of an order, with consecutive numbers and the same quantity (or twice it when two stage
units make one finished part):

    312151-030  1MG4107HP  Stage 1, pleating        5000
    312152-030  2MG4107HP  Stage 2, end bands        5000
    312153-030  MG4107HP   packaging, the finished   5000

and gives a number a new revision each time units are posted (306556 → 306556-001 → …), with the quantity still to
make. Here:

* a revision updates the MO already on file (matched on the number without its revision): the revision, what S2K
  says is still to make and the due date. Its quantity, made count and schedule stay;
* the stage MOs of one order are merged into one order MO, the one the Scheduler shows: the finished part's MO when
  there is one. The others hang off it (MO.parent) and keep their own S2K number for the floor to post against. The
  order's stages are the stage MOs it has, and each stage makes what its own MO says (MO.stage_qty).
"""
import re

from django.db import transaction

from .models import MO, part_key
from .util import Catalog, default_ops

NUM = re.compile(r"(\d+)-(\d{1,4})")
PREFIX = {"1": "s1", "2": "eb"}
FAMILY = ("s1", "eb", "pk")
GAP = 3   # the stage MOs of one order have consecutive numbers


def split_number(number):
    """("306556", 5) for 306556-005; (number, 0) for one without a revision."""
    n = str(number or "").strip()
    m = NUM.fullmatch(n)
    return (m.group(1), int(m.group(2))) if m else (n, 0)


def stage_of(part):
    """The stage an S2K MO is for, from its part number: 1MG4062 Stage 1, 2MG4062 end bands, MG4062 packaging."""
    p = str(part or "").strip().upper()
    m = re.match(r"^(\d)(?=[A-Z])", p)
    if not m:
        return "pk"
    return PREFIX.get(m.group(1), "")


def _members(order):
    return [order, *order.stage_mos.all()]


def refresh_order(order, cat=None):
    """An order's stages and quantities from its stage MOs. Arts & Crafts and Glue Bead still come from the cost
    sheet (S2K has no MO for them). The order's own quantity is what Stage 1 makes (the pleater counts to it)."""
    members = _members(order)
    stages = {m.stage: m for m in members if m.stage in FAMILY}
    if not stages:
        return
    cat = cat or Catalog()
    item = (cat.matches(order.part) or [None])[0]
    cs = default_ops(item)
    own = (order.stage_qty or {}).get(order.stage, order.qty)   # the order's own MO quantity (its qty becomes Stage 1's)
    order.stage_qty = {st: (own if m.pk == order.pk else m.qty) for st, m in stages.items()}
    order.ops = {"s1": "s1" in stages, "eb": "eb" in stages, "ac": bool(cs.get("ac")) and "s1" in stages,
                 "gb": bool(cs.get("gb")) and "s1" in stages, "pk": "pk" in stages}
    order.qty = stages["s1"].qty if "s1" in stages else order.stage_qty[order.stage] if order.stage in order.stage_qty else order.qty
    order.save(update_fields=["stage_qty", "ops", "qty"])


def _movable(order):
    """An order can hand over to its finished-part MO while nothing has been made or started on it."""
    from hmi.models import ShiftRun
    return order.produced == 0 and order.status == "OPEN" and not ShiftRun.objects.filter(active=True, mo=order).exists()


def _take_over(new, old):
    """The finished part's MO becomes the order: the board and the line queue move to it."""
    from scheduler.models import Job
    Job.objects.filter(mo=old).update(mo=new)
    for f in ("line", "seq", "plan_start", "plan_end", "plan_co", "item_uid", "pleats"):
        setattr(new, f, getattr(old, f))
    new.parent = None
    new.save()
    MO.objects.filter(parent=old).update(parent=new)
    old.qty = (old.stage_qty or {}).get(old.stage, old.qty)   # back to its own MO quantity
    old.parent, old.line, old.seq, old.plan_start, old.plan_end, old.stage_qty = new, None, 0, None, None, {}
    old.save(update_fields=["qty", "parent", "line", "seq", "plan_start", "plan_end", "stage_qty"])


def group(mo, cat=None):
    """Put a new stage MO with the rest of its order (the open stage MOs of the same part numbered next to it).
    Returns the order MO."""
    if mo.stage not in FAMILY or not mo.base.isdigit():
        return mo
    key, n = part_key(mo.part), int(mo.base)
    order = None
    for c in MO.objects.filter(status="OPEN", stage__in=FAMILY).exclude(pk=mo.pk).select_related("parent"):
        if not c.base.isdigit() or abs(int(c.base) - n) > GAP or part_key(c.part) != key:
            continue
        o = c.parent or c
        if any(x.stage == mo.stage for x in _members(o)):
            continue   # that order already has this stage: another order of the same part
        order = o
        break
    if not order:
        refresh_order(mo, cat)
        return mo
    if mo.stage == "pk" and order.stage != "pk" and _movable(order):
        _take_over(mo, order)
        order = mo
    else:
        mo.parent = order
        mo.save(update_fields=["parent"])
    refresh_order(order, cat)
    return order


@transaction.atomic
def upsert(rows, source="s2k"):
    """Open MOs from S2K (or a spreadsheet of them): [{"mo", "item", "qty", "due", "pleats"?}]. New MOs are added
    and merged into their orders; a newer revision of one on file updates it. Returns (added, updated, skipped)."""
    from hmi.engine import to_number, valid_date
    cat = Catalog()
    added = updated = skipped = 0
    for r in rows or []:
        if not isinstance(r, dict) or not r.get("mo"):
            continue
        number, part, qty = str(r["mo"]).strip(), str(r.get("item") or r.get("part") or "").strip(), to_number(r.get("qty"))
        if not number or not part or len(number) > 40 or len(part) > 40 or qty is None or qty < 0:
            skipped += 1
            continue
        base, rev = split_number(number)
        due = valid_date(str(r.get("due") or "")[:10] or None)
        mo = MO.objects.filter(base=base).order_by("-rev").first() or MO.objects.filter(number=number).first()
        if mo:
            if mo.status != "OPEN":
                skipped += 1   # closed here: S2K can't reopen or change it
                continue
            if rev < mo.rev:
                continue   # an older revision than the one on file
            fields = []
            if rev > mo.rev:   # MO.number stays as first seen (records name it); s2k_number shows the revision
                mo.rev = rev
                fields.append("rev")
            if mo.remaining != int(qty):
                mo.remaining = int(qty)
                fields.append("remaining")
            if due and due != mo.due:
                mo.due = due
                fields.append("due")
            if r.get("pleats") is not None and mo.pleats != r["pleats"]:
                mo.pleats = r["pleats"]
                fields.append("pleats")
            if fields:
                mo.save(update_fields=fields)
                updated += 1
            continue
        if qty < 1:
            skipped += 1
            continue
        item = (cat.matches(part) or [None])[0]
        mo = MO.objects.create(number=number, part=part, qty=int(qty), remaining=int(qty), due=due, base=base, rev=rev,
                               stage=stage_of(part), ops=r.get("ops") or default_ops(item), pleats=r.get("pleats"), source=source)
        group(mo, cat)
        added += 1
    return added, updated, skipped
