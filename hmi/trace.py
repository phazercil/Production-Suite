"""Traceability: which media rolls (and lots) went into which MOs, and the other way round. Quality › Traceability.

A roll is on its line from when its tag was scanned (Roll tags on the Operator Kiosk) until the next roll's tag is
scanned on that line, or until it is taken off (Roll off), whichever comes first. An MO runs on a line over its
MO runs (hmi_mo_runs, written when an order ends or a shift log closes) and, while it is still running, from its
start on the open shift log. The filters a roll made for an MO are the units the line counted (hmi_ppm.units) while
both were on. So a roll that runs across a shift change or an order change is split correctly, and an MO that
used several rolls lists each of them with its own count.
"""
from django.db.models import Q, Sum

from .models import MoRun, PpmReading, QCCheck, RollTag, ShiftRun

MAX = 200


def _units(line, a, b):
    if b <= a:
        return 0
    return PpmReading.objects.filter(line=line, t__gte=a, t__lt=b).aggregate(n=Sum("units"))["n"] or 0


def roll_end(tag, now):
    """When the roll came off its line (None = still on)."""
    nxt = RollTag.objects.filter(line=tag.line, t__gt=tag.t).order_by("t", "id").values_list("t", flat=True).first()
    ends = [x for x in (tag.off_t, nxt) if x]
    return min(ends) if ends else None


def current_roll(line):
    """The roll on the line now, if its tag was scanned and it hasn't been taken off or replaced."""
    tag = RollTag.objects.filter(line=line).order_by("-t", "-id").first()
    return tag if tag and not tag.off_t else None


def mo_windows(line, a, b, now):
    """(mo, item, start, end) for every MO run on the line that overlaps [a, b), the running one included."""
    out = [(r.mo, r.item, r.start_time, r.end_time) for r in
           MoRun.objects.filter(line=line, start_time__lt=b, end_time__gt=a).order_by("start_time")]
    for sh in ShiftRun.objects.filter(line=line, active=True, mo__isnull=False).select_related("mo"):
        s = sh.mo_start or sh.start
        if s < b:
            out.append((sh.mo.number, sh.mo.part, s, now))
    return out


def _overlap(a, b, c, d):
    return max(a, c), min(b, d)


def _qc(line, a, b):
    qs = QCCheck.objects.filter(line=line, t__gte=a, t__lt=b)
    return {"n": qs.count(), "fail": qs.filter(passed=False).count()}


def roll_usage(tag, now):
    """What one roll made: per MO, from when to when and how many filters."""
    end = roll_end(tag, now)
    b = end or now
    parts, covered = [], 0
    for mo, item, s, e in mo_windows(tag.line, tag.t, b, now):
        x, y = _overlap(tag.t, b, s, e)
        if y <= x:
            continue
        n = _units(tag.line, x, y)
        covered += n
        parts.append({"mo": mo, "item": item, "from": x, "to": y, "units": n})
    total = _units(tag.line, tag.t, b)
    return {"tag": tag, "end": end, "on": end is None, "mos": parts, "units": total, "noMo": max(0, total - covered),
            "qc": _qc(tag.line, tag.t, b)}


def mo_rolls(mo_number, now):
    """The rolls an MO ran on: per line and run, each roll with the filters it made for this MO."""
    runs = [(r.line, r.item, r.start_time, r.end_time) for r in MoRun.objects.filter(mo=mo_number).order_by("start_time")]
    for sh in ShiftRun.objects.filter(active=True, mo__number=mo_number).select_related("mo"):
        runs.append((sh.line, sh.mo.part, sh.mo_start or sh.start, now))
    item = runs[0][1] if runs else ""
    used = {}
    for line, _, s, e in runs:
        before = RollTag.objects.filter(line=line, t__lte=s).order_by("-t", "-id").first()   # the roll already on when the run began
        tags = list(RollTag.objects.filter(line=line, t__gt=s, t__lt=e).order_by("t", "id"))
        if before:
            tags.insert(0, before)
        for tag in tags:
            end = roll_end(tag, now) or now
            x, y = _overlap(tag.t, end, s, e)
            if y <= x:
                continue
            u = used.setdefault(tag.pk, {"tag": tag, "units": 0, "from": x, "to": y})
            u["units"] += _units(line, x, y)
            u["from"], u["to"] = min(u["from"], x), max(u["to"], y)
    total = sum(_units(line, s, e) for line, _, s, e in runs)
    rolls = sorted(used.values(), key=lambda r: r["from"])
    return {"mo": mo_number, "item": item, "runs": [{"line": line, "from": s, "to": e} for line, _, s, e in runs], "rolls": rolls,
            "units": total, "noRoll": max(0, total - sum(r["units"] for r in rolls))}


def search(q, now, a=None, b=None, line=""):
    """Rolls whose roll ID, lot or material matches, and MOs (or parts) that match, each traced."""
    q = str(q or "").strip()[:80]
    tags = RollTag.objects.all()
    if q:
        tags = tags.filter(Q(roll__icontains=q) | Q(lot__icontains=q) | Q(material__icontains=q))
    if a:
        tags = tags.filter(t__gte=a)
    if b:
        tags = tags.filter(t__lt=b)
    if line:
        tags = tags.filter(line=line)
    rolls = [roll_usage(t, now) for t in tags.order_by("-t", "-id")[:MAX]]
    mos = []
    if q:
        nums = list(MoRun.objects.filter(Q(mo__icontains=q) | Q(item__icontains=q)).order_by("-start_time").values_list("mo", flat=True)[:500])
        nums += list(ShiftRun.objects.filter(active=True).filter(Q(mo__number__icontains=q) | Q(mo__part__icontains=q))
                     .values_list("mo__number", flat=True))
        seen = []
        for n in nums:
            if n and n not in seen:
                seen.append(n)
        mos = [mo_rolls(n, now) for n in seen[:50]]
    return {"rolls": rolls, "mos": mos, "more": len(rolls) >= MAX}


def csv_rows(R):
    yield ["Roll", "Lot", "Material", "Footage", "Line", "Loaded", "Loaded by", "Off", "MO", "Part", "From", "To", "Filters"]
    from plant.util import local
    f = lambda d: local(d).strftime("%Y-%m-%d %H:%M") if d else ""   # noqa: E731
    for r in R["rolls"]:
        t = r["tag"]
        base = [t.roll, t.lot, t.material, t.feet or "", t.line, f(t.t), t.by, f(r["end"]) if r["end"] else "on the line"]
        for m in r["mos"] or [{"mo": "", "item": "", "from": None, "to": None, "units": r["units"]}]:
            yield base + [m["mo"], m["item"], f(m["from"]), f(m["to"]), m["units"]]
