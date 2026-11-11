"""Keeps the Labor Budget plan and the rest of the suite in step.

The suite's lines (Admin › Machines, the Scheduler's Lines & Shifts) are their own: the Labor Budget is a plan,
not the master.
- A production line (Stage 1, Stage 2, Stage 3, Glue Bead) added in budget A starts the suite's machine with the
  budget's shifts and running-through-lunch (per shift) when the budget is saved (`apply_to_suite`). After that
  the budget never changes a machine: a line's name, stage, shifts, lunch and being in service are set in the
  suite, and `budget_diff()` says where the budget differs (shown on Lines & Shifts). A line deleted in the budget
  stays in service; it shows as not in the Labor Budget. Labor-only lines (Packaging Only, Non Production,
  Backup) aren't machines.
- The shifts (times, days, lunch and authorized downtimes) belong to Admin › Machine schedule.
- `merge()` puts the suite into the plan whenever it is read or saved: each line's name, stage, color and speed,
  and machines added in the suite (with their shifts and lunch to start with). The plan's own shifts and lunch
  per line stay the plan's. Crew names are offered from the Team list; wages stay in the plan (shift leads only).
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
                         "rate": rate, "rateBasis": basis, "downtime": m.downtime_pct, "scrap": m.scrap_pct, "shifts": sorted(num[s.id] for s in m.shifts.all() if s.id in num),
                         "lunch": sorted(num[i] for i in (m.lunch_shifts or []) if i in num)})
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
    removed = set(data.get("suiteRemoved") or [])   # machines deleted from the budget: they stay in the suite, not in the plan
    for i, m in enumerate(view["machines"]):
        ln = by_id.get(m["id"]) or by_name.get(m["name"].strip().lower())
        if ln is None and (not m["active"] or m["id"] in removed):
            continue   # out of service, or deleted in the Labor Budget: don't bring it back into the plan
        fresh = ln is None or id(ln) in used
        if fresh:
            ln = {"id": f"LM{m['id']}", "shiftSettings": {}, "shiftPositions": {}, "shiftSchedules": {}}
            notes.append(f"Added line {m['name']} from the suite.")
        used.add(id(ln))
        ln.update({"machineId": m["id"], "name": m["name"], "stage": m["stage"], "color": m["color"] if m["color"].startswith("#") else PALETTE[i % 6],
                   "hourlyRate": m["rate"], "rateBasis": m["rateBasis"], "inService": m["active"]})
        ln.pop("suiteMissing", None)
        # which shifts it runs and running through lunch are the plan's own; a line new to the plan starts from the suite
        act = ln.get("activeShifts") if isinstance(ln.get("activeShifts"), dict) else {}
        for n in shift_nums:
            if fresh or str(n) not in act:
                act[str(n)] = n in m["shifts"] and m["active"]
        ln["activeShifts"] = act
        for n in shift_nums:
            k = str(n)
            ln.setdefault("shiftSettings", {}).setdefault(k, {"downtimePct": m["downtime"], "scrapPct": m["scrap"]})   # the Scheduler's to start
            if fresh or "runLunch" not in ln["shiftSettings"][k]:
                ln["shiftSettings"][k]["runLunch"] = n in m["lunch"]
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
    """A production line new in the Labor Budget (budget A) starts a machine in the suite, with the budget's shifts
    and running through lunch per shift. Lines already in the suite are only linked (by machineId): the budget never
    changes them (`budget_diff()` says where it differs). A line deleted from the budget since it was last saved
    (`old`) is remembered in data["suiteRemoved"], so the plan doesn't bring it back; the machine stays in service.
    Returns (data, changes)."""
    if not isinstance(data, dict):
        return data, []
    changes = []
    suite = list(Shift.objects.order_by("sort", "id"))
    by_num = {k + 1: sh for k, sh in enumerate(suite[:MAX_SHIFTS])}
    old_ids = {ln.get("machineId") for ln in ((old or {}).get("lines") or []) if isinstance(ln, dict) and ln.get("machineId")}
    machines = {m.id: m for m in Machine.objects.all()}
    by_name = {m.name.strip().lower(): m for m in machines.values()}
    removed = set(data.get("suiteRemoved") or (old or {}).get("suiteRemoved") or [])
    seen = set()
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
        elif m is not None:
            stage = m.stage
        else:
            continue   # labor-only lines (packaging, non-production, backup) aren't machines
        if m is None:
            if name.lower() in by_name:
                changes.append(f"two lines are called {name}; the second was left out")
                continue
            act = ln.get("activeShifts") if isinstance(ln.get("activeShifts"), dict) else {}
            sset = ln.get("shiftSettings") if isinstance(ln.get("shiftSettings"), dict) else {}
            runs = [n for n in by_num if act.get(str(n), act.get(n)) is True]
            m = Machine.objects.create(name=name, stage=stage, color=str(ln.get("color") or PALETTE[len(machines) % len(PALETTE)])[:20],
                                       ppm=180, rate=float(_num(ln.get("hourlyRate")) or 300), speed_mode="fixed", sort=len(machines),
                                       lunch_shifts=[by_num[n].id for n in runs if (sset.get(str(n)) or {}).get("runLunch")])
            m.shifts.set([by_num[n].id for n in runs])
            machines[m.id] = m
            by_name[name.lower()] = m
            changes.append(f"added line {name} from the Labor Budget (" + (", ".join(by_num[n].name for n in runs) or "no shifts") + ")")
        removed.discard(m.id)
        ln["machineId"] = m.id
        seen.add(m.id)
    for mid in old_ids - seen:   # deleted in the Labor Budget since it was last saved: the suite keeps it
        if mid in machines:
            removed.add(mid)
            changes.append(f"{machines[mid].name} was deleted in the Labor Budget; it stays in the suite")
    data["suiteRemoved"] = sorted(removed)
    return data, changes


def budget_diff():
    """Where the Labor Budget (budget A) differs from the suite's lines: {machine id: [what differs, ...]}.
    Empty when there's no saved budget."""
    from .models import BUDGETS, LaborPlan
    plan = LaborPlan.objects.filter(pk=BUDGETS["A"]).first()
    if not (plan and plan.version and isinstance(plan.data, dict)):
        return {}
    data = plan.data
    suite = list(Shift.objects.order_by("sort", "id"))
    by_num = {k + 1: sh for k, sh in enumerate(suite[:MAX_SHIFTS])}
    lines = {ln.get("machineId"): ln for ln in data.get("lines") or [] if isinstance(ln, dict) and ln.get("machineId")}
    removed = set(data.get("suiteRemoved") or [])
    out = {}
    for m in Machine.objects.filter(active=True).prefetch_related("shifts"):
        ln = lines.get(m.id)
        if ln is None:
            if m.id in removed:
                out[m.id] = ["Not in the Labor Budget (deleted there)"]
            continue
        notes = []
        act = ln.get("activeShifts") if isinstance(ln.get("activeShifts"), dict) else {}
        sset = ln.get("shiftSettings") if isinstance(ln.get("shiftSettings"), dict) else {}
        have = {s.id for s in m.shifts.all()}
        want = [n for n in by_num if act.get(str(n), act.get(n)) is True]
        if {by_num[n].id for n in want} != have & {s.id for s in by_num.values()}:
            names = [by_num[n].name for n in want]
            notes.append("Labor Budget: " + (f"{names[0]} only" if len(names) == 1 else " and ".join(names) if names else "no shifts"))
        for n in want:
            sh = by_num[n]
            if sh.id not in have:
                continue
            b = bool((sset.get(str(n)) or {}).get("runLunch"))
            if b != (sh.id in (m.lunch_shifts or [])):
                notes.append(f"Labor Budget {'runs through' if b else 'stops for'} lunch on {sh.name}")
        if notes:
            out[m.id] = notes
    return out


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
