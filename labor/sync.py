"""Keeps the Labor Budget plan in step with the rest of the suite.

The suite is the source of truth for what the plant has; the Labor Budget for what it costs:

- Lines. Every suite machine (Scheduler › Lines & Shifts / Admin › Machines) is a line in the plan, linked by
  `machineId`, in the suite's order. The suite sets each linked line's name, stage, color and speed
  (`hourlyRate`, filters per hour). Which shifts a line runs is one setting in both places: turning a shift on
  or off for a line in the Labor Budget changes the machine's shifts in the Scheduler, and the other way
  round (`save_line_shifts`). The plan keeps its crews,
  positions, wages, downtime / scrap estimates and the rest. Lines the plan adds with a labor-only stage
  (Packaging Only, Non Production, Backup) stay the plan's own. A plan line whose machine was removed from the
  suite is kept, unlinked and marked, so its crew can be moved before it is deleted.
- Shifts. The suite's shifts, in order, are the plan's shifts 1, 2, 3 (the plan has at most 3). Each shift's
  main crew schedule (code A) takes the suite shift's start, end and work days, and its lunch from the
  shift's breaks named "lunch". Other crew schedules (B, C...) stay the plan's own.
- Work week. The plan's normal work days are the days the suite's shifts run; its holidays and extra work
  days stay the plan's own.
- People. The Team list is offered for the names on crew slots, and names not on it are pointed out. Wages
  stay in the plan (shift leads only).

`merge()` is applied whenever the plan is read or saved, so the Labor Budget, the Scheduler and Reports never
disagree, whichever was changed last.
"""
from statistics import median

from plant.models import STAGES, MO, Machine, Person, Shift
from plant.util import Catalog

STAGE_LABEL = {"s1": "Stage 1", "eb": "Stage 2", "ac": "Stage 3", "gb": "Glue Bead"}
SUITE_STAGES = set(STAGE_LABEL.values())
MAX_SHIFTS = 3
PALETTE = ["#2563eb", "#059669", "#7c3aed", "#d97706", "#dc2626", "#0891b2"]


def _hm(t):
    try:
        h, m = str(t).split(":")
        return int(h) * 60 + int(m)
    except (ValueError, TypeError):
        return 0


def line_rate(m, cat):
    """A machine's speed for the budget, in filters per hour, and how it was worked out."""
    if m.stage == "s1":
        pleats = []
        for mo in MO.objects.filter(line=m, status="OPEN")[:200]:
            it = cat.item_for(mo)
            if it and it.get("pleats"):
                pleats.append(float(it["pleats"]))
        basis = "open MOs on this line"
        if not pleats:
            pleats = [float(i["pleats"]) for i in cat.items if i.get("pleats")]
            basis = "every part on the cost sheet"
        if not pleats:
            return 0, "No pleat counts on the cost sheet yet"
        mid = median(pleats)
        return round(m.ppm * 60 / mid), f"{m.ppm:g} PPM × 60 ÷ {mid:g} pleats (median of {basis})"
    return round(m.rate or 0), f"{m.rate or 0:g} filters/hr (the line's rate in the Scheduler)"


def suite_view():
    """What the suite has, for the plan and for the Labor Budget page (no wages, no PINs)."""
    cat = Catalog()
    shifts = list(Shift.objects.prefetch_related("breaks"))
    num = {s.id: i + 1 for i, s in enumerate(shifts)}
    machines = []
    for m in Machine.objects.prefetch_related("shifts"):
        rate, basis = line_rate(m, cat)
        machines.append({"id": m.id, "name": m.name, "stage": STAGE_LABEL.get(m.stage, "Stage 1"), "color": m.color, "active": m.active,
                         "rate": rate, "rateBasis": basis, "downtime": m.downtime_pct, "scrap": m.scrap_pct, "shifts": sorted(num[s.id] for s in m.shifts.all() if s.id in num)})
    out_shifts = []
    for s in shifts[:MAX_SHIFTS]:
        lunch = sum((_hm(b.end) - _hm(b.start)) % 1440 for b in s.breaks.all() if "lunch" in (b.label or "").lower())
        out_shifts.append({"n": num[s.id], "id": s.id, "name": s.name, "start": s.start, "end": s.end,
                           "days": sorted(int(d) for d in (s.days or []) if str(d).isdigit() and 0 <= int(d) <= 6), "lunch": lunch})
    people = [{"name": p["name"], "role": p["role"]} for p in Person.objects.filter(active=True).values("name", "role")]
    return {"machines": machines, "shifts": out_shifts, "extraShifts": max(0, len(shifts) - MAX_SHIFTS), "people": people,
            "stages": [lbl for _, lbl in STAGES]}


def merge(data, view=None):
    """The plan with the suite's lines, speeds, shifts and work week in it. Returns (plan, notes)."""
    if not isinstance(data, dict):
        return data, []
    view = view or suite_view()
    notes = []
    lines = [ln for ln in (data.get("lines") or []) if isinstance(ln, dict)]
    by_id = {ln.get("machineId"): ln for ln in lines if ln.get("machineId")}
    by_name = {str(ln.get("name") or "").strip().lower(): ln for ln in lines if not ln.get("machineId")}
    shift_nums = [s["n"] for s in view["shifts"]] or [1]
    linked, used = [], set()
    for i, m in enumerate(view["machines"]):
        ln = by_id.get(m["id"]) or by_name.get(m["name"].strip().lower())
        if ln is None or id(ln) in used:
            ln = {"id": f"LM{m['id']}", "shiftSettings": {}, "shiftPositions": {}, "shiftSchedules": {}}
            notes.append(f"Added line {m['name']} from the suite.")
        used.add(id(ln))
        ln.update({"machineId": m["id"], "name": m["name"], "stage": m["stage"], "color": m["color"] if m["color"].startswith("#") else PALETTE[i % 6],
                   "hourlyRate": m["rate"], "rateBasis": m["rateBasis"], "inService": m["active"]})
        ln.pop("suiteMissing", None)
        runs = set(m["shifts"])
        ln["activeShifts"] = {str(n): (n in runs and m["active"]) for n in shift_nums}
        for n in shift_nums:
            k = str(n)
            ln.setdefault("shiftSettings", {}).setdefault(k, {"downtimePct": m["downtime"], "scrapPct": m["scrap"]})   # the Scheduler's to start
            ln.setdefault("shiftPositions", {}).setdefault(k, [{"id": f"P{n}_{ln['id']}", "title": _first_title(data), "reqCount": 1}])
            ln.setdefault("shiftSchedules", {}).setdefault(k, f"S{n}A")
        linked.append(ln)
    others = []
    known = {m["id"] for m in view["machines"]}
    for ln in lines:
        if id(ln) in used:
            continue
        if ln.get("machineId") and ln["machineId"] not in known:
            if not ln.get("suiteMissing"):
                notes.append(f"{ln.get('name')} is no longer in the suite: move its crew, then delete it.")
            ln["suiteMissing"], ln["machineId"] = True, None
        elif not ln.get("machineId") and ln.get("stage") in SUITE_STAGES and not ln.get("suiteMissing"):
            # a production line the suite doesn't have: say so, keep it (it might be a planned line)
            ln["suiteMissing"] = True
        others.append(ln)
    data["lines"] = linked + others

    # shifts and the main crew schedule of each
    if view["shifts"]:
        data["shiftCount"] = min(MAX_SHIFTS, len(view["shifts"]))
        scheds = [s for s in (data.get("schedules") or []) if isinstance(s, dict)]
        for s in view["shifts"]:
            a = next((x for x in scheds if x.get("suiteShift") == s["id"]), None) or \
                next((x for x in scheds if x.get("shift") == s["n"] and str(x.get("code") or "").upper() == "A"), None)
            if a is None:
                a = {"id": f"S{s['n']}A", "shift": s["n"], "code": "A", "name": s["name"], "paidLunch": False, "enabled": True}
                scheds.append(a)
            a.update({"shift": s["n"], "code": "A", "suiteShift": s["id"], "suiteName": s["name"], "startTime": s["start"], "endTime": s["end"],
                      "daysOfWeek": s["days"], "lunchMins": s["lunch"], "enabled": True})
        ids = {s["id"] for s in view["shifts"]}
        for x in scheds:   # the main schedule of a shift the suite no longer has
            if x.get("suiteShift") and x["suiteShift"] not in ids:
                x.pop("suiteShift", None)
                x["enabled"] = False
        data["schedules"] = scheds
        days = sorted({d for s in view["shifts"] for d in s["days"]})
        if days and isinstance(data.get("calendar"), dict):   # (a new plan gets its calendar from the page, then this)
            data["calendar"]["defaultWorkDays"] = days

    # people on crew slots who aren't on the Team list
    team = {p["name"].strip().lower() for p in view["people"]}
    slots = data.get("slotAssignments") if isinstance(data.get("slotAssignments"), dict) else {}
    unknown = sorted({str(v.get("name")).strip() for v in slots.values() if isinstance(v, dict) and str(v.get("name") or "").strip()
                      and str(v.get("name")).strip().lower() not in team})
    data["suite"] = {"people": sorted(p["name"] for p in view["people"]), "notOnTeam": unknown, "extraShifts": view["extraShifts"]}
    return data, notes


def save_line_shifts(data, by=""):
    """The shifts the Labor Budget turned on or off for its suite lines, saved on the machines (the Scheduler
    plans with them). Returns what changed, for the audit log."""
    if not isinstance(data, dict):
        return []
    shifts = list(Shift.objects.all()[:MAX_SHIFTS])
    by_num = {i + 1: s for i, s in enumerate(shifts)}
    machines = {m.id: m for m in Machine.objects.prefetch_related("shifts")}
    names = {m.name.strip().lower(): m for m in machines.values()}
    changed = []
    for ln in data.get("lines") or []:
        if not isinstance(ln, dict) or not isinstance(ln.get("activeShifts"), dict):
            continue
        m = machines.get(ln.get("machineId")) or (None if ln.get("machineId") else names.get(str(ln.get("name") or "").strip().lower()))
        if not m or not m.active:   # an out-of-service machine shows every shift off; that isn't a change
            continue
        have = {s.id for s in m.shifts.all()}
        want = set(have)
        for n, s in by_num.items():
            v = ln["activeShifts"].get(str(n), ln["activeShifts"].get(n))
            if v is True:
                want.add(s.id)
            elif v is False:
                want.discard(s.id)
        if want != have:
            m.shifts.set(want)
            on = [s.name for n, s in by_num.items() if s.id in want]
            changed.append(f"{m.name} runs {', '.join(on) if on else 'no shifts'}")
    return changed


def _first_title(data):
    for p in data.get("positionsDefaults") or []:
        if isinstance(p, dict) and p.get("title"):
            return p["title"]
    return "Operator"


def rename_machine(old, new):
    """A machine was renamed: the plan's line keeps its crew (it is linked by machineId; this covers a plan
    saved before lines were linked)."""
    from .models import LaborPlan
    plan = LaborPlan.objects.filter(pk=1).first()
    if not plan or not isinstance(plan.data, dict):
        return
    changed = False
    for ln in plan.data.get("lines") or []:
        if isinstance(ln, dict) and not ln.get("machineId") and str(ln.get("name") or "").strip().lower() == old.strip().lower():
            ln["name"] = new
            changed = True
    if changed:
        plan.save(update_fields=["data"])
