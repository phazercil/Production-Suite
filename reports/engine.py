"""Line summary for the Reports page, with drill up and down.

Where:  plant  ->  line  ->  shift (1st, 2nd ...)  ->  MO
When:   year   ->  month ->  day  ->  hour        (a week view is available from any day or month)

Everything comes from the suite's own records, the same tables Grafana reads: hmi_ppm (one row per machine per
minute, with the filters made that minute), hmi_downtime, hmi_qc (operator checks), hmi_qc_auto (machine vision),
hmi_mo_runs and hmi_shifts. Crew wages come from the Labor Budget plan (shift leads only).
"""
import calendar as pycal
from datetime import date, datetime, timedelta

from django.db.models import Avg, Count, Q, Sum
from django.utils import timezone

from hmi.models import AutoQC, Downtime, MoRun, PpmReading, QCCheck
from plant.models import Machine, PlantSettings, Shift

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]


class BadPeriod(ValueError):
    pass


def _aware(d, h=0):
    return timezone.make_aware(datetime(d.year, d.month, d.day, h))


# ------------------------------------------------------------------ when
def parse_period(p):
    """'year:2026' | 'month:2026-10' | 'week:2026-10-26' | 'day:2026-10-29' -> description with its children."""
    try:
        kind, val = str(p).split(":", 1)
        if kind == "year":
            y = int(val)
            a, b = date(y, 1, 1), date(y + 1, 1, 1)
            kids = [(f"month:{y}-{m:02d}", MONTHS[m - 1][:3], _aware(date(y, m, 1)), _aware(date(y + (m == 12), m % 12 + 1, 1))) for m in range(1, 13)]
            return {"key": p, "kind": kind, "label": str(y), "start": _aware(a), "end": _aware(b), "children": kids, "parent": None}
        if kind == "month":
            y, m = (int(x) for x in val.split("-"))
            n = pycal.monthrange(y, m)[1]
            kids = [(f"day:{y}-{m:02d}-{d:02d}", str(d), _aware(date(y, m, d)), _aware(date(y, m, d) + timedelta(days=1))) for d in range(1, n + 1)]
            end = date(y + (m == 12), m % 12 + 1, 1)
            return {"key": p, "kind": kind, "label": f"{MONTHS[m - 1]} {y}", "start": _aware(date(y, m, 1)), "end": _aware(end),
                    "children": kids, "parent": f"year:{y}"}
        if kind == "week":
            d0 = date.fromisoformat(val)
            d0 -= timedelta(days=d0.weekday())  # Monday
            kids = [(f"day:{(d0 + timedelta(days=i)).isoformat()}", (d0 + timedelta(days=i)).strftime("%a %-m/%-d") if hasattr(d0, "strftime") else "",
                     _aware(d0 + timedelta(days=i)), _aware(d0 + timedelta(days=i + 1))) for i in range(7)]
            return {"key": f"week:{d0.isoformat()}", "kind": kind, "label": f"Week of {d0.strftime('%b')} {d0.day}, {d0.year}",
                    "start": _aware(d0), "end": _aware(d0 + timedelta(days=7)), "children": kids, "parent": f"month:{d0.year}-{d0.month:02d}"}
        if kind == "day":
            d0 = date.fromisoformat(val)
            nxt = d0 + timedelta(days=1)
            kids = []
            for h in range(24):
                a = _aware(d0, h)
                b = _aware(d0, h + 1) if h < 23 else _aware(nxt)
                kids.append((None, f"{h:02d}", a, b))
            return {"key": p, "kind": kind, "label": f"{d0.strftime('%A')} {d0.strftime('%b')} {d0.day}, {d0.year}", "start": _aware(d0),
                    "end": _aware(nxt), "children": kids, "parent": f"month:{d0.year}-{d0.month:02d}", "week": f"week:{d0.isoformat()}"}
    except (ValueError, TypeError) as e:
        raise BadPeriod(f"Unknown period {p!r}") from e
    raise BadPeriod(f"Unknown period {p!r}")


def period_crumbs(per):
    out = []
    cur = per
    while cur:
        out.insert(0, {"key": cur["key"], "label": cur["label"]})
        cur = parse_period(cur["parent"]) if cur.get("parent") else None
    return out


# ------------------------------------------------------------------ where
def parse_scope(s):
    s = str(s or "plant")
    if s == "plant":
        return {"key": "plant", "level": "plant", "label": "All lines"}
    kind, _, rest = s.partition(":")
    parts = rest.split("|")
    if kind == "line" and parts[0]:
        return {"key": s, "level": "line", "label": parts[0], "line": parts[0]}
    if kind == "shift" and len(parts) == 2:
        return {"key": s, "level": "shift", "label": parts[1], "line": parts[0], "shift": parts[1]}
    if kind == "mo" and len(parts) == 3:
        return {"key": s, "level": "mo", "label": f"MO {parts[2]}", "line": parts[0], "shift": parts[1], "mo": parts[2]}
    raise BadPeriod(f"Unknown scope {s!r}")


def scope_crumbs(sc):
    out = [{"key": "plant", "label": "All lines"}]
    if sc["level"] in ("line", "shift", "mo"):
        out.append({"key": f"line:{sc['line']}", "label": sc["line"]})
    if sc["level"] in ("shift", "mo"):
        out.append({"key": f"shift:{sc['line']}|{sc['shift']}", "label": sc["shift"]})
    if sc["level"] == "mo":
        out.append({"key": sc["key"], "label": sc["label"]})
    return out


# which field each record type is filtered and grouped by, at each level
GROUP = {"plant": "line", "line": "shift__shift_name", "shift": "mo", "mo": None}


def _filters(sc):
    """(filter for readings, filter for everything else) for a scope."""
    f = {}
    if sc["level"] in ("line", "shift", "mo"):
        f["line"] = sc["line"]
    if sc["level"] in ("shift", "mo"):
        f["shift__shift_name"] = sc["shift"]
    g = dict(f)
    if sc["level"] == "mo":
        g["mo"] = sc["mo"]
    return f, g


def _blank():
    return {"obs": 0, "run": 0, "units": 0, "ppmAvg": None, "down": 0.0, "qc": 0, "qcFail": 0, "aq": 0, "aqFail": 0, "mos": 0, "moQty": 0,
            "moMin": 0.0, "laborCost": None, "reasons": {}}


def _clip(a, b, s, e):
    return max(0.0, (min(b, e) - max(a, s)).total_seconds() / 60)


def summarize(sc, a, b, stop_ppm, with_rows=True, labor=None):
    """Totals for scope sc between a and b, and (with_rows) the same per child row."""
    f, g = _filters(sc)
    grp = GROUP[sc["level"]] if with_rows else None
    rows, total = {}, _blank()

    def row(k):
        k = k if k not in (None, "") else "(none)"
        if k not in rows:
            rows[k] = _blank()
        return rows[k]

    # readings: minutes observed and running, filters made, average speed while running
    if sc["level"] != "mo":
        qs = PpmReading.objects.filter(t__gte=a, t__lt=b, **f)
        # a minute stopped during a planned break, lunch or start-up is not counted (same as the kiosk and hmi_downtime)
        ann = dict(obs=Count("id", filter=Q(ppm__gt=stop_ppm) | Q(planned=False)), run=Count("id", filter=Q(ppm__gt=stop_ppm)), units=Sum("units"),
                   ppmAvg=Avg("ppm", filter=Q(ppm__gt=stop_ppm)))
        tot = qs.aggregate(**ann)
        for k in ("obs", "run", "units"):
            total[k] = tot[k] or 0
        total["ppmAvg"] = round(tot["ppmAvg"], 1) if tot["ppmAvg"] is not None else None
        if grp and grp != "mo":
            for r in qs.values(grp).annotate(**ann):
                x = row(r[grp])
                x.update(obs=r["obs"] or 0, run=r["run"] or 0, units=r["units"] or 0,
                         ppmAvg=round(r["ppmAvg"], 1) if r["ppmAvg"] is not None else None)
    # downtime, cut to the period
    for d in Downtime.objects.filter(start_time__lt=b, end_time__gt=a, **g).values("start_time", "end_time", "reason", *( [grp] if grp else [])):
        m = _clip(a, b, d["start_time"], d["end_time"])
        reason = d["reason"] or "No reason given"
        total["down"] += m
        total["reasons"][reason] = total["reasons"].get(reason, 0) + m
        if grp:
            x = row(d[grp])
            x["down"] += m
            x["reasons"][reason] = x["reasons"].get(reason, 0) + m
    # operator QC checks and machine vision measurements
    for model, n_key, f_key in ((QCCheck, "qc", "qcFail"), (AutoQC, "aq", "aqFail")):
        qs = model.objects.filter(t__gte=a, t__lt=b, **g)
        ann = dict(n=Count("id"), fails=Count("id", filter=Q(passed=False)))
        tot = qs.aggregate(**ann)
        total[n_key], total[f_key] = tot["n"] or 0, tot["fails"] or 0
        if grp:
            for r in qs.values(grp).annotate(**ann):
                x = row(r[grp])
                x[n_key], x[f_key] = r["n"], r["fails"]
    # MOs finished (a run of an MO that ended in the period)
    for r in MoRun.objects.filter(end_time__gte=a, end_time__lt=b, **g).values("mo", "qty", "start_time", "end_time", *([grp] if grp and grp != "mo" else [])):
        mins = max(0.0, (r["end_time"] - r["start_time"]).total_seconds() / 60)
        total["mos"] += 1
        total["moQty"] += r["qty"] or 0
        total["moMin"] += mins
        if grp:
            x = row(r[grp] if grp != "mo" else r["mo"])
            x["mos"] += 1
            x["moQty"] += r["qty"] or 0
            x["moMin"] += mins
    if sc["level"] == "mo" or grp == "mo":
        # an MO's own filters made = what its runs recorded; its time = how long they ran
        if sc["level"] == "mo":
            total["units"], total["obs"] = total["moQty"], round(total["moMin"])
        for k, x in rows.items():
            x["units"], x["obs"] = x["moQty"], round(x["moMin"])
    if labor is not None:
        _labor(sc, a, b, f, total, rows, grp, labor)
    for x in [total, *rows.values()]:
        x["down"] = round(x["down"], 1)
        per_mo = sc["level"] == "mo" or (x is not total and grp == "mo")  # MOs have run time, not minute readings
        x["uptime"] = round(100 * x["run"] / x["obs"], 1) if x["obs"] and not per_mo else None
        x["qcPass"] = round(100 * (x["qc"] - x["qcFail"]) / x["qc"], 1) if x["qc"] else None
        x["aqPass"] = round(100 * (x["aq"] - x["aqFail"]) / x["aq"], 1) if x["aq"] else None
        x["reasons"] = sorted(({"reason": k, "min": round(v, 1)} for k, v in x["reasons"].items()), key=lambda r: -r["min"])[:5]
        if x["laborCost"] is not None:
            x["laborPerUnit"] = round(x["laborCost"] / x["units"], 4) if x["units"] else None
    return total, rows


def _labor(sc, a, b, f, total, rows, grp, labor):
    """Crew wages: minutes each line ran a shift x that shift's crew cost per hour in the Labor Budget plan."""
    plan, nums = labor
    cost_of = {}

    def rate(line, shift_name):
        key = (line, shift_name)
        if key not in cost_of:
            ln = plan.line_for(line)
            n = nums.get(shift_name)
            cost_of[key] = plan.shift_info(ln, n)["costPerHour"] if ln and n else 0
        return cost_of[key]

    if sc["level"] == "mo":
        total["laborCost"] = round(total["moMin"] / 60 * rate(sc["line"], sc["shift"]), 2)
        return
    total["laborCost"] = 0.0
    for r in PpmReading.objects.filter(t__gte=a, t__lt=b, **f).values("line", "shift__shift_name").annotate(n=Count("id")):
        c = r["n"] / 60 * rate(r["line"], r["shift__shift_name"])
        total["laborCost"] += c
        if grp == "line":
            rows.setdefault(r["line"] or "(none)", _blank())
            rows[r["line"] or "(none)"]["laborCost"] = (rows[r["line"] or "(none)"]["laborCost"] or 0) + c
        elif grp == "shift__shift_name":
            k = r["shift__shift_name"] or "(none)"
            rows.setdefault(k, _blank())
            rows[k]["laborCost"] = (rows[k]["laborCost"] or 0) + c
    if grp == "mo":
        for k, x in rows.items():
            x["laborCost"] = round(x["moMin"] / 60 * rate(sc["line"], sc["shift"]), 2)
    total["laborCost"] = round(total["laborCost"], 2)
    for x in rows.values():
        if x["laborCost"] is not None:
            x["laborCost"] = round(x["laborCost"], 2)


def report(scope, period, lead=False):
    sc, per = parse_scope(scope), parse_period(period)
    stop = PlantSettings.load().config["rules"].get("stopPpm", 5)
    labor = None
    if lead:
        from labor.plan import LaborPlanView
        plan = LaborPlanView.current()
        if plan:
            nums = {s.name: i + 1 for i, s in enumerate(Shift.objects.all())}
            labor = (plan, nums)
    total, rows = summarize(sc, per["start"], per["end"], stop, True, labor)
    # child rows: drill target for each
    out_rows = []
    if sc["level"] == "plant":
        names = list(Machine.objects.filter(stage="s1").values_list("name", flat=True))
        keys = sorted(set(rows) | set(names), key=lambda k: (k not in names, names.index(k) if k in names else 0, k))
        for k in keys:
            out_rows.append({"key": f"line:{k}", "label": k, **(rows.get(k) or _finish(_blank()))})
    elif sc["level"] == "line":
        order = {s.name: i for i, s in enumerate(Shift.objects.all())}
        for k in sorted(rows, key=lambda k: (order.get(k, 99), k)):
            out_rows.append({"key": f"shift:{sc['line']}|{k}", "label": k, **rows[k]})
    elif sc["level"] == "shift":
        for k in sorted(rows):
            out_rows.append({"key": f"mo:{sc['line']}|{sc['shift']}|{k}", "label": f"MO {k}", **rows[k]})
    # chart: the scope's totals in each part of the period
    buckets = []
    now = timezone.now()
    for key, label, a, b in per["children"]:
        if a > now:
            buckets.append({"key": key, "label": label, "future": True})
            continue
        t, _ = summarize(sc, a, b, stop, False, None)
        buckets.append({"key": key, "label": label, "units": t["units"], "uptime": t["uptime"], "down": t["down"], "obs": t["obs"],
                        "qcFail": t["qcFail"] + t["aqFail"]})
    if not lead:
        total.pop("laborCost", None)
        for r in out_rows:
            r.pop("laborCost", None)
            r.pop("laborPerUnit", None)
    return {"scope": {"key": sc["key"], "level": sc["level"], "label": sc["label"], "crumbs": scope_crumbs(sc)},
            "period": {"key": per["key"], "kind": per["kind"], "label": per["label"], "crumbs": period_crumbs(per),
                       "week": per.get("week") or (f"week:{per['start'].date().isoformat()}" if per["kind"] == "day" else None),
                       "start": per["start"].isoformat(), "end": per["end"].isoformat()},
            "total": total, "rows": out_rows, "buckets": buckets, "labor": bool(labor), "stopPpm": stop,
            "drill": {"rows": sc["level"] != "mo", "buckets": per["kind"] != "day"}}


def _finish(x):
    x["uptime"] = None
    x["laborPerUnit"] = None
    x["qcPass"] = x["aqPass"] = None
    x["reasons"] = []
    return x
