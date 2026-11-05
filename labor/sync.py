"""Keeps the Labor Budget plan and the rest of the suite in step.

Budget A of the Labor Budget is the master of the lines and the shifts (`apply_to_suite`, run when it's saved):
- Shifts: how many there are (up to 3), each one's start, end and work days (its main crew schedule, code A) and
  its lunch length. When lunch starts, other breaks and shift names stay the suite's (the plan has no times for them).
- Lines: a production line (Stage 1, Stage 2, Stage 3, Glue Bead) is a machine. Adding one adds the machine,
  renaming it renames the machine (its records follow), deleting it takes the machine out of service (records
  kept), and which shifts it runs and whether it runs through lunch are set there. Labor-only lines (Packaging
  Only, Non Production, Backup) aren't machines.
Admin › Machines, Admin › Shifts & breaks and the Scheduler's Lines & Shifts show these and can't change them
(`master_on`). Machine settings stay the suite's and are put into the plan by `merge()` whenever it is read or
saved: each line's color and speed, and machines added in Admin before or outside the plan. Crew names are
offered from the Team list; wages stay in the plan (shift leads only).
"""
from statistics import median

from plant.models import STAGES, MO, Machine, Person, Shift
from plant.util import Catalog

from .plan import _num

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
    known = {m["id"] for m in view["machines"]}
    by_id = {ln.get("machineId"): ln for ln in lines if ln.get("machineId") in known}
    # a line not linked yet, or linked to a machine that's gone (e.g. after restoring a backup), is matched by name
    by_name = {str(ln.get("name") or "").strip().lower(): ln for ln in lines if ln.get("machineId") not in known}
    shift_nums = [s["n"] for s in view["shifts"]] or [1]
    linked, used = [], set()
    for i, m in enumerate(view["machines"]):
        ln = by_id.get(m["id"]) or by_name.get(m["name"].strip().lower())
        if ln is None and not m["active"]:
            continue   # out of service and not in the plan (e.g. deleted in the Labor Budget): don't bring it back
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


STAGE_CODE = {v: k for k, v in STAGE_LABEL.items()}


def master_on():
    """True once the plant's Labor Budget (budget A) has been saved: from then on it is the master of the lines,
    the shifts and their times, and Admin and the Scheduler mirror it."""
    from .models import BUDGETS, LaborPlan
    plan = LaborPlan.objects.filter(pk=BUDGETS["A"]).first()
    return bool(plan and plan.version and isinstance(plan.data, dict) and plan.data.get("lines"))


def _ordinal(n):
    return f"{n}{'st' if n == 1 else 'nd' if n == 2 else 'rd' if n == 3 else 'th'}"


def _hhmm(v, default):
    v = str(v or "").strip()
    return v if len(v) == 5 and v[2] == ":" and v[:2].isdigit() and v[3:].isdigit() and int(v[:2]) < 24 and int(v[3:]) < 60 else default


def apply_to_suite(data, old=None):
    """The Labor Budget (budget A) is the master of lines and shifts: put its shifts (how many, start, end, work
    days, lunch length), its production lines (added, renamed, removed, which shifts each runs, running through
    lunch) into the suite. Machine settings the plan doesn't have (speeds, feeds, colors, training, breaks other
    than lunch, when lunch starts) stay the suite's. Returns (data with new lines linked by machineId, changes).
    A line is only taken out of service when it was in the plan saved before (`old`) and has been deleted since."""
    from plant.models import ShiftBreak

    from hmi.models import ShiftRun
    if not isinstance(data, dict):
        return data, []
    changes = []
    # ---- shifts: the plan's shift k is the suite's k-th shift
    scheds = [x for x in (data.get("schedules") or []) if isinstance(x, dict)]
    suite = list(Shift.objects.prefetch_related("breaks").order_by("sort", "id"))
    if data.get("shiftCount") in (None, ""):   # a plan without a shift count leaves the suite's shifts as they are
        count = max([len(suite)] + [int(_num(x.get("shift"))) for x in scheds if _num(x.get("shift"))])
    else:
        count = int(_num(data.get("shiftCount")) or 1)
    count = max(1, min(MAX_SHIFTS, count))
    by_num = {}
    for k in range(1, count + 1):
        a = next((x for x in scheds if x.get("shift") == k and str(x.get("code") or "").upper() == "A"), None) or \
            next((x for x in scheds if x.get("shift") == k), None)
        sh = suite[k - 1] if k - 1 < len(suite) else None
        if a is None:
            if sh:
                by_num[k] = sh
            continue
        start, end = _hhmm(a.get("startTime"), sh.start if sh else "06:00"), _hhmm(a.get("endTime"), sh.end if sh else "14:30")
        if isinstance(a.get("daysOfWeek"), list):   # a field the plan doesn't have leaves the suite's value alone
            days = sorted({int(d) for d in a["daysOfWeek"] if str(d).isdigit() and 0 <= int(d) <= 6})
        else:
            days = sorted(sh.days or []) if sh else [1, 2, 3, 4, 5]
        if sh is None:
            sh = Shift.objects.create(name=str(a.get("suiteName") or f"{_ordinal(k)} shift")[:60], start=start, end=end, days=days, sort=k - 1)
            changes.append(f"added {sh.name} {start}–{end}")
        elif (sh.start, sh.end, sorted(sh.days or [])) != (start, end, days):
            changes.append(f"{sh.name} set to {start}–{end}")
            sh.start, sh.end, sh.days = start, end, days
            sh.save(update_fields=["start", "end", "days"])
        by_num[k] = sh
        # lunch: its length comes from the plan; when it starts stays the suite's (the plan has no time for it)
        if a.get("lunchMins") in (None, ""):
            continue
        mins = max(0, int(_num(a.get("lunchMins"))))
        lunch = next((b for b in sh.breaks.all() if "lunch" in (b.label or "").lower()), None)
        if not mins and lunch:
            lunch.delete()
            changes.append(f"{sh.name}: no lunch")
        elif mins:
            if lunch:
                b0 = _hm(lunch.start)
            else:
                s0, e0 = _hm(start), _hm(end)
                length = (e0 - s0) % 1440 or 1440
                b0 = (s0 + max(0, (length - mins) // 2) // 15 * 15) % 1440
                lunch = ShiftBreak(shift=sh, label="Lunch")
            new_end = f"{(b0 + mins) // 60 % 24:02d}:{(b0 + mins) % 60:02d}"
            if lunch.pk is None or lunch.end != new_end:
                lunch.start, lunch.end = f"{b0 // 60:02d}:{b0 % 60:02d}", new_end
                lunch.save()
                changes.append(f"{sh.name}: lunch {mins} min")
    for sh in suite[count:]:
        if not ShiftRun.objects.filter(active=True, shift_name=sh.name).exists():
            changes.append(f"removed {sh.name}")
            sh.delete()
    # ---- lines
    old_ids = {ln.get("machineId") for ln in ((old or {}).get("lines") or []) if isinstance(ln, dict) and ln.get("machineId")}
    machines = {m.id: m for m in Machine.objects.prefetch_related("shifts")}
    by_name = {m.name.strip().lower(): m for m in machines.values()}
    seen = set()
    sort = 0
    for ln in data.get("lines") or []:
        if not isinstance(ln, dict):
            continue
        name = " ".join(str(ln.get("name") or "").split())[:60]
        if not name:
            continue
        m = machines.get(ln.get("machineId")) or (None if ln.get("machineId") in machines else by_name.get(name.lower()))
        if m is not None and m.id in seen:
            m = None
        if ln.get("stage") in STAGE_CODE:
            stage = STAGE_CODE[ln["stage"]]
        elif not ln.get("stage") and m is not None:
            stage = m.stage   # a plan saved before lines had stages: the machine keeps its own
        else:
            continue   # labor-only lines (packaging, non-production, backup) aren't machines
        if m is None:
            if name.lower() in by_name and by_name[name.lower()].id in seen:
                changes.append(f"two lines are called {name}; the second was left out")
                continue
            m = Machine.objects.create(name=name, stage=stage, color=str(ln.get("color") or PALETTE[sort % len(PALETTE)])[:20],
                                       ppm=180, rate=float(_num(ln.get("hourlyRate")) or 300), speed_mode="fixed", sort=sort)
            machines[m.id] = m
            changes.append(f"added line {name}")
        else:
            upd = []
            if m.name != name and not Machine.objects.filter(name__iexact=name).exclude(pk=m.pk).exists():
                from hmi.engine import rename_machine_records
                rename_machine_records(m.name, name)
                changes.append(f"renamed {m.name} to {name}")
                m.name = name
                upd.append("name")
            if m.stage != stage:
                if ShiftRun.objects.filter(machine=m, active=True).exists():
                    changes.append(f"{m.name} has a shift running, so its stage wasn't changed")
                else:
                    m.stage = stage
                    upd.append("stage")
            if not m.active and m.id not in old_ids:   # put back in the Labor Budget after being deleted there
                m.active = True
                upd.append("active")
                changes.append(f"{m.name} back in service")
            if m.sort != sort:
                m.sort = sort
                upd.append("sort")
            if upd:
                m.save(update_fields=upd)
        ln["machineId"] = m.id
        seen.add(m.id)
        sort += 1
        # which shifts it runs, and running through lunch where the plan says so
        act = ln.get("activeShifts") if isinstance(ln.get("activeShifts"), dict) else {}
        want = {by_num[n].id for n in by_num if act.get(str(n), act.get(n)) is True}
        have = {s.id for s in m.shifts.all()}
        if want != have:
            m.shifts.set(want)
            on = [by_num[n].name for n in sorted(by_num) if by_num[n].id in want]
            changes.append(f"{m.name} runs {', '.join(on) if on else 'no shifts'}")
        sset = ln.get("shiftSettings") if isinstance(ln.get("shiftSettings"), dict) else {}
        flags = [(sset.get(str(n)) or {}).get("runLunch") for n in by_num if act.get(str(n), act.get(n)) is True]
        flags = [f for f in flags if f is not None]
        if flags and bool(any(flags)) != m.run_lunch:
            m.run_lunch = bool(any(flags))
            m.save(update_fields=["run_lunch"])
            changes.append(f"{m.name} {'runs through' if m.run_lunch else 'stops for'} lunch")
    for mid in old_ids - seen:   # deleted in the Labor Budget since it was last saved
        m = machines.get(mid)
        if m and m.active:
            if ShiftRun.objects.filter(machine=m, active=True).exists():
                changes.append(f"{m.name} has a shift running, so it stays in service")
                continue
            m.active = False
            m.save(update_fields=["active"])
            changes.append(f"{m.name} taken out of service (deleted in the Labor Budget)")
    return data, changes


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
    for plan in LaborPlan.objects.all():   # budget A and budget B
        if not isinstance(plan.data, dict):
            continue
        changed = False
        for ln in plan.data.get("lines") or []:
            if isinstance(ln, dict) and not ln.get("machineId") and str(ln.get("name") or "").strip().lower() == old.strip().lower():
                ln["name"] = new
                changed = True
        if changed:
            plan.save(update_fields=["data"])
