"""Shared helpers: time conversions, shift windows, cost sheet lookups and part specs.

Times cross the API as epoch milliseconds so the browser can do arithmetic on them directly.
"""
from datetime import datetime, timedelta

from django.utils import timezone

from .models import CostItem, PartSpec, Shift, part_key

MIN = 60_000
HOUR = 3_600_000
DAY = 86_400_000


def now():
    return timezone.now()


def ms(dt):
    return int(dt.timestamp() * 1000) if dt else None


def from_ms(v):
    if v in (None, ""):
        return None
    return datetime.fromtimestamp(int(v) / 1000, tz=timezone.get_current_timezone())


def local(dt):
    return timezone.localtime(dt)


def hm(s):
    try:
        h, m = str(s or "0:0").split(":")[:2]
        return int(h) * 60 + int(m)
    except ValueError:
        return 0


def sod_ms(t):
    """Local midnight at or before t (ms)."""
    d = local(from_ms(t)).replace(hour=0, minute=0, second=0, microsecond=0)
    return ms(d)


def add_days_ms(t, n):
    d = local(from_ms(t))
    nd = (d.replace(tzinfo=None) + timedelta(days=n))
    return ms(timezone.make_aware(nd))


def at_local_minutes(day_ms, minutes):
    """Local wall-clock time `minutes` after midnight of the day starting at day_ms (handles DST)."""
    d = local(from_ms(day_ms)).replace(tzinfo=None) + timedelta(minutes=minutes)
    return ms(timezone.make_aware(d))


def at_wall(day, minutes):
    """The local wall-clock time `minutes` after midnight on `day` (a date), as an aware datetime. Adding the minutes
    before making it aware keeps 6:00 at 6:00 across a daylight-saving change (after midnight + 6 h it would be 5:00
    or 7:00 that day). Minutes past 24 h land on the next days, also by the clock."""
    from datetime import datetime, timedelta

    from django.utils import timezone
    return timezone.make_aware(datetime(day.year, day.month, day.day) + timedelta(minutes=minutes))


def iso_date(t_ms):
    return local(from_ms(t_ms)).date().isoformat()


def extra_breaks(start, end):
    """Extra breaks (Admin-scheduled) between two aware datetimes: [{id, s, e, label, machines: set of ids or None}]."""
    from datetime import timedelta

    from django.db.models import Q

    from .models import ExtraBreak
    a, b = local(start).date(), local(end).date()
    a0 = a - timedelta(days=1)   # a break late in a shift that started yesterday
    out = []
    for x in ExtraBreak.objects.filter(day__lte=b).filter(Q(until__gte=a0) | Q(until__isnull=True, day__gte=a0)).prefetch_related("machines"):
        ids = {m.pk for m in x.machines.all()} or None
        d = max(x.day, a0)
        while d <= min(x.last_day, b):
            s = at_wall(d, hm(x.start))
            e = at_wall(d, hm(x.start) + x.minutes)
            if s < end and e > start and not (x.cancelled_at and s >= x.cancelled_at):   # (a cancelled break's earlier times stay)
                out.append({"id": x.pk, "s": s, "e": e, "label": x.label or "Break", "machines": ids, "by": x.by})
            d += timedelta(days=1)
    return sorted(out, key=lambda w: w["s"])


def is_lunch(label):
    return "lunch" in str(label or "").lower()


def shift_list():
    return list(Shift.objects.prefetch_related("breaks").all())


def shift_windows(day_ms, shifts=None, all_days=False):
    """Shift windows starting on the local day `day_ms`: [{name, s, e, brk:[{name, s, e}]}] in ms. Each shift as it
    was (or will be) worked that day: its seasonal variant, or how it was before a later change (plant/schedule.py)."""
    from .schedule import book
    d = local(from_ms(day_ms)).date()
    dow = (d.weekday() + 1) % 7  # 0 = Sunday
    ids = None if shifts is None else {x.id for x in shifts}
    out = []
    for sh in book().all_on(d, ids):
        if not all_days and dow not in (sh.get("days") or []):  # a shift with no days ticked is not worked (same as the Scheduler)
            continue
        s0 = hm(sh["start"])
        e = hm(sh["end"])
        if e <= s0:
            e += 1440
        brk = []
        for b in sh.get("breaks") or []:
            bs = hm(b["start"])
            if bs < s0:
                bs += 1440
            be = hm(b["end"])
            if be < hm(b["start"]):
                be += 1440
            ln = be - hm(b["start"])
            brk.append({"name": b.get("label") or "Break", "s": at_local_minutes(day_ms, bs), "e": at_local_minutes(day_ms, bs + ln), "sid": sh.get("id")})
        out.append({"sid": sh.get("id"), "name": sh["name"], "s": at_local_minutes(day_ms, s0), "e": at_local_minutes(day_ms, e), "brk": brk,
                    "variant": sh.get("variant")})
    return sorted(out, key=lambda w: w["s"])


def shift_at(t_ms, shifts=None):
    shifts = shifts if shifts is not None else shift_list()
    day = sod_ms(t_ms)
    for off in (0, -1):
        for w in shift_windows(add_days_ms(day, off), shifts):
            if w["s"] <= t_ms < w["e"]:
                return w
    return None


# ---------------------------------------------------------------- cost sheet
class Catalog:
    """Cost sheet index: customer part numbers match first, then AQ numbers."""

    def __init__(self, items=None):
        self.items = [i.as_dict() for i in (items if items is not None else CostItem.objects.all())]
        self.by_uid = {i["uid"]: i for i in self.items}
        self.index = {}
        for it in self.items:
            for key, pri in ((it["cust"], 0), (it["aq"], 1)):
                k = str(key or "").strip().upper()
                if k:
                    self.index.setdefault(k, []).append((pri, it))
        for v in self.index.values():
            v.sort(key=lambda x: x[0])

    def matches(self, part):
        """Cost sheet rows for a part number; a stage part (1MG4062, 2MG4062) matches its finished part's rows."""
        p = str(part or "").strip().upper()
        hit = self.index.get(p) or self.index.get(part_key(p)) or []
        return [it for _, it in hit]

    def item_for(self, mo):
        uid = mo.get("itemUid") if isinstance(mo, dict) else mo.item_uid
        if uid and uid in self.by_uid:
            return self.by_uid[uid]
        part = mo.get("part") if isinstance(mo, dict) else mo.part
        m = self.matches(part)
        return m[0] if m else None


def default_ops(item):
    return {"s1": True, "eb": bool(item and (item.get("s2") or item.get("mask"))), "ac": bool(item and item.get("s3")), "gb": False}


def part_spec(item, specs=None, catalog=None):
    """Drawing spec for a part: the Parts & specs table, else only the cost sheet's pleat count (no dimensions: they
    are never guessed). "estimated" marks a part that isn't in Parts & specs."""
    k = part_key(item)
    if specs is None:
        specs = {p.key: p.as_dict() for p in PartSpec.objects.all()}
    if k in specs:
        return dict(specs[k], key=k)
    cat = catalog or Catalog()
    m = cat.matches(k) or cat.matches(item)
    pleats = m[0]["pleats"] if m else None
    return {"key": k, "pleats": pleats, "len": None, "wid": None, "ph": None, "sb": None, "tol": None, "rev": "",
            "media": "", "url": "", "estimated": True}
