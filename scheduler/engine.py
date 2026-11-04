"""Scheduling engine.

Takes the jobs people placed on the board and works out when each one really runs:
- run time comes from the cost sheet rate for the part on that machine, slowed by the machine's
  downtime %, with extra pieces for its scrap %;
- a changeover is added when the part changes (and before the first run, if set);
- runs only use shift time, pausing through breaks and lunches;
- End Band, Arts & Crafts and Glue Bead may run alongside Stage 1 but are held back just enough
  that their share of the order never passes Stage 1's (they never run out of parts).

After any change, `refresh_floor_queue` writes each Stage 1 machine's run order onto the MOs,
which is what the Operator Kiosk shows as "Up next".
"""
import json
import math

from django.db import transaction

from plant.models import MO, STAGE_ORDER, Machine, PlantSettings, Shift
from plant.util import DAY, HOUR, MIN, Catalog, add_days_ms, at_local_minutes, from_ms, hm, is_lunch, local, ms, now, sod_ms

from .models import Job

STAGE_NAMES = {"s1": "Stage 1", "eb": "End Band", "ac": "Arts & Crafts", "gb": "Glue Bead"}


def fmt_dt(t):
    d = local(from_ms(t))
    return d.strftime("%a ") + f"{d.month}/{d.day} {d:%H:%M}"


def fmt_n(n):
    return f"{round(n):,}"


# ------------------------------------------------------------------ world snapshot
def load_world(jobs_override=None):
    st = PlantSettings.load()
    shifts = []
    for sh in Shift.objects.prefetch_related("breaks"):
        shifts.append({"id": sh.id, "name": sh.name, "start": sh.start, "end": sh.end, "days": list(sh.days or []),
                       "breaks": [{"label": b.label, "start": b.start, "end": b.end} for b in sh.breaks.all()]})
    machines = []
    for m in Machine.objects.prefetch_related("shifts"):
        machines.append({"id": m.id, "name": m.name, "stage": m.stage, "mode": m.speed_mode, "ppm": m.ppm, "rate": m.rate,
                         "scrap": m.scrap_pct, "downtime": m.downtime_pct, "changeover": m.changeover_min, "runLunch": m.run_lunch,
                         "shifts": [s.id for s in m.shifts.all()], "active": m.active, "color": m.color, "code": m.code})
    # Open orders take time on the board. A closed MO has finished Stage 1 at the pleater: it no longer holds a
    # Stage 1 slot, but any End Band / Arts & Crafts / Glue Bead work already on the board stays scheduled.
    from django.db.models import Q
    mos = {}
    later = Job.objects.filter(op__in=["eb", "ac", "gb"]).values("mo_id")
    for mo in MO.objects.filter(Q(status="OPEN") | Q(status="CLOSE", pk__in=later)):
        mos[mo.id] = {"id": mo.id, "mo": mo.number, "part": mo.part, "qty": mo.qty, "due": ms_from_date(mo.due), "pleats": mo.pleats,
                      "ops": mo.ops or {"s1": True}, "itemUid": mo.item_uid, "status": mo.status, "produced": mo.produced,
                      "example": mo.example}
    if jobs_override is not None:
        jobs = jobs_override
    else:
        jobs = {j.key: {"id": j.key, "moId": j.mo_id, "op": j.op, "machineId": j.machine_id, "reqStart": ms(j.req_start)}
                for j in Job.objects.all()}
    from hmi.models import ShiftRun  # local import: hmi depends on scheduler
    running = {r["mo_id"]: r["machine_id"] for r in ShiftRun.objects.filter(active=True, mo__isnull=False, machine__isnull=False).values("mo_id", "machine_id")}
    cfg = {"shifts": shifts, "machines": machines, "coSamePart": st.planner.get("coSamePart", False),
           "coFirst": st.planner.get("coFirst", True), "useLabor": st.planner.get("useLabor", True) is not False}
    # The Labor Budget plan: work calendar, which shifts each line has a crew for, and crew cost (labor/plan.py).
    from labor.plan import LaborPlanView, machine_labor
    plan = LaborPlanView.current() if cfg["useLabor"] else None
    return {"cfg": cfg, "labor": plan, "laborLines": machine_labor(plan, cfg),
            "mos": mos, "jobs": jobs, "catalog": Catalog(), "running": running, "now": ms(now())}


def config_sig(cfg):
    """Fingerprint of lines, shifts and planner options. A Lines & Shifts save must carry the fingerprint the
    screen loaded, so a screen that was left open can't undo changes made meanwhile on another screen."""
    import hashlib
    keep = {"machines": cfg["machines"], "shifts": cfg["shifts"], "coSamePart": cfg["coSamePart"], "coFirst": cfg["coFirst"],
            "useLabor": cfg.get("useLabor", True)}
    return hashlib.sha1(json.dumps(keep, sort_keys=True, default=str).encode()).hexdigest()


def ms_from_date(d):
    if not d:
        return None
    from datetime import datetime
    from django.utils import timezone
    return ms(timezone.make_aware(datetime(d.year, d.month, d.day)))


# ------------------------------------------------------------------ rates
def rate_for(m, item, mo):
    st = m["stage"]
    if st == "s1":
        pleats = (item and item.get("pleats")) or float(mo.get("pleats") or 0)
        if m["mode"] == "old":
            ppm = (item and item.get("ppm")) or m["ppm"]
        elif m["mode"] == "new":
            ppm = (item and item.get("nppm")) or m["ppm"]
        else:
            ppm = m["ppm"]
        if not pleats:
            return 0, "No pleat count for this part"
        return ppm * 60 / pleats, f"{fmt_n(ppm)} PPM × 60 ÷ {fmt_n(pleats)} pleats"
    r = float(m["rate"] or 0)
    basis = f"Fixed {fmt_n(r)}/hr"
    if st == "eb" and m["mode"] == "sheet" and item and item.get("s2"):
        r, basis = item["s2"], f"Cost sheet Stage 2: {fmt_n(item['s2'])}/hr"
    if st == "eb" and m["mode"] == "mask" and item and item.get("mask"):
        r, basis = item["mask"] * 30, f"Mask qty {fmt_n(item['mask'])} × 30 = {fmt_n(item['mask'] * 30)}/hr"
    if st == "ac" and m["mode"] == "sheet" and item and item.get("s3"):
        r, basis = item["s3"], f"Cost sheet Stage 3: {fmt_n(item['s3'])}/hr"
    if st != "gb" and m["mode"] != "fixed" and basis.startswith("Fixed"):
        basis = f"No sheet rate · line default {fmt_n(r)}/hr"
    return r, basis


# ------------------------------------------------------------------ calendar
class Calendar:
    """Shift time for each line. With a Labor Budget plan: no shifts on its holidays and days off, shifts on its
    extra work days, and only the shifts it has a crew for on that line."""

    def __init__(self, cfg, labor=None, labor_lines=None):
        self.shifts = {s["id"]: s for s in cfg["shifts"]}
        self.labor = labor
        self.labor_lines = labor_lines or {}
        self.cache = {}

    def day_iv(self, m, day):
        return [(s, e) for s, e, _ in self.day_shifts(m, day)]

    def day_shifts(self, m, day):
        """(start, end, shift id) of each working stretch of shifts that start on this day."""
        key = (m["id"], day)
        if key in self.cache:
            return self.cache[key]
        dow = (local(from_ms(day)).weekday() + 1) % 7
        status, by_date = self.labor.day_status_ms(day) if self.labor else ("WORK", False)
        crews = (self.labor_lines.get(m["id"]) or {}).get("shifts")
        out = []
        for sid in ((m.get("shifts") or []) if status == "WORK" else []):
            sh = self.shifts.get(sid)
            if not sh or not (by_date or dow in (sh.get("days") or [])):
                continue
            if crews is not None and not (crews.get(sid) or {}).get("staffed"):
                continue  # the labor plan has no crew for this shift on this line
            s0 = hm(sh["start"])
            L = (hm(sh["end"]) - s0 + 1440) % 1440 or 1440
            segs = [(0, L)]
            for b in sh.get("breaks") or []:
                if m.get("runLunch") and is_lunch(b.get("label")):
                    continue   # this line keeps running through lunch
                bo = (hm(b["start"]) - s0 + 1440) % 1440
                bl = (hm(b["end"]) - hm(b["start"]) + 1440) % 1440
                if not bl or bo >= L:
                    continue
                be = min(L, bo + bl)
                nxt = []
                for a, z in segs:
                    if be <= a or bo >= z:
                        nxt.append((a, z))
                    else:
                        nxt += [x for x in ((a, bo), (be, z)) if x[1] > x[0]]
                segs = nxt
            for a, z in segs:
                out.append((at_local_minutes(day, s0 + a), at_local_minutes(day, s0 + z), sid))
        self.cache[key] = out
        return out

    def labor_cost(self, m, segs):
        """Crew wages for the shift time in segs, using each shift's crew cost per hour from the labor plan."""
        crews = (self.labor_lines.get(m["id"]) or {}).get("shifts")
        if not crews or not segs:
            return None
        total = 0.0
        a, b = segs[0][0], segs[-1][1]
        d = add_days_ms(sod_ms(a), -1)
        while d < b:
            for s, e, sid in self.day_shifts(m, d):
                rate = (crews.get(sid) or {}).get("costPerHour") or 0
                if not rate:
                    continue
                for x, y in segs:
                    ov = min(e, y) - max(s, x)
                    if ov > 0:
                        total += ov / HOUR * rate
            d = add_days_ms(d, 1)
        return round(total, 2)

    def between(self, m, a, b):
        iv = []
        d = add_days_ms(sod_ms(a), -1)
        while d < b:
            iv += self.day_iv(m, d)
            d = add_days_ms(d, 1)
        iv = sorted(((max(s, a), min(e, b)) for s, e in iv if min(e, b) > max(s, a)))
        out = []
        for s, e in iv:
            if out and s <= out[-1][1]:
                out[-1][1] = max(out[-1][1], e)
            else:
                out.append([s, e])
        return [tuple(x) for x in out]

    def consume(self, m, frm, dur):
        if not m.get("shifts"):
            return None
        rem, cur, segs = dur, frm, []
        for _ in range(110):
            if rem <= 0:
                break
            end = add_days_ms(cur, 7)
            for s, e in self.between(m, cur, end):
                if rem <= 0:
                    break
                take = min(e - s, rem)
                segs.append((s, s + take))
                rem -= take
            cur = end
        return None if rem > 0 else segs


def split_segs(segs, dur):
    a, b, rem = [], [], dur
    for s, e in segs:
        if rem <= 0:
            b.append((s, e))
            continue
        take = min(e - s, rem)
        if take > 0:
            a.append((s, s + take))
        if e - s > take:
            b.append((s + take, e))
        rem -= take
    return a, b


def frac_at(segs, t):
    tot = done = 0
    for a, b in segs:
        tot += b - a
        if t > a:
            done += min(b, t) - a
    return done / tot if tot else 1


def keeps_up(down, up):
    if not down:
        return True
    a, b = down[0][0], down[-1][1]
    pts = []
    for x, y in down:
        pts += [x, y]
    for x, y in up:
        if a < x < b:
            pts.append(x)
        if a < y < b:
            pts.append(y)
    return all(frac_at(down, p) <= frac_at(up, p) + 1e-9 for p in pts)


def compute(world, jobs=None):
    cfg = world["cfg"]
    jobs = world["jobs"] if jobs is None else jobs
    mos, cat = world["mos"], world["catalog"]
    cal = Calendar(cfg, world.get("labor"), world.get("laborLines"))
    running = world.get("running", {})
    t_now = world.get("now") or ms(now())
    res, by_m, s1_res, s1_done = {}, {}, {}, set()
    for st in STAGE_ORDER:
        for m in [x for x in cfg["machines"] if x["stage"] == st]:
            # The MO running on a Stage 1 machine right now goes first, from now, with what is left to make.
            lst = sorted((j for j in jobs.values() if j["machineId"] == m["id"] and j["moId"] in mos),
                         key=lambda j: (0 if st == "s1" and running.get(j["moId"]) == m["id"] else 1, j["reqStart"], str(j["id"])))
            by_m[m["id"]] = []
            prev_end, prev_part = -math.inf, None
            for j in lst:
                mo = mos[j["moId"]]
                item = cat.item_for(mo)
                rate, basis = rate_for(m, item, mo)
                warn = []
                is_running = st == "s1" and running.get(mo["id"]) == m["id"]
                earliest = max(t_now, prev_end) if is_running else max(j["reqStart"], prev_end)
                waiting, up = False, None
                if st == "s1":
                    qty_left = 0 if mo["status"] != "OPEN" else max(0, float(mo["qty"] or 0) - float(mo.get("produced") or 0))
                    if qty_left <= 0:
                        s1_done.add(mo["id"])  # Stage 1 already made the full quantity
                        continue
                elif mo["id"] not in s1_done:
                    up = s1_res.get(mo["id"])
                    if not up:
                        waiting = True
                        warn.append("Stage 1 for this order is not scheduled yet")
                pkey = item["uid"] if item else str(mo["part"]).upper()
                co_min = float(m["changeover"] or 0) if ((prev_part is None and cfg.get("coFirst") is not False) or
                                                        (prev_part is not None and (prev_part != pkey or cfg.get("coSamePart")))) else 0
                if is_running:
                    co_min = 0  # already set up and running
                scrap = min(.95, max(0, float(m["scrap"] or 0) / 100))
                dt = min(.95, max(0, float(m["downtime"] or 0) / 100))
                qty = qty_left if st == "s1" else float(mo["qty"] or 0)
                gross = math.ceil(qty / (1 - scrap))
                eff = rate * (1 - dt)
                if not rate:
                    warn.append("No run speed for this part on this line")
                run_min = gross / eff * 60 if eff > 0 else 0

                def plan(frm):
                    dur = (co_min + run_min) * MIN
                    if dur <= 0:
                        # Nothing to run (no speed known, or zero time): keep the job on the board as a
                        # zero-length slot at the next working moment instead of silently dropping it.
                        nxt = cal.consume(m, frm, 1)
                        if not nxt:
                            return None
                        return {"segs": [(nxt[0][0], nxt[0][0])], "co": [], "run": []}
                    sg = cal.consume(m, frm, dur)
                    if not sg:
                        return None
                    c, rn = split_segs(sg, co_min * MIN)
                    return {"segs": sg, "co": c, "run": rn}

                p = plan(earliest)
                if not p:
                    # Report it instead of dropping it: the board and the floor queue still show the job.
                    res[j["id"]] = {"id": j["id"], "moId": mo["id"], "op": st, "machineId": m["id"], "rate": rate, "basis": basis, "eff": 0,
                                    "gross": 0, "qty": float(mo["qty"] or 0), "scrap": 0, "dt": 0, "coMin": 0, "runMin": 0,
                                    "start": j["reqStart"], "runStart": j["reqStart"], "end": j["reqStart"], "segs": [], "coSegs": [], "runSegs": [],
                                    "warn": warn + [("The Labor Budget plan has no crew on this line's shifts, so this run can't be timed"
                                                     if m["id"] in world.get("laborLines", {}) else
                                                     "This line has no shifts assigned, so this run can't be timed")], "waiting": waiting,
                                    "paced": False, "overlap": False, "pushed": False, "late": False, "reqStart": j["reqStart"], "running": is_running,
                                    "unplanned": True}
                    by_m[m["id"]].append(j["id"])
                    continue
                paced = False
                if up and up["runSegs"] and not keeps_up(p["run"], up["runSegs"]):
                    lo, hi = earliest, max(earliest, up["end"])
                    best = plan(hi)
                    while hi - lo > MIN:
                        mid = lo + max(MIN, ((hi - lo) // 2 // MIN) * MIN)
                        q = plan(mid)
                        if q and keeps_up(q["run"], up["runSegs"]):
                            hi, best = mid, q
                        else:
                            lo = mid
                    if best:
                        p, paced = best, True
                segs, run_segs = p["segs"], p["run"]
                run_start = (run_segs[0] if run_segs else segs[0])[0]
                out = {"id": j["id"], "moId": mo["id"], "op": st, "machineId": m["id"], "rate": rate, "basis": basis, "eff": eff,
                       "gross": gross, "qty": qty, "scrap": scrap, "dt": dt, "coMin": co_min, "runMin": run_min,
                       "start": segs[0][0], "runStart": run_start, "end": segs[-1][1], "segs": segs, "coSegs": p["co"],
                       "runSegs": run_segs, "warn": warn, "waiting": waiting, "paced": paced,
                       "overlap": bool(up and run_start < up["end"]), "pushed": segs[0][0] - j["reqStart"] > MIN, "late": False,
                       "reqStart": j["reqStart"], "running": is_running}
                if is_running:
                    out["pushed"] = False
                cost = cal.labor_cost(m, segs)
                if cost is not None:
                    out["laborCost"] = cost
                if out["pushed"] and not waiting:
                    warn.append(f"Held back from {fmt_dt(j['reqStart'])} so it never runs out of Stage 1 parts" if paced
                                else f"Starts later than requested ({fmt_dt(j['reqStart'])}) because the line is busy")
                if mo.get("due") and out["end"] > add_days_ms(sod_ms(mo["due"]), 1):
                    out["late"] = True
                res[j["id"]] = out
                by_m[m["id"]].append(j["id"])
                prev_end, prev_part = out["end"], pkey
                if st == "s1":
                    s1_res[mo["id"]] = out
    return {"res": res, "byM": by_m, "cal": cal}


def qty_in(r, a, b):
    tot = sum(y - x for x, y in r["runSegs"])
    if not tot:
        return 0
    ov = sum(max(0, min(y, b) - max(x, a)) for x, y in r["runSegs"])
    return r["qty"] * ov / tot


# ------------------------------------------------------------------ floor queue
def refresh_floor_queue(by="Scheduler"):
    """Write each Stage 1 machine's planned run order onto its MOs (line, seq, plan times).
    MOs no longer planned on a machine leave its queue, except the one running there now."""
    from hmi.engine import notify  # local import: hmi depends on scheduler for ETAs

    added = moved = removed = 0
    with transaction.atomic():
        world = load_world()
        sch = compute(world)
        running_by_mo = world["running"]  # mo id -> machine id it is running on right now
        planned = {}
        for m in world["cfg"]["machines"]:
            if m["stage"] != "s1":
                continue
            for i, key in enumerate(sch["byM"].get(m["id"], [])):
                r = sch["res"][key]
                planned[r["moId"]] = (m["id"], i + 1, r)
        for mo in MO.objects.select_for_update().filter(status="OPEN"):  # closed MOs never sit in a kiosk queue
            if mo.id in running_by_mo:
                # A running MO always stays on the machine running it, whatever the board says.
                mid = running_by_mo[mo.id]
                p = planned.get(mo.id)
                seq, r = (p[1], p[2]) if p and p[0] == mid else (0, None)
                changed = mo.line_id != mid
                mo.line_id, mo.seq = mid, seq
                if r:
                    mo.plan_start, mo.plan_end, mo.plan_co = from_ms(r["start"]), from_ms(r["end"]), r["coMin"]
                mo.save(update_fields=["line", "seq", "plan_start", "plan_end", "plan_co"])
                moved += changed
            elif mo.id in planned:
                mid, seq, r = planned[mo.id]
                if mo.line_id != mid or mo.seq != seq:
                    if mo.line_id is None:
                        added += 1
                    else:
                        moved += 1
                mo.line_id, mo.seq = mid, seq
                mo.plan_start, mo.plan_end, mo.plan_co = from_ms(r["start"]), from_ms(r["end"]), r["coMin"]
                mo.save(update_fields=["line", "seq", "plan_start", "plan_end", "plan_co"])
            elif mo.line_id:
                mo.line_id, mo.seq, mo.plan_start, mo.plan_end, mo.plan_co = None, 0, None, None, 0
                mo.save(update_fields=["line", "seq", "plan_start", "plan_end", "plan_co"])
                removed += 1
        st = PlantSettings.load()
        st.plan_at = now()
        st.save(update_fields=["plan_at"])
        parts = [x for x in (added and f"{added} added", moved and f"{moved} re-ordered", removed and f"{removed} taken off a line") if x]
        if parts:
            notify(f"Schedule updated by {by}: " + ", ".join(parts))
        PlantSettings.bump()
    return sch


def eta(machine, remaining_units, start_ms, world=None, item=None, mo=None):
    """When a run of `remaining_units` on `machine` finishes if started at start_ms, using shift time."""
    world = world or load_world()
    m = next((x for x in world["cfg"]["machines"] if x["id"] == machine.id), None)
    if not m or remaining_units <= 0:
        return None
    rate, _ = rate_for(m, item, mo or {})
    eff = rate * (1 - min(.95, max(0, float(m["downtime"] or 0) / 100)))
    if eff <= 0:
        return None
    segs = Calendar(world["cfg"], world.get("labor"), world.get("laborLines")).consume(m, start_ms, remaining_units / eff * HOUR)
    return segs[-1][1] if segs else None
