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


def overlaps(wins):
    """Pairs of MO windows on one line that overlap: [(mo, mo, from, to)]. Production in that time would be counted
    for both, so it is reported instead of being split by guesswork."""
    out = []
    ws = sorted(wins, key=lambda w: w[2])
    for i, (m1, _, s1, e1) in enumerate(ws):
        for m2, _, s2, e2 in ws[i + 1:]:
            if s2 >= e1:
                break
            x, y = max(s1, s2), min(e1, e2)
            if y > x:
                out.append((m1, m2, x, y))
    return out


def _check(total, allocated):
    """Reconciliation: allocated + unallocated = made. Over-allocation (the same filters given to two MOs) is shown,
    never hidden behind a zero."""
    return {"unallocated": max(0, total - allocated), "over": max(0, allocated - total), "ok": allocated <= total}


def _qc(line, a, b):
    qs = QCCheck.objects.filter(line=line, t__gte=a, t__lt=b)
    return {"n": qs.count(), "fail": qs.filter(passed=False).count()}


def roll_usage(tag, now):
    """What one roll made: per MO, from when to when and how many filters."""
    end = roll_end(tag, now)
    b = end or now
    parts, covered = [], 0
    wins = mo_windows(tag.line, tag.t, b, now)
    for mo, item, s, e in wins:
        x, y = _overlap(tag.t, b, s, e)
        if y <= x:
            continue
        n = _units(tag.line, x, y)
        covered += n
        parts.append({"mo": mo, "item": item, "from": x, "to": y, "units": n})
    total = _units(tag.line, tag.t, b)
    ov = [(m1, m2, max(x, tag.t), min(y, b)) for m1, m2, x, y in overlaps(wins) if min(y, b) > max(x, tag.t)]
    return {"tag": tag, "end": end, "on": end is None, "mos": parts, "units": total, "noMo": max(0, total - covered),
            "rec": _check(total, covered), "overlaps": ov, "qc": _qc(tag.line, tag.t, b)}


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
    ov = []    # other MOs whose windows overlap this one's on the same line (the same filters counted for both)
    for line, _, s, e in runs:
        for m1, m2, x, y in overlaps(mo_windows(line, s, e, now)):
            if mo_number in (m1, m2) and m1 != m2:
                ov.append((m2 if m1 == mo_number else m1, line, x, y))
    allocated = sum(r["units"] for r in rolls)
    return {"mo": mo_number, "item": item, "runs": [{"line": line, "from": s, "to": e} for line, _, s, e in runs], "rolls": rolls,
            "units": total, "noRoll": max(0, total - allocated), "rec": _check(total, allocated), "overlaps": ov}


def reconcile(now, a, b, line=""):
    """Quality › Traceability › Reconciliation: per line, filters the machine counted in [a, b) against what MO
    runs and rolls account for, with every overlap of MO windows (filters that would be counted twice)."""
    from plant.models import Machine
    lines = [line] if line else list(Machine.objects.filter(stage="s1").values_list("name", flat=True))
    out = []
    for ln in lines:
        made = _units(ln, a, b)
        wins = mo_windows(ln, a, b, now)
        on_mo = sum(_units(ln, max(a, s), min(b, e)) for _, _, s, e in wins)
        tags = list(RollTag.objects.filter(line=ln, t__lt=b).filter(Q(off_t__isnull=True) | Q(off_t__gt=a)).order_by("t", "id"))
        on_roll = 0
        for tg in tags:
            e = roll_end(tg, now) or now
            x, y = max(a, tg.t), min(b, e)
            if y > x:
                on_roll += _units(ln, x, y)
        ov = [(m1, m2, max(x, a), min(y, b)) for m1, m2, x, y in overlaps(wins) if min(y, b) > max(x, a)]
        out.append({"line": ln, "made": made, "onMo": on_mo, "onRoll": on_roll, "mo": _check(made, on_mo),
                    "roll": _check(made, on_roll), "overlaps": ov,
                    "overlapUnits": sum(_units(ln, x, y) for _, _, x, y in ov)})
    return out


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


def _pallets(mo_number):
    from packkiosk.models import PalletTag
    return list(PalletTag.objects.filter(mo=mo_number, voided=False).order_by("pallet_no"))


def recall(q, now):
    """Quality › Traceability › Recall: everything a supplier lot (or roll) touched, end to end: each MO it went into,
    the shift logs and QC checks while it ran, and every pallet tag of those MOs to quarantine. Backward: a pallet
    tag number (or MO) gives its MO, the rolls and lots that went into it, and its QC history.
    q: a lot or roll ID, "pallet:<MO>-<pallet #>" / "pallet:<MO>" or "mo:<MO>"."""
    from packkiosk.models import PalletTag
    q = str(q or "").strip()[:80]
    if not q:
        return None
    if q.lower().startswith(("pallet:", "mo:")):
        kind, _, val = q.partition(":")
        mo, _, no = val.strip().partition("-")
        pallets = PalletTag.objects.filter(mo=mo.strip(), voided=False)
        if no.strip().isdigit():
            pallets = pallets.filter(pallet_no=int(no))
        pallets = list(pallets.order_by("pallet_no"))
        m = mo_rolls(mo.strip(), now)
        runs = m["runs"]
        checks = []
        for r in runs:
            checks += list(QCCheck.objects.filter(line=r["line"], t__gte=r["from"], t__lt=r["to"], mo=mo.strip()).order_by("t"))
        return {"dir": "back", "q": q, "mo": mo.strip(), "pallets": pallets, "rolls": m["rolls"], "lots": sorted({r["tag"].lot for r in m["rolls"] if r["tag"].lot}),
                "runs": runs, "checks": checks, "fails": [c for c in checks if not c.passed], "rec": m["rec"], "overlaps": m["overlaps"]}
    tags = list(RollTag.objects.filter(Q(lot__iexact=q) | Q(roll__iexact=q)).order_by("t", "id")[:MAX])
    if not tags:
        return {"dir": "fwd", "q": q, "none": True}
    mos, shifts, checks = {}, {}, []
    for tag in tags:
        u = roll_usage(tag, now)
        for p in u["mos"]:
            x = mos.setdefault(p["mo"], {"mo": p["mo"], "item": p["item"], "units": 0, "from": p["from"], "to": p["to"], "lines": set()})
            x["units"] += p["units"]
            x["from"], x["to"] = min(x["from"], p["from"]), max(x["to"], p["to"])
            x["lines"].add(tag.line)
            for sh in ShiftRun.objects.filter(line=tag.line, start__lt=p["to"]).filter(Q(end__isnull=True) | Q(end__gt=p["from"])):
                shifts[sh.pk] = sh
            checks += list(QCCheck.objects.filter(line=tag.line, t__gte=p["from"], t__lt=p["to"]).order_by("t"))
    out_mos = []
    for x in sorted(mos.values(), key=lambda x: x["from"]):
        x["lines"] = ", ".join(sorted(x["lines"]))
        x["pallets"] = _pallets(x["mo"])
        out_mos.append(x)
    return {"dir": "fwd", "q": q, "tags": tags, "mos": out_mos, "shifts": sorted(shifts.values(), key=lambda s: s.start),
            "checks": checks, "fails": [c for c in checks if not c.passed],
            "pallets": [p for x in out_mos for p in x["pallets"]], "units": sum(x["units"] for x in out_mos)}


def recall_csv(R):
    from plant.util import local
    f = lambda d: local(d).strftime("%Y-%m-%d %H:%M") if d else ""   # noqa: E731
    if R["dir"] == "fwd":
        yield ["Recall for", R["q"]]
        yield ["MO", "Part", "Lines", "From", "To", "Filters from this lot", "Pallet #", "Pallet qty", "Packed", "Line"]
        for m in R["mos"]:
            for p in m["pallets"] or [None]:
                yield [m["mo"], m["item"], m["lines"], f(m["from"]), f(m["to"]), m["units"]] + (
                    [p.pallet_no, p.qty, f(p.t), p.line] if p else ["", "", "", ""])
    else:
        yield ["Trace back", R["q"]]
        yield ["Roll", "Lot", "Material", "Line", "Loaded", "Filters for this MO"]
        for r in R["rolls"]:
            yield [r["tag"].roll, r["tag"].lot, r["tag"].material, r["tag"].line, f(r["tag"].t), r["units"]]
