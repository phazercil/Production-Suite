"""The machine schedule as it was (or will be) on a given day.

A shift on a day is, in order: nothing, if it was added after that day or removed on or before it; the seasonal
variant whose date range holds the day; else how the shift was then (the ShiftVersion kept the first time it was
changed after that day); else the shift as it is now. Every place that works out shift times for a day (the kiosk's
shift windows and planned time, the Scheduler's crewed hours, the data snapshot) asks this module, so changing the
schedule today never re-judges the days before."""
import time
from datetime import date

from django.utils import timezone

_BOOK = {"t": 0, "book": None}
TTL = 20   # seconds a loaded schedule is reused (a change in this process clears it at once)


def _brk(b):
    return {"label": b.label, "start": b.start, "end": b.end}


def _var(v):
    return {"id": v.id, "name": v.name, "from": v.date_from.isoformat(), "to": v.date_to.isoformat(), "start": v.start, "end": v.end,
            "days": list(v.days or []), "breaks": list(v.breaks or [])}


def current_data(sh):
    """A shift as it is now: {id, name, start, end, days, breaks, variants}."""
    return {"id": sh.id, "name": sh.name, "start": sh.start, "end": sh.end, "days": list(sh.days or []),
            "breaks": [_brk(b) for b in sh.breaks.all()], "variants": [_var(v) for v in sh.variants.all()]}


def apply_variant(data, d):
    """The shift on day d with its seasonal variant (if one covers d) in place of its own times, days and breaks."""
    iso = d.isoformat()
    for v in data.get("variants") or []:
        if v["from"] <= iso <= v["to"]:
            return {**data, "start": v["start"], "end": v["end"], "days": v["days"], "breaks": v["breaks"],
                    "variant": v.get("name") or "Seasonal variant"}
    return data


class Book:
    def __init__(self):
        from .models import Shift, ShiftVersion
        self.shifts = {s.id: s for s in Shift.all_objects.prefetch_related("breaks", "variants")}
        self.now = {i: current_data(s) for i, s in self.shifts.items()}
        self.versions = {}
        for v in ShiftVersion.objects.order_by("until"):
            self.versions.setdefault(v.shift_id, []).append(v)

    def on(self, sid, d):
        """Shift `sid` as worked on day d (dict), or None if it wasn't in the schedule that day."""
        s = self.shifts.get(sid)
        if not s or (s.retired_on and d >= s.retired_on) or (s.created_on and d < s.created_on):
            return None
        for v in self.versions.get(sid, []):
            if d < v.until:
                return apply_variant({**v.data, "id": sid}, d)
        return apply_variant(self.now[sid], d)

    def all_on(self, d, ids=None):
        """Every shift worked on day d (in schedule order), optionally only these ids."""
        order = sorted(self.shifts.values(), key=lambda s: (s.sort, s.id))
        out = []
        for s in order:
            if ids is not None and s.id not in ids:
                continue
            x = self.on(s.id, d)
            if x:
                out.append(x)
        return out


def book():
    import sys
    ttl = 0 if "test" in sys.argv[1:2] else TTL   # tests change shifts straight in the database
    if _BOOK["book"] is None or time.monotonic() - _BOOK["t"] > ttl:
        _BOOK.update(book=Book(), t=time.monotonic())
    return _BOOK["book"]


def forget():
    _BOOK["book"] = None


def machine_shift_ids(machine_id):
    """The shifts a machine runs, the removed ones included (they still count for the days before)."""
    from .models import Machine
    return set(Machine.shifts.through.objects.filter(machine_id=machine_id).values_list("shift_id", flat=True))


def keep_before_change(shift_id):
    """Before the first change to a shift on a day, keep how it was, for the days before."""
    from .models import Shift, ShiftVersion
    if not shift_id:
        return
    today = timezone.localdate()
    s = Shift.all_objects.filter(pk=shift_id).prefetch_related("breaks", "variants").first()
    if not s or s.created_on == today or ShiftVersion.objects.filter(shift_id=shift_id, until=today).exists():
        return   # added today (no days before it to protect), or already kept today
    data = current_data(s)
    data.pop("id", None)
    ShiftVersion.objects.create(shift_id=shift_id, until=today, data=data)


# ------------------------------------------------------------------ signals (connected in plant/apps.py)
def _shift_pre_save(sender, instance, raw=False, **kw):
    if raw or not instance.pk:
        return
    old = sender.all_objects.filter(pk=instance.pk).values("name", "start", "end", "days", "retired_on").first()
    if old and (old["start"], old["end"], list(old["days"] or [])) != (instance.start, instance.end, list(instance.days or [])):
        keep_before_change(instance.pk)


def _child_change(sender, instance, raw=False, **kw):
    if not raw:
        keep_before_change(instance.shift_id)


def _changed(sender, **kw):
    forget()


def connect():
    from django.db.models.signals import post_delete, post_save, pre_delete, pre_save

    from .models import Shift, ShiftBreak, ShiftVariant
    pre_save.connect(_shift_pre_save, sender=Shift, dispatch_uid="sched-shift")
    for m in (ShiftBreak, ShiftVariant):
        pre_save.connect(_child_change, sender=m, dispatch_uid=f"sched-{m.__name__}-save")
        pre_delete.connect(_child_change, sender=m, dispatch_uid=f"sched-{m.__name__}-del")
    for m in (Shift, ShiftBreak, ShiftVariant):
        post_save.connect(_changed, sender=m, dispatch_uid=f"sched-{m.__name__}-forget")
        post_delete.connect(_changed, sender=m, dispatch_uid=f"sched-{m.__name__}-forget-del")
    from .models import Machine
    from django.db.models.signals import m2m_changed
    m2m_changed.connect(_changed, sender=Machine.shifts.through, dispatch_uid="sched-machine-shifts")


def today():
    return timezone.localdate()


__all__ = ["book", "forget", "keep_before_change", "machine_shift_ids", "apply_variant", "current_data", "date"]
