import json
import math
import re
from datetime import date

from django.conf import settings
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from plant.models import MO, SPEED_MODES, STAGE_ORDER, CostItem, Machine, PlantSettings, Shift, ShiftBreak, PALETTE
from plant.util import Catalog, from_ms, ms, now

from hmi.views import current_person, touch
from plant.api import BadRequest, json_api, to_int

from . import imports
from .engine import compute, config_sig, fmt_dt, load_world, rate_for, refresh_floor_queue
from .models import Job, job_key

TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


@ensure_csrf_cookie
def board(request):
    return render(request, "scheduler/board.html", {"mode": "scheduler"})


def body(request):
    """The JSON object a screen sent; anything else (bad JSON, a list, a number) counts as empty."""
    try:
        data = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def ok(**kw):
    return JsonResponse({"ok": True, **kw})


def fail(msg, status=400):
    return JsonResponse({"ok": False, "error": msg}, status=status)


def result_json(r, lead=False):
    out = {k: r[k] for k in ("id", "moId", "op", "machineId", "rate", "basis", "eff", "gross", "qty", "scrap", "dt", "coMin", "runMin", "start",
                             "runStart", "end", "warn", "waiting", "paced", "overlap", "pushed", "late", "reqStart")}
    out["pace"] = r.get("pace")
    out["inkjet"] = r.get("inkjet", False)
    out["runSegs"] = r["runSegs"]
    out["unplanned"] = r.get("unplanned", False)
    out["running"] = r.get("running", False)
    for k in ("part", "parts", "rest", "split"):
        out[k] = r.get(k)
    if lead and r.get("laborCost") is not None:  # wages are for shift leads only
        out["laborCost"] = r["laborCost"]
    return out


def labor_json(world, lead, a=None, b=None):
    """The Labor Budget plan as the board uses it. Crew cost per hour only for shift leads."""
    plan = world.get("labor")
    lines = {}
    for mid, info in (world.get("laborLines") or {}).items():
        lines[mid] = {"line": info["line"], "shifts": {str(sid): {k: v for k, v in sh.items() if lead or k != "costPerHour"}
                                                        for sid, sh in info["shifts"].items()}}
    from labor.sync import budget_diff
    return {"on": bool(plan), "diff": {str(k): v for k, v in budget_diff().items()}, "enabled": world["cfg"].get("useLabor", True), "version": plan.version if plan else 0,
            "lines": lines, "days": plan.special_days(a, b) if plan and a and b else {}}


def mo_json(mo, cat):
    item = cat.item_for(mo)
    ms_ = cat.matches(mo["part"])
    return {**mo, "item": item, "matches": ms_ if len(ms_) > 1 else []}


@json_api
@require_GET
def state(request):
    world = load_world()
    sch = compute(world)
    st = PlantSettings.load()
    a, b = request.GET.get("from"), request.GET.get("to")
    work, hist = {}, []
    if a and b and a.isdigit() and b.isdigit() and 0 < int(a) < int(b) and int(b) - int(a) <= 400 * 86_400_000:
        a, b = int(a), int(b)
        for m in world["cfg"]["machines"]:
            work[m["id"]] = sch["cal"].between(m, a, b)
        from .engine import history
        hist = history(world, sch["cal"], a, b)
    cat = world["catalog"]
    from plant import perms
    me, _ = current_person(request)
    lead = perms.can(me, "costs.view")   # labor cost per run
    edit = perms.can(me, "schedule.edit")
    return ok(me={"name": me.name, "lead": edit} if me else None, editLocked=settings.SUITE["SCHEDULER_REQUIRE_LEAD"] and not edit,
              cfg=world["cfg"], cfgSig=config_sig(world["cfg"]), modes=SPEED_MODES, mos=[mo_json(m, cat) for m in world["mos"].values()],
              jobs=world["jobs"], res={k: result_json(r, lead) for k, r in sch["res"].items()}, byM=sch["byM"], work=work, hist=hist,
              labor=labor_json(world, lead, *( (a, b) if work else (None, None))),
              catalog={"count": len(cat.items), **(st.catalog_source or {})}, v=st.version, now=ms(now()))


def mutate(fn):
    """Every change runs in one transaction, then the Stage 1 queues on the floor are rebuilt."""
    def wrap(request, *a, **kw):
        if settings.SUITE["SCHEDULER_REQUIRE_LEAD"]:
            me, note = current_person(request)
            from plant import perms
            if not perms.can(me, "schedule.edit"):
                return JsonResponse({"ok": False, "login": True, "error": note or "Log in as someone allowed to change the schedule."}, status=403)
            touch(request)
        with transaction.atomic():
            r = fn(request, *a, **kw)
            if getattr(r, "status_code", 200) >= 400:
                transaction.set_rollback(True)
        if getattr(r, "status_code", 200) < 400:
            refresh_floor_queue(by="the Scheduler")
        return r
    return require_POST(wrap)


@json_api
@mutate
def put_job(request):
    p = body(request)
    mo = MO.objects.filter(pk=to_int(p.get("moId"), "order")).first()
    m = Machine.objects.filter(pk=to_int(p.get("machineId"), "line")).first()
    op = p.get("op")
    req = from_ms(to_int(p.get("reqStart"), "start time"))
    if not mo or not m or op not in STAGE_ORDER:
        return fail("That order or line no longer exists.")
    if m.stage != op:
        return fail(f"That work can only go on a {dict([('s1','Stage 1'),('eb','End Band'),('ac','Arts & Crafts'),('gb','Glue Bead'),('pk','Packaging')])[op]} line.")
    s1_finished = mo.status != "OPEN" or mo.produced >= mo.qty
    if op != "s1" and not s1_finished and not Job.objects.filter(mo=mo, op="s1").exists():
        return fail("Schedule Stage 1 for this order first.")
    from hmi.models import ShiftRun
    if op == "s1" and to_int(p.get("part") or 1, "part") == 1 and ShiftRun.objects.filter(active=True, mo=mo).exclude(machine=m).exists():
        return fail(f"MO {mo.number} is running on another machine right now. Move it after that run is changed over.")
    part = to_int(p.get("part") or 1, "part")
    if part != 1 and not Job.objects.filter(mo=mo, op=op, part=part).exists():
        return fail("That part of the split was merged back. Reload the board.")
    if op == "s1" and mo.status != "OPEN" and part == 1:
        return fail(f"MO {mo.number} is closed, so its Stage 1 run is finished.")
    Job.objects.update_or_create(mo=mo, op=op, part=part, defaults={"machine": m, "req_start": req})
    return ok()


def _key(p):
    bits = str(p.get("key", "")).split("__")
    if len(bits) not in (2, 3) or not bits[0].isdigit() or bits[1] not in STAGE_ORDER or (len(bits) == 3 and not bits[2].isdigit()):
        raise BadRequest("Unknown job.")
    return int(bits[0]), bits[1], int(bits[2]) if len(bits) == 3 else 1


@json_api
@mutate
def split_job(request):
    """Split a stage in two: part 1 stops at a time of day, after a quantity, or when the operator ends the MO at the
    kiosk (Stage 1); part 2 makes the rest. Part 2 starts out right after part 1 on the same line, to drag anywhere."""
    p = body(request)
    mo_id, op, _ = _key(p)
    j = Job.objects.select_for_update().filter(mo_id=mo_id, op=op, part=1).select_related("mo").first()
    if not j:
        return fail("Put this stage on the board first.")
    mode = p.get("mode")
    if mode not in ("time", "operator", "qty"):
        return fail("Pick how to split: at a time, when the operator ends it, or by quantity.")
    if mode == "operator" and op != "s1":
        return fail("Only Stage 1 is ended by an operator at the kiosk. Split this stage at a time or by quantity.")
    world = load_world()
    sch = compute(world)
    r1 = sch["res"].get(j.key)
    total = float(j.mo.qty or 0) - (float(j.mo.produced or 0) if op == "s1" else 0)
    j.split_mode, j.split_at, j.split_qty = mode, None, None
    if mode == "time":
        t = to_int(p.get("at"), "time")
        if r1 and not (r1["runStart"] < t < max(r1["end"], r1["runStart"] + 1)) and Job.objects.filter(mo_id=mo_id, op=op, part=2).count() == 0:
            return fail(f"Pick a time while part 1 runs: between {fmt_dt(r1['runStart'])} and {fmt_dt(r1['end'])}.")
        j.split_at = from_ms(t)
    elif mode == "qty":
        try:
            q = float(p.get("qty"))
        except (TypeError, ValueError):
            return fail("Type the quantity part 1 makes.")
        if not 0 < q < total:
            return fail(f"Part 1 has to make between 1 and {int(total) - 1:,} (the stage has {int(total):,} to make).")
        j.split_qty = q
    j.save()
    if not Job.objects.filter(mo_id=mo_id, op=op, part=2).exists():
        start = from_ms(r1["end"]) if r1 else j.req_start
        if mode == "time":
            start = j.split_at
        Job.objects.create(mo_id=mo_id, op=op, part=2, machine_id=j.machine_id, req_start=start)
    return ok(key=job_key(mo_id, op, 2))


@json_api
@mutate
def merge_job(request):
    """Undo a split: part 1 makes everything again."""
    p = body(request)
    mo_id, op, _ = _key(p)
    Job.objects.filter(mo_id=mo_id, op=op, part__gt=1).delete()
    Job.objects.filter(mo_id=mo_id, op=op, part=1).update(split_mode="", split_at=None, split_qty=None)
    return ok()


@json_api
@mutate
def del_job(request):
    """Take a stage off the board (all its parts). Taking part 2 of a split off merges it back into part 1."""
    p = body(request)
    mo_id, op, part = _key(p)
    if part > 1:
        Job.objects.filter(mo_id=mo_id, op=op, part__gt=1).delete()
        Job.objects.filter(mo_id=mo_id, op=op, part=1).update(split_mode="", split_at=None, split_qty=None)
        return ok()
    qs = Job.objects.filter(mo_id=mo_id, op=op)
    if op == "s1" and p.get("cascade"):
        qs = Job.objects.filter(mo_id=mo_id)
    qs.delete()
    return ok()


@json_api
@require_POST
def preview(request):
    p = body(request)
    world = load_world()
    part = to_int(p.get("part") or 1, "part")
    key = f"{p.get('moId')}__{p.get('op')}" + (f"__{part}" if part > 1 else "")   # part 2 of a split is its own job
    jobs = dict(world["jobs"])
    if p.get("op") not in STAGE_ORDER:
        raise BadRequest("Unknown stage.")
    jobs[key] = {**jobs.get(key, {}), "id": key, "moId": to_int(p.get("moId"), "order"), "op": p.get("op"), "machineId": to_int(p.get("machineId"), "line"),
                 "reqStart": to_int(p.get("reqStart"), "start time")}
    sch = compute(world, jobs)
    r = sch["res"].get(key)
    s1 = sch["res"].get(f"{p.get('moId')}__s1")
    me, _ = current_person(request)
    from plant import perms
    return ok(res=result_json(r, perms.can(me, "costs.view")) if r else None, s1End=s1["end"] if s1 else None)


@json_api
@require_POST
def suggest(request):
    """Scheduler › Suggest order for one Stage 1 line: nothing is changed (scheduler/sequence.py)."""
    from .sequence import suggest as run
    p = body(request)
    return ok(s=run(load_world(), to_int(p.get("machineId"), "line")))


@json_api
@mutate
def suggest_apply(request):
    """Put the suggested order on the board: worked out again now, so it is only applied as it was shown."""
    from .sequence import suggest as run
    p = body(request)
    mid = to_int(p.get("machineId"), "line")
    s = run(load_world(), mid)
    if not s.get("ok"):
        return fail(s.get("why") or "There is nothing to apply.")
    if list(p.get("keys") or []) != s["keys"]:
        return fail("The board changed since the suggestion was made. Press Suggest order again.")
    for i, key in enumerate(s["keys"]):
        mo_id, op, part = _key({"key": key})
        Job.objects.filter(mo_id=mo_id, op=op, part=part, machine_id=mid).update(req_start=from_ms(s["base"] + i))
    return ok(msg=f"Run order changed: {s['saved']} min less changeover.")


def _mo_fields(mo, p):
    try:
        if "qty" in p:
            q = float(p["qty"] or 0)
            if not math.isfinite(q) or q < 1:
                raise BadRequest("Quantity must be 1 or more.")
            mo.qty = int(q)
        if "due" in p:
            mo.due = date.fromisoformat(str(p["due"])[:10]) if p.get("due") else None
        if "pleats" in p:
            pl = float(p["pleats"]) if p.get("pleats") not in (None, "") else None
            if pl is not None and (not math.isfinite(pl) or pl <= 0):
                raise BadRequest("Pleats must be above 0.")
            mo.pleats = pl
    except ValueError:
        raise BadRequest("Check the quantity, due date and pleats.")
    if "ops" in p and isinstance(p["ops"], dict):
        mo.ops = {k: bool(p["ops"].get(k)) for k in STAGE_ORDER}
        # an S2K order has the stages it has MOs for (plant/mofamily.py); any other order always starts at Stage 1
        mo.ops["s1"] = "s1" in (mo.stage_qty or {}) if mo.stage_qty else True
    if "itemUid" in p:
        mo.item_uid = p["itemUid"] or None


@json_api
@mutate
def put_mo(request):
    p = body(request)
    if p.get("id"):
        mo = MO.objects.filter(pk=to_int(p["id"], "order")).first()
        if not mo:
            return fail("That order no longer exists.")
        before = dict(mo.ops or {})
        if "itemUid" in p:
            item = Catalog().by_uid.get(p["itemUid"])
            d = imports.default_ops(item)
            mo.ops = {**(mo.ops or {}), "eb": d["eb"], "ac": d["ac"]}
        _mo_fields(mo, p)
        mo.save()
        for o in STAGE_ORDER:
            if before.get(o) and not mo.ops.get(o):
                Job.objects.filter(mo=mo, op=o).delete()
        return ok(id=mo.id)
    number, part = str(p.get("mo") or "").strip(), str(p.get("part") or "").strip()
    if not number or not part or not p.get("qty"):
        return fail("Enter an MO number, part and quantity.")
    if len(number) > 40 or len(part) > 40:
        return fail("MO numbers and part numbers can be up to 40 characters.")
    if MO.objects.filter(number=number).exists():
        return fail(f"MO {number} is already on the list.")
    item = (Catalog().matches(part) or [None])[0]
    mo = MO(number=number, part=part, ops=imports.default_ops(item), source="manual")
    _mo_fields(mo, {k: v for k, v in p.items() if k != "ops"})
    mo.save()
    return ok(id=mo.id, matched=bool(item))


@json_api
@mutate
def del_mo(request):
    from hmi.models import ShiftRun
    mo = MO.objects.filter(pk=to_int(body(request).get("id"), "order")).first()
    if not mo:
        return ok()
    if ShiftRun.objects.filter(active=True, mo=mo).exists():
        return fail(f"MO {mo.number} is running on a machine. It can't be deleted until that run is changed over.")
    if mo.produced or mo.scrap:
        return fail(f"MO {mo.number} already has parts made against it. Close it from the Operator Kiosk instead, so its records stay.")
    mo.delete()
    return ok()


@json_api
@mutate
def save_config(request):
    """The Lines & Shifts page sends the whole set of machines and shifts. Everything is checked before
    anything is written; any problem rejects the whole save and nothing changes."""
    from hmi.models import ShiftRun
    p = body(request)
    shifts_in, machines_in = p.get("shifts"), p.get("machines")
    if not isinstance(shifts_in, list) or not isinstance(machines_in, list):
        raise BadRequest("The lines and shifts didn't come through. Reload the page and try again.")
    if not machines_in:
        raise BadRequest("Keep at least one line.")
    if not shifts_in:
        raise BadRequest("Keep at least one shift.")
    if p.get("cfgSig") != config_sig(load_world(jobs_override={})["cfg"]):
        return JsonResponse({"ok": False, "stale": True, "error": "Lines or shifts were changed on another screen. The page has reloaded "
                             "with the latest version; make your change again."}, status=409)

    def hhmm(v, what):
        v = str(v or "").strip()
        if not TIME_RE.match(v):
            raise BadRequest(f"{what} needs a time like 06:00.")
        return f"{int(v.split(':')[0]):02d}:{v.split(':')[1]}"

    def num(v, what, lo=0, hi=1e7):
        try:
            f = float(v or 0)
        except (TypeError, ValueError):
            raise BadRequest(f"{what} must be a number.")
        if not math.isfinite(f) or f < lo or f > hi:
            raise BadRequest(f"{what} must be between {lo:g} and {hi:g}.")
        return f

    labor = False   # the lines are the suite's own (the Labor Budget only starts new ones; Lines & Shifts shows where it differs)
    st = PlantSettings.load()
    bases = p.get("inkjetBases") if isinstance(p.get("inkjetBases"), list) else st.planner.get("inkjetBases", ["AQ"])
    bases = [b for b in dict.fromkeys(re.sub(r"[^A-Z]", "", str(x).upper())[:6] for x in bases[:20]) if b]
    st.planner = {"coSamePart": bool(p.get("coSamePart")), "coFirst": p.get("coFirst") is not False,
                  "useLabor": p.get("useLabor") is not False, "inkjetBases": bases, "coHistory": bool(p.get("coHistory", st.planner.get("coHistory"))),
                  "inkjetMin": num(p.get("inkjetMin", st.planner.get("inkjetMin", 0)), "Inkjet changeover minutes", 0, 240)}
    st.save(update_fields=["planner"])
    keep_s, sid_map = [], {}
    for i, sh in enumerate(shifts_in):   # the shifts are the machine schedule's (Admin › Machine schedule), also with the Labor Budget on
        if not isinstance(sh, dict):
            raise BadRequest("A shift didn't come through. Reload the page and try again.")
        name = str(sh.get("name") or "").strip()[:60] or f"Shift {i + 1}"
        start, end = hhmm(sh.get("start"), f"{name} start"), hhmm(sh.get("end"), f"{name} end")
        try:
            days = sorted({int(d) for d in sh.get("days") or [] if 0 <= int(d) <= 6})
        except (TypeError, ValueError):
            raise BadRequest(f"{name}: pick the days again.")
        obj = Shift.objects.filter(pk=sh.get("id")).first() if isinstance(sh.get("id"), int) else None
        obj = obj or Shift(created_on=timezone.localdate())   # a new shift isn't worked on the days before it
        obj.name, obj.start, obj.end, obj.days, obj.sort = name, start, end, days, i
        obj.save()
        sid_map[sh.get("id")] = obj.id
        keep_s.append(obj.id)
        obj.breaks.all().delete()
        for b in sh.get("breaks") or []:
            if not isinstance(b, dict):
                raise BadRequest(f"{name}: a break didn't come through. Reload the page and try again.")
            ShiftBreak.objects.create(shift=obj, label=str(b.get("label") or "Break").strip()[:60] or "Break",
                                      start=hhmm(b.get("start"), f"{name} break start"), end=hhmm(b.get("end"), f"{name} break end"))
    # Delete only what this planner removed. Lines or shifts added meanwhile from another screen are kept.
    removed_s = [x for x in p.get("removedShifts") or [] if isinstance(x, int) and x not in keep_s]
    Shift.objects.filter(id__in=removed_s).update(retired_on=timezone.localdate())   # kept for the days before (plant/schedule.py)
    from plant.schedule import forget
    forget()
    if not Shift.objects.exists():
        raise BadRequest("Keep at least one shift.")
    orig_names = dict(Machine.objects.values_list("id", "name"))
    keep_m, names, renames = [], set(), []
    for i, m in enumerate(machines_in):
        if not isinstance(m, dict):
            raise BadRequest("A line didn't come through. Reload the page and try again.")
        obj = Machine.objects.filter(pk=m.get("id")).first() if isinstance(m.get("id"), int) else None
        if not obj:
            obj = Machine(color=PALETTE[Machine.objects.count() % len(PALETTE)])
        name = obj.name if labor else (str(m.get("name") or "").strip()[:60] or f"Line {i + 1}")
        if name.lower() in names:
            raise BadRequest(f"Two lines are called {name}. Give each line its own name.")
        names.add(name.lower())
        stage = obj.stage if labor else (m.get("stage") if m.get("stage") in STAGE_ORDER else "s1")
        if obj.pk and obj.stage != stage and ShiftRun.objects.filter(machine=obj, active=True).exists():
            raise BadRequest(f"{obj.name} has a shift running, so its stage can't change. End the shift on the Operator Kiosk first.")
        modes = [x for x, _ in SPEED_MODES[stage]]
        if obj.pk and orig_names.get(obj.pk) != name:
            renames.append((obj.pk, orig_names.get(obj.pk), name))
        obj.name, obj.stage = name, stage
        if not labor:
            obj.sort = i
        obj.speed_mode = m.get("mode") if m.get("mode") in modes else modes[0]
        obj.ppm = num(m.get("ppm"), f"{name} speed (PPM)", 0, 5000)
        obj.rate = num(m.get("rate"), f"{name} speed (/hr)", 0, 100000)
        obj.scrap_pct = num(m.get("scrap"), f"{name} scrap %", 0, 90)
        obj.downtime_pct = num(m.get("downtime"), f"{name} downtime %", 0, 90)
        obj.changeover_min = num(m.get("changeover"), f"{name} changeover minutes", 0, 1440)
        lunch_in = [x for x in m.get("lunchShifts") or [] if x in sid_map]
        if Machine.objects.filter(name__iexact=name).exclude(pk=obj.pk).exclude(pk__in=[x.get("id") for x in machines_in if isinstance(x.get("id"), int)]).exists():
            raise BadRequest(f"There is already a line called {name}.")
        # free the name if another line in this save is taking it over (renames that swap names)
        clash = Machine.objects.filter(name=name).exclude(pk=obj.pk).first()
        if clash:
            clash.name = f"{clash.name}~{clash.pk}"
            clash.save(update_fields=["name"])
        obj.save()
        if not labor:
            runs = [sid_map[x] for x in m.get("shifts") or [] if x in sid_map]
            obj.shifts.set(runs)
            obj.lunch_shifts = sorted({sid_map[x] for x in lunch_in} & set(runs))   # runs through lunch, per shift it runs
            obj.save(update_fields=["lunch_shifts"])
        keep_m.append(obj.id)
    removed = Machine.objects.filter(id__in=[x for x in p.get("removedMachines") or [] if isinstance(x, int) and x not in keep_m])
    busy = [r.name for r in removed if ShiftRun.objects.filter(machine=r, active=True).exists()]
    if busy:
        raise BadRequest(f"{', '.join(busy)} has a shift running, so it can't be removed. End the shift on the Operator Kiosk first.")
    removed.delete()  # history stays: records keep the line name
    if not Machine.objects.exists():
        raise BadRequest("Keep at least one line.")
    # Carry kiosk records over to renamed lines (two steps, so lines that swap names don't collide).
    from hmi.engine import rename_machine_records
    for pk, old, new in renames:
        rename_machine_records(old, f"~renaming~{pk}")
    for pk, old, new in renames:
        rename_machine_records(f"~renaming~{pk}", new)
    return ok()


# ------------------------------------------------------------------ imports
@json_api
@require_POST
def import_parse(request):
    try:
        rows = imports.read_rows(request.FILES.get("file"), request.POST.get("text"))
    except Exception as e:
        return fail(str(e))
    if not rows:
        return fail("No rows found.")
    imp = imports.detect(rows)
    return ok(**imp, labels=[[k, l] for k, l, _ in imports.FIELDS])


@json_api
@require_POST
def import_preview(request):
    p = body(request)
    lst = imports.orders(p)
    return ok(orders=lst[:6], count=len(lst), unmatched=sum(1 for o in lst if not o["matched"]))


@json_api
@mutate
def import_commit(request):
    """The same as an S2K pull (plant/mofamily.py): new MOs merged into their orders by stage, a newer revision of an
    MO on file updates what's still to make and the due date, keeping its schedule."""
    from plant import mofamily
    lst = imports.orders(body(request))
    rows = [{"mo": o["mo"], "item": o["part"], "qty": o["qty"], "due": o["due"], "pleats": o["pleats"], "ops": o["ops"]} for o in lst]
    added, updated, skipped = mofamily.upsert(rows, "import")
    return ok(added=added, updated=updated, skipped=skipped)


@json_api
@require_GET
def catalog(request):
    cat = Catalog()
    q = request.GET.get("q", "").strip().upper()
    lst = [i for i in cat.items if not q or q in i["aq"].upper() or q in i["cust"].upper() or q in i["cat"].upper()]
    s1 = [m for m in load_world()["cfg"]["machines"] if m["stage"] == "s1"]
    rows = [{**i, "rates": [round(rate_for(m, i, {})[0]) for m in s1]} for i in lst[:250]]
    return ok(items=rows, total=len(lst), lines=[m["name"] for m in s1], source=PlantSettings.load().catalog_source, count=len(cat.items))


@json_api
@mutate
def catalog_import(request):
    f = request.FILES.get("file")
    if not f:
        return fail("Choose the cost sheet workbook.")
    items = imports.parse_cost_workbook(f)
    if not items:
        return fail("No cost sheet tabs found. Each tab needs 'Pleats' and 'Pleats per Minute' headers in row 2.")
    CostItem.objects.all().delete()
    CostItem.objects.bulk_create(items)
    st = PlantSettings.load()
    st.catalog_source = {"source": f.name, "importedAt": ms(now()), "builtin": False}
    st.save(update_fields=["catalog_source"])
    return ok(count=len(items))


@json_api
@mutate
def catalog_reset(request):
    items, src = imports.builtin_catalog()
    CostItem.objects.all().delete()
    CostItem.objects.bulk_create(items)
    st = PlantSettings.load()
    st.catalog_source = {"source": src, "builtin": True}
    st.save(update_fields=["catalog_source"])
    return ok(count=len(items))
