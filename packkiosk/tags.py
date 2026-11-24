"""Pallet tag rules: the next pallet number, what to fill in for a part, and saving, signing and voiding tags."""
from django.db.models import Max
from django.utils import timezone

from plant import perms
from plant.models import MO, Machine, Person, part_key

from .models import PalletTag

VERIFY, SUPERVISE = "pallets.verify", "pallets.supervise"


def clean(v, n):
    return str(v or "").strip()[:n]


def to_int(v):
    try:
        return int(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def lines():
    """Packaging machines first, then every other active line."""
    ms = list(Machine.objects.filter(active=True).order_by("sort", "name").values_list("name", "stage"))
    return [n for n, s in ms if s == "pk"] + [n for n, s in ms if s != "pk"]


TODAY_KEY = "packkiosk.todays_orders"


def _todays_orders():
    """The orders (top-level MO ids) running today: on the Scheduler's board today on any line and stage, running
    on a line now, or run on one earlier today."""
    from datetime import datetime, time, timedelta

    from hmi.models import MoRun, ShiftRun
    from scheduler.engine import compute, load_world, ms

    day = timezone.localdate()
    a = timezone.make_aware(datetime.combine(day, time.min))
    b = a + timedelta(days=1)
    ids = set()
    try:
        sch = compute(load_world())
        sa, sb = ms(a), ms(b)
        ids |= {r["moId"] for r in sch["res"].values() if r.get("segs") and r["start"] < sb and r["end"] > sa}
    except Exception:   # a board that can't be worked out still leaves what is running and what ran today
        import logging
        logging.getLogger("plant").exception("Packaging Kiosk: today's MOs from the Scheduler")
    numbers = set(MoRun.objects.filter(end_time__gte=a, start_time__lt=b).values_list("mo", flat=True))
    ran = MO.objects.filter(number__in=numbers).values_list("id", "parent_id")
    live = MO.objects.filter(pk__in=ShiftRun.objects.filter(active=True, mo__isnull=False).values("mo_id")).values_list("id", "parent_id")
    ids |= {parent or pk for pk, parent in list(ran) + list(live)}
    return ids


def todays_mos(keep=""):
    """Open MOs running today to pack against (the order and its finished-part MOs, not Stage 1 or end band MOs):
    never the whole MO list. `keep`: an MO already picked on the form stays in the list."""
    from django.core.cache import cache
    from django.db.models import Q
    ids = cache.get(TODAY_KEY)
    if ids is None:
        ids = _todays_orders()
        cache.set(TODAY_KEY, ids, 60)
    qs = MO.objects.filter(Q(pk__in=ids) | Q(parent_id__in=ids), status="OPEN", example=False).exclude(stage__in=("s1", "eb"))
    if keep:
        qs = qs | MO.objects.filter(number=keep)
    return [{"number": m.number, "part": m.part, "qty": m.qty} for m in qs.distinct().order_by("due", "number")]


def _today():
    """Today, local: [midnight, next midnight) as aware times. (t__date needs MariaDB's time zone tables, which a
    Windows install often hasn't got: then it finds nothing.)"""
    from plant.util import at_wall
    d = timezone.localdate()
    return at_wall(d, 0), at_wall(d, 1440)


def _live(qs):
    return qs.filter(voided=False)


def next_pallet(mo="", part=""):
    """The next pallet number: one more than the highest on this MO, or (no MO) on this part today."""
    if mo:
        qs = _live(PalletTag.objects.filter(mo=mo))
    elif part:
        a, b = _today()
        qs = _live(PalletTag.objects.filter(part=part, mo="", t__gte=a, t__lt=b))
    else:
        return 1
    return (qs.aggregate(m=Max("pallet_no"))["m"] or 0) + 1


def suggest(mo="", part=""):
    """What to fill in on a new tag: the MO's part, the next pallet number, and the last quantity, line and
    description used for this part."""
    mo, part = clean(mo, 40), clean(part, 40).upper()
    m = MO.objects.filter(number=mo).first() if mo else None
    if m and not part:
        part = m.part
    last = _live(PalletTag.objects.filter(part=part)).first() if part else None
    return {"part": part, "mo": mo if m or mo else "", "pallet": next_pallet(mo, part),
            "qty": last.qty if last else None, "line": last.line if last else "", "description": last.description if last else "",
            "moQty": m.qty if m else None, "packed": sum(_live(PalletTag.objects.filter(mo=mo)).values_list("qty", flat=True)) if mo else None}


def create(p, me):
    part = clean(p.get("part"), 40).upper()
    mo = clean(p.get("mo"), 40)
    line = clean(p.get("line"), 60)
    qty = to_int(p.get("qty"))
    pal = to_int(p.get("pallet_no")) if str(p.get("pallet_no") or "").strip() else next_pallet(mo, part)
    if not part:
        return {"ok": False, "error": "Enter the part number."}
    if not line:
        return {"ok": False, "error": "Pick the manufacturing line."}
    if line not in lines():
        return {"ok": False, "error": f"{line} isn't a line in the suite (Admin › Machines)."}
    if not qty or qty < 1 or qty > 1_000_000:
        return {"ok": False, "error": "Enter how many filters are on the pallet."}
    if not pal or pal < 1 or pal > 99999:
        return {"ok": False, "error": "Enter the pallet number."}
    if mo:
        m = MO.objects.filter(number=mo).first()
        if not m:
            return {"ok": False, "error": f"MO {mo} isn't open in the suite. Leave the MO blank to tag without one."}
        if part_key(m.part) != part_key(part):
            return {"ok": False, "error": f"MO {mo} is for {m.part}, not {part}."}
        dup = _live(PalletTag.objects.filter(mo=mo, pallet_no=pal)).first()
    else:
        a, b = _today()
        dup = _live(PalletTag.objects.filter(part=part, mo="", pallet_no=pal, t__gte=a, t__lt=b)).first()
    if dup:
        return {"ok": False, "error": f"Pallet {pal} already has a tag ({dup.tag_no}, {dup.qty:,} pcs by {dup.packed_by}). "
                                      f"Use pallet {next_pallet(mo, part)}, or void the old tag first."}
    now = timezone.now()
    tag = PalletTag.objects.create(part=part, description=clean(p.get("description"), 80), mo=mo, line=line, pallet_no=pal, qty=qty,
                                   t=now, packed_by=me.name, packed_by_id=me.pk, updated=now, updated_by=me.name)
    return {"ok": True, "id": tag.pk, "msg": f"Tag {tag.tag_no} made: {part}, pallet {pal}, {qty:,} pcs."}


def sign(tag, signer, kind):
    """`signer` puts their name on the tag as verifier or supervisor (their PIN was checked by the view)."""
    if tag.voided:
        return {"ok": False, "error": "That tag was voided."}
    now = timezone.now()
    if kind == "verify":
        if not perms.can(signer, VERIFY):
            return {"ok": False, "error": f"{signer.name} can't verify pallets (Admin › Permissions › Verify pallet tags)."}
        if tag.packed_by_id == signer.pk:
            return {"ok": False, "error": "Someone other than the packer has to verify the pallet."}
        tag.verified_by, tag.verified_t = signer.name, now
    elif kind == "supervisor":
        if not perms.can(signer, SUPERVISE):
            return {"ok": False, "error": f"{signer.name} can't sign as supervisor (Admin › Permissions › Supervisor sign-off on pallet tags)."}
        tag.supervisor, tag.supervisor_t = signer.name, now
    else:
        return {"ok": False, "error": "Unknown sign-off."}
    tag.updated, tag.updated_by = now, signer.name
    tag.save()
    who = "Verified" if kind == "verify" else "Supervisor sign-off"
    return {"ok": True, "msg": f"{who} by {signer.name} on {tag.tag_no}. Print the tag again to show it."}


def void(tag, me, reason):
    if tag.voided:
        return {"ok": False, "error": "That tag was already voided."}
    if tag.packed_by_id != me.pk and not perms.can(me, SUPERVISE):
        return {"ok": False, "error": "Only the person who made the tag or a shift lead can void it."}
    reason = clean(reason, 200)
    if not reason:
        return {"ok": False, "error": "Say why the tag is being voided."}
    tag.voided, tag.voided_by, tag.void_reason = True, me.name, reason
    tag.updated, tag.updated_by = timezone.now(), me.name
    tag.save()
    return {"ok": True, "msg": f"{tag.tag_no} voided. Pallet {tag.pallet_no} can be tagged again."}


def person(pk):
    return Person.objects.filter(pk=pk, active=True).first()
