"""Schedule (/schedule/): the plant's shifts and its calendar, in one place the whole suite follows.

Left, the shifts: their times, the weekdays each one works, and their breaks. Right, the calendar: each day as it
will be worked, and any day can be changed on its own (closed, off, no production, an extra working day, or one
shift off / added / at other times that day). The kiosks' shift windows and planned time, the Scheduler's run times
and Reports all read the same schedule (plant/schedule.py), so a change here, even for today, is picked up at once:
the open shift ends at its new time, the board re-plans, and stops outside the new hours aren't downtime.
The Labor Budget keeps its own calendar for the budget; its holidays can be copied in once.
"""
import calendar as pycal
import re
from datetime import date, timedelta
from urllib.parse import urlencode

from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from . import perms
from .models import PlantDay, PlantSettings, Shift, ShiftBreak
from .schedule import book, forget

PERM = "schedule.edit"
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
KIND_LABEL = dict(PlantDay.KINDS)


def _me(request):
    from hmi.views import current_person
    me, _ = current_person(request)
    return me


def _hhmm(v, what):
    v = str(v or "").strip()
    if not TIME_RE.match(v):
        raise ValueError(f"{what} needs a time like 06:00.")
    h, m = v.split(":")
    return f"{int(h):02d}:{m}"


def _mins(a, b):
    return ((int(b[:2]) * 60 + int(b[3:])) - (int(a[:2]) * 60 + int(a[3:]))) % 1440 or 1440


def _changed(me, what):
    """Everything that follows the schedule hears about it now: the kiosks' windows and planned time, the board."""
    from hmi.engine import Ctx, audit
    forget()
    PlantSettings.bump()
    try:
        from scheduler.engine import refresh_floor_queue
        refresh_floor_queue(by="Schedule")
    except Exception:  # noqa: BLE001 - the change is saved either way
        pass
    audit(Ctx(user=me), f"Schedule: {what}")


def month_grid(y, m, today):
    b = book()
    shifts = [s for s in Shift.objects.order_by("sort", "id")]
    weeks = []
    for wk in pycal.Calendar(firstweekday=6).monthdatescalendar(y, m):
        row = []
        for d in wk:
            on = b.all_on(d, {s.id for s in shifts})
            dow = (d.weekday() + 1) % 7
            worked = [x for x in on if dow in (x.get("days") or [])]
            info = b.day_info(d) or {}
            kind = info.get("kind") or ""
            row.append({"d": d, "in": d.month == m, "today": d == today, "kind": kind, "label": KIND_LABEL.get(kind, ""),
                        "note": info.get("note", ""), "changed": bool(info.get("shifts")),
                        "shifts": [{"name": x["name"], "start": x["start"], "end": x["end"], "changed": x.get("changed")} for x in worked],
                        "state": kind if kind in ("off", "closed", "noprod") else ("work" if worked else "idle")})
        weeks.append(row)
    return weeks


@ensure_csrf_cookie
@require_GET
def page(request):
    me = _me(request)
    today = timezone.localdate()
    try:
        y, m = (int(x) for x in str(request.GET.get("month") or "").split("-")[:2])
        date(y, m, 1)
    except (ValueError, TypeError):
        y, m = today.year, today.month
    first = date(y, m, 1)
    prev = (first - timedelta(days=1)).replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    sel = None
    if request.GET.get("day"):
        try:
            sel = date.fromisoformat(request.GET["day"])
        except ValueError:
            sel = None
    day_edit = None
    shifts = list(Shift.objects.order_by("sort", "id").prefetch_related("breaks"))
    if sel:
        pd = PlantDay.objects.filter(day=sel).first()
        dow = (sel.weekday() + 1) % 7
        rows = []
        for s in shifts:
            o = (pd.shifts if pd else {}).get(str(s.id)) or {}
            normal = dow in (s.days or [])
            base = book().on(s.id, sel)
            rows.append({"s": s, "o": o, "normal": normal, "state": "off" if o.get("off") else "on" if o.get("on") else "",
                         "start": o.get("start") or s.start, "end": o.get("end") or s.end, "times": bool(o.get("start")),
                         "worked": bool(base and dow in (base.get("days") or []))})
        day_edit = {"d": sel, "pd": pd, "rows": rows, "kind": pd.kind if pd else ""}
    for s in shifts:
        s.hours = round((_mins(s.start, s.end) - sum(_mins(b.start, b.end) for b in s.breaks.all())) / 60, 2)
    return render(request, "plant/schedule.html", {
        "mode": "schedule", "me": me, "can": perms.can(me, PERM), "shifts": shifts, "dows": list(enumerate(DOW)),
        "weeks": month_grid(y, m, today), "month": first, "prev": prev.strftime("%Y-%m"), "next": nxt.strftime("%Y-%m"),
        "thisMonth": today.strftime("%Y-%m"), "kinds": PlantDay.KINDS, "day": day_edit,
        "upcoming": PlantDay.objects.filter(day__gte=today).order_by("day")[:12],
        "msg": request.GET.get("msg", "")[:300], "err": request.GET.get("err", "")[:300]})


def _back(request, **kw):
    q = {k: v for k, v in (("month", request.POST.get("month")), ("day", request.POST.get("day"))) if v}
    q.update(kw)
    return HttpResponseRedirect("/schedule/?" + urlencode(q))


@require_POST
def do(request, name):
    me = _me(request)
    if not perms.can(me, PERM):
        return _back(request, err="Log in as someone who can change the schedule (Admin › Permissions › Change the schedule).")
    p = request.POST
    try:
        if name == "shift":
            return _shift_save(request, me, p)
        if name == "shiftAdd":
            s = Shift.objects.create(name=f"Shift {Shift.objects.count() + 1}", start="07:00", end="15:30", days=[1, 2, 3, 4, 5],
                                     sort=Shift.objects.count(), created_on=timezone.localdate())
            _changed(me, f"added {s.name}")
            return _back(request, msg=f"{s.name} added (Mon–Fri 07:00–15:30): set its times and the lines that run it (Scheduler › Lines & Shifts).")
        if name == "shiftRetire":
            s = Shift.objects.get(pk=int(p.get("id")))
            if Shift.objects.count() <= 1:
                raise ValueError("Keep at least one shift.")
            s.retired_on = timezone.localdate()
            s.save(update_fields=["retired_on"])
            _changed(me, f"removed {s.name} from today (kept for the days before)")
            return _back(request, msg=f"{s.name} removed from today on. The days before keep it.")
        if name == "day":
            return _day_save(request, me, p)
        if name == "copyLabor":
            return _copy_labor(request, me)
    except (ValueError, Shift.DoesNotExist) as e:
        return _back(request, err=str(e) or "That didn't save.")
    return _back(request, err="Unknown action.")


def _shift_save(request, me, p):
    s = Shift.objects.get(pk=int(p.get("id")))
    name = str(p.get("name") or "").strip()[:60] or s.name
    start, end = _hhmm(p.get("start"), f"{name} start"), _hhmm(p.get("end"), f"{name} end")
    days = sorted({int(d) for d in p.getlist("days") if str(d).isdigit() and 0 <= int(d) <= 6})
    breaks = []
    for lab, a, b in zip(p.getlist("bLabel"), p.getlist("bStart"), p.getlist("bEnd")):
        if not (a or b):
            continue
        breaks.append((str(lab or "Break").strip()[:60] or "Break", _hhmm(a, f"{name} break start"), _hhmm(b, f"{name} break end")))
    s.name, s.start, s.end, s.days = name, start, end, days
    s.save()   # (plant/schedule.py keeps how it was for the days before)
    s.breaks.all().delete()
    for lab, a, b in breaks:
        ShiftBreak.objects.create(shift=s, label=lab, start=a, end=b)
    _changed(me, f"{name}: {start}–{end}, " + (", ".join(DOW[d] for d in days) or "no days") + f", {len(breaks)} break(s)")
    return _back(request, msg=f"{name} saved. It applies from today; the days before keep the times they had.")


def _day_save(request, me, p):
    d = date.fromisoformat(str(p.get("day")))
    kind = p.get("kind") if p.get("kind") in KIND_LABEL else ""
    note = str(p.get("note") or "").strip()[:120]
    shifts = {}
    for s in Shift.objects.all():
        st = p.get(f"s{s.id}") or ""
        o = {}
        if st == "off":
            o["off"] = True
        elif st == "on":
            o["on"] = True
        if p.get(f"t{s.id}") == "1":
            a, b = _hhmm(p.get(f"a{s.id}"), f"{s.name} start"), _hhmm(p.get(f"b{s.id}"), f"{s.name} end")
            if (a, b) != (s.start, s.end):
                o.update(start=a, end=b)
        if o:
            shifts[str(s.id)] = o
    if not kind and not shifts and not note:
        PlantDay.objects.filter(day=d).delete()
        _changed(me, f"{d:%a %b %d}: back to normal")
        return _back(request, msg=f"{d:%a %b %d} is back to normal (as the shifts' weekdays say).")
    PlantDay.objects.update_or_create(day=d, defaults={"kind": kind, "note": note, "shifts": shifts, "updated_by": me.name if me else ""})
    what = KIND_LABEL.get(kind) or "shift changes"
    _changed(me, f"{d:%a %b %d}: {what}" + (f" ({note})" if note else ""))
    soon = d <= timezone.localdate() + timedelta(days=1)
    return _back(request, msg=f"{d:%a %b %d} saved: {what}." + (" The kiosks and the schedule follow it now." if soon else ""))


def _copy_labor(request, me):
    """Copy the Labor Budget calendar's holidays, days off and extra work days (from today on) onto the plant calendar.
    Days already on the plant calendar are left as they are."""
    from labor.plan import LaborPlanView
    plan = LaborPlanView.current()
    if not plan:
        raise ValueError("There's no Labor Budget plan saved to copy from.")
    today, n = timezone.localdate(), 0
    kinds = {"WORK": "work", "OFF": "off", "HOLIDAY": "closed", "NOPROD": "noprod", "INVENTORY": "noprod"}
    for k, v in (plan.overrides or {}).items():
        try:
            d = date.fromisoformat(k)
        except ValueError:
            continue
        kind = kinds.get(str(v).upper())
        if d < today or not kind or PlantDay.objects.filter(day=d).exists():
            continue
        PlantDay.objects.create(day=d, kind=kind, note="From the Labor Budget", updated_by=me.name if me else "")
        n += 1
    _changed(me, f"copied {n} days from the Labor Budget calendar")
    return _back(request, msg=f"{n} day{'s' if n != 1 else ''} copied from the Labor Budget calendar (days already set here were kept).")
