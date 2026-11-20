"""History builder (Supervisor › History builder): fill in what the paper logs say for time the machine recorded with
nobody logged in, or for time with no machine data at all.

A paper-log row is: operator, from, to, MO / part, qty, scrap. Saving the rows for a log
- puts the operator on the log; rows naming different operators split the log at each hand-over, and each piece gets
  its own minutes, units, downtime, QC checks, notes and changeover pieces (by time), uptime and downtime figures;
- adds an MO run for each row with an MO or part (qty blank = what the machine counted in that time).
A log can also be added by hand for time with no record at all (the feed was down, or the machine has none).
Each change is kept in HistoryEdit with who, when and why, the same as Supervisor › History corrections.
MO progress (made counts on the order) is not changed: those counts come from the machine and from S2K."""
import re
from datetime import datetime, time, timedelta

from django.db.models import Sum
from django.utils import timezone

from hmi import engine
from hmi.engine import Need, audit, cmd, fmt_t, need
from hmi.models import (AutoQC, ChangeoverPiece, Downtime, MoRun, PlannedTime, PpmReading, QCCheck, RollTag, ShiftNote, ShiftRun,
                        Ticket)
from plant.models import MO, Machine, Person
from plant.util import local

from .edit import PERM, restat, show
from .models import HistoryEdit

MAX_ROWS = 20
SOURCE = "Paper log"
HAND = "Entered by hand from the paper log (no machine data)."


# ------------------------------------------------------------------ reading the rows
def when(v, base, what, slack=timedelta(0)):
    """A time from a paper-log row: "14:30" (on the log's day, the next day if that's more than `slack` before
    `base`) or a full date and time."""
    v = str(v or "").strip()
    if not v:
        raise Need(f"Enter the {what} time.")
    if re.fullmatch(r"\d{1,2}:\d{2}", v):
        h, m = (int(x) for x in v.split(":"))
        need(h < 24 and m < 60, f"{v} isn't a time.")
        b = local(base)
        d = timezone.make_aware(datetime.combine(b.date(), time(h, m)))
        if d < b.replace(second=0, microsecond=0) - slack:
            d += timedelta(days=1)
        return d
    try:
        d = datetime.fromisoformat(v)
    except ValueError:
        raise Need(f"{v} isn't a time: type it as 14:30.")
    return timezone.make_aware(d) if timezone.is_naive(d) else d


def blank(r):
    return not any(str(r.get(k) or "").strip() for k in ("who", "mo", "item", "qty", "scrap", "from", "to"))


def number(v, what):
    v = str(v or "").strip()
    if not v:
        return None
    try:
        n = int(float(v))
    except ValueError:
        raise Need(f"Type the {what} as a number.")
    need(n >= 0, f"The {what} can't be below 0.")
    return n


def part_of(mo):
    from plant.mofamily import split_number
    if not mo:
        return ""
    x = MO.objects.filter(number=mo).first()
    if not x:
        base, _ = split_number(mo)
        x = MO.objects.filter(number=base).first()
    return x.part if x else ""


def clean_rows(raw, a, b):
    """The paper-log rows for the time a..b, checked and in time order: [{who, s, e, mo, item, qty, scrap}]."""
    rows = []
    for i, r in enumerate(raw or []):
        if not isinstance(r, dict) or blank(r):
            continue
        n = len(rows) + 1
        s = when(r.get("from"), a, f"row {n} from", slack=timedelta(hours=2))
        e = when(r.get("to"), s, f"row {n} to")
        if e <= s:
            e += timedelta(days=1)
        slack = timedelta(minutes=15)   # paper times are rounded: a little before or after the log is its start or end
        need(s >= a - slack and e <= b + slack, f"Row {n} ({fmt_t(s)} – {fmt_t(e)}) is outside this log ({fmt_t(a)} – {fmt_t(b)}).")
        s, e = max(s, a), min(e, b)
        need(e > s, f"Row {n} is outside this log ({fmt_t(a)} – {fmt_t(b)}).")
        mo = re.sub(r"\s+", "", str(r.get("mo") or ""))[:40]
        item = re.sub(r"\s+", "", str(r.get("item") or "")).upper()[:40] or part_of(mo)
        qty, scrap = number(r.get("qty"), f"row {n} qty"), number(r.get("scrap"), f"row {n} scrap")
        need(not (qty or scrap) or mo or item, f"Row {n}: enter the MO or the part the {qty or scrap} were for.")
        rows.append({"who": str(r.get("who") or "").strip()[:120], "s": s, "e": e, "mo": mo, "item": item, "qty": qty, "scrap": scrap or 0})
    need(rows, "Fill in at least one row from the paper log.")
    rows.sort(key=lambda x: x["s"])
    for x, y in zip(rows, rows[1:]):
        need(y["s"] >= x["e"], f"Rows {fmt_t(x['s'])} – {fmt_t(x['e'])} and {fmt_t(y['s'])} – {fmt_t(y['e'])} overlap: one machine makes "
                               "one thing at a time.")
    return rows


def segments(rows, a, b):
    """Who was on the machine when: [(who, start, end, [rows])]. A hand-over is where the next operator's first row
    starts; time no row covers goes to the operator before it (or the first one, at the start)."""
    segs = []
    for r in rows:
        if segs and segs[-1][0] == r["who"]:
            segs[-1][3].append(r)
        else:
            segs.append([r["who"], r["s"], None, [r]])
    segs[0][1] = a
    for x, y in zip(segs, segs[1:]):
        x[2] = y[1]
    segs[-1][2] = b
    return [tuple(x) for x in segs]


# ------------------------------------------------------------------ splitting a log at hand-overs
MOVE = [(PpmReading, "t", "gt_lte"), (QCCheck, "t", None), (RollTag, "t", None), (Ticket, "t", None), (ShiftNote, "t", None),
        (AutoQC, "t", None), (ChangeoverPiece, "start", None), (MoRun, "start_time", None)]


def split_log(sh, cuts):
    """Cut a closed log at each time in `cuts`; the records in each piece's time go with it. Returns the pieces."""
    pieces = [sh]
    for at in sorted(cuts):
        cur = pieces[-1]
        need(cur.start < at < cur.end, f"{fmt_t(at)} isn't inside the log.")
        new = ShiftRun.objects.create(machine=cur.machine, line=cur.line, shift_name=cur.shift_name, start=at, end=cur.end, active=False,
                                      state="idle", mo=cur.mo, mo_start=at if cur.mo_id else None, person=None, user_name="", qc_last=at)
        for model, f, how in MOVE:
            q = {f"{f}__gt": at} if how == "gt_lte" else {f"{f}__gte": at}
            model.objects.filter(shift=cur, **q).update(shift=new)
        for d in Downtime.objects.filter(shift=cur, end_time__gt=at):
            if d.start_time >= at:
                d.shift = new
                d.save(update_fields=["shift"])
            else:   # a stop going on at the hand-over: each operator gets their part of it
                Downtime.objects.create(shift=new, line=d.line, mo=d.mo, start_time=at, end_time=d.end_time,
                                        minutes=max(1, round((d.end_time - at).total_seconds() / 60)), reason=d.reason, notes=d.notes, by=d.by)
                d.end_time, d.minutes = at, max(1, round((at - d.start_time).total_seconds() / 60))
                d.save(update_fields=["end_time", "minutes"])
        cur.end = at
        cur.save(update_fields=["end"])
        pieces.append(new)
    return pieces


def refigure(sh, units=None):
    """A piece's units (what the machine counted in it, or `units` when it has no machine data), its planned time
    for Grafana, and its uptime and downtime figures."""
    got = PpmReading.objects.filter(shift=sh).aggregate(u=Sum("units"))["u"]
    if PpmReading.objects.filter(shift=sh).exists():
        sh.count = got or 0
    elif units is not None:
        sh.count = units
    sh.mo_count = sh.count if sh.mo_id else 0
    sh.save(update_fields=["count", "mo_count"])
    PlannedTime.objects.filter(shift=sh).delete()
    PlannedTime.objects.bulk_create([PlannedTime(shift=sh, line=sh.line, start=max(a, sh.start), end=min(b, sh.end), kind=k[:60])
                                     for a, b, k in engine.planned_windows(sh, kinds=True) if a < sh.end and b > sh.start])
    if PpmReading.objects.filter(shift=sh).exists():
        restat(sh)
    else:   # nothing recorded by the machine: no uptime figure can be worked out
        sh.down_mins = sum(d.minutes or 0 for d in Downtime.objects.filter(shift=sh))
        sh.save(update_fields=["down_mins"])


def counted(sh, s, e):
    return PpmReading.objects.filter(shift=sh, t__gt=s, t__lte=e).aggregate(u=Sum("units"))["u"] or 0


def kept(ctx, kind, x, field, old, new, why):
    HistoryEdit.objects.create(t=ctx.now, by=ctx.by, kind=kind, obj_id=x.pk, line=x.line or "", field=field, old=show(old), new=show(new), why=why)


def fill(sh, rows, ctx, why, units_by_hand=False):
    """Put the paper-log rows on a closed log: operators (splitting it at hand-overs) and MO runs."""
    segs = segments(rows, sh.start, sh.end)
    for r in rows:
        clash = MoRun.objects.filter(line=sh.line, start_time__lt=r["e"], end_time__gt=r["s"]).first()
        need(not clash, f"{sh.line} already has MO run {clash.mo} {clash.item} {fmt_t(clash.start_time)} – {fmt_t(clash.end_time)} in that "
                        "time. Edit or delete it in Supervisor › History first." if clash else "")
    pieces = split_log(sh, [s for _, s, _, _ in segs[1:]]) if len(segs) > 1 else [sh]
    for piece in pieces[1:]:
        kept(ctx, "log", piece, "Split from", f"#{sh.pk}", f"{fmt_t(piece.start)} – {fmt_t(piece.end)}", why)
    if len(pieces) > 1:
        kept(ctx, "log", sh, "Split at", None, ", ".join(fmt_t(p.start) for p in pieces[1:]), why)
    made = 0
    for (who, s, e, rs), piece in zip(segs, pieces):
        if who != (piece.user_name or ""):
            kept(ctx, "log", piece, "Operator", piece.user_name, who, why)
            piece.user_name, piece.person = who, Person.objects.filter(name=who).first() if who else None
            piece.save(update_fields=["user_name", "person"])
        for r in rs:
            if not (r["mo"] or r["item"]):
                continue
            qty = r["qty"] if r["qty"] is not None else counted(piece, r["s"], r["e"])
            run = MoRun.objects.create(shift=piece, line=piece.line, mo=r["mo"], item=r["item"], qty=qty, scrap=r["scrap"], start_time=r["s"],
                                       end_time=r["e"])
            kept(ctx, "morun", run, f"Added from {SOURCE.lower()}", None, f"MO {run.mo} {run.item} {qty} pcs {fmt_t(run.start_time)} – "
                                                                           f"{fmt_t(run.end_time)}" + (f", {run.scrap} scrap" if run.scrap else ""), why)
            made += 1
        refigure(piece, sum(r["qty"] or 0 for r in rs) if units_by_hand else None)
    names = ", ".join(f"{w or 'nobody'} {fmt_t(s)} – {fmt_t(e)}" for w, s, e, _ in segs)
    audit(ctx, f"Filled in {sh.line}'s log #{sh.pk} from the paper log: {names}; {made} MO run{'s' if made != 1 else ''}"
               + (f" ({why})" if why else ""))
    return pieces, made


# ------------------------------------------------------------------ commands
@cmd("histFill", PERM)
def hist_fill(p, ctx):
    """Fill in a closed log from the paper log. p: {id, rows: [{who, from, to, mo, item, qty, scrap}], why}"""
    sh = ShiftRun.objects.select_for_update().filter(pk=p.get("id")).first()
    need(sh, "That log is no longer on file.")
    need(not sh.active and sh.end, "That log is still running. Fill it in after its shift ends (or fix it at the kiosk now).")
    rows = clean_rows(p.get("rows"), sh.start, sh.end)
    pieces, made = fill(sh, rows, ctx, str(p.get("why") or "").strip())
    split = f", split into {len(pieces)} logs" if len(pieces) > 1 else ""
    return {"msg": f"Saved{split}: {made} MO run{'s' if made != 1 else ''} added.", "data": {"ids": [x.pk for x in pieces]}}


@cmd("histAdd", PERM)
def hist_add(p, ctx):
    """A log for time with no record at all, from the paper log. p: {machine, day, start, end, shift_name, rows, why}"""
    m = Machine.objects.filter(name=str(p.get("machine") or "")).first()
    need(m, "Pick the machine.")
    day = str(p.get("day") or "").strip()
    try:
        base = timezone.make_aware(datetime.fromisoformat(day)) if day else None
    except ValueError:
        base = None
    need(base, "Pick the day.")
    a = when(p.get("start"), base, "start")
    b = when(p.get("end"), a, "end")
    if b <= a:
        b += timedelta(days=1)
    need(b - a <= timedelta(hours=24), "A log can be at most 24 hours long: add one for each shift.")
    need(a < timezone.now(), "That time hasn't happened yet.")
    from django.db.models import Q
    clash = ShiftRun.objects.filter(Q(end__gt=a) | Q(end__isnull=True), machine=m, start__lt=b).first()
    need(not clash, f"{m.name} already has a log {fmt_t(clash.start)} – {fmt_t(clash.end) if clash and clash.end else 'now'} in that time. "
                    "Open it in the History builder and fill that one in." if clash else "")
    rows = clean_rows(p.get("rows"), a, b)
    name = str(p.get("shift_name") or "").strip()[:60] or engine.machine_window(m, a + timedelta(seconds=30))["name"]
    sh = ShiftRun.objects.create(machine=m, line=m.name, shift_name=name, start=a, end=b, active=False, state="idle", person=None, user_name="",
                                 qc_last=a, end_issues=HAND)
    why = str(p.get("why") or "").strip()
    kept(ctx, "log", sh, f"Added from {SOURCE.lower()}", None, f"{name} {fmt_t(a)} – {fmt_t(b)}", why)
    pieces, made = fill(sh, rows, ctx, why, units_by_hand=True)
    return {"msg": f"Log added for {m.name}" + (f" ({len(pieces)} operators)" if len(pieces) > 1 else "") + f": {made} MO run"
                   f"{'s' if made != 1 else ''}.", "data": {"ids": [x.pk for x in pieces]}}


# ------------------------------------------------------------------ what the pages show
def to_fill(line="", a=None, b=None):
    """Closed logs that need the paper log: nobody logged in while the machine ran or made parts, or an operator
    but no parts on record."""
    from django.db.models import Count, Exists, OuterRef, Q
    ran = PpmReading.objects.filter(shift=OuterRef("pk"), run=True, planned=False)
    qs = ShiftRun.objects.filter(active=False, end__isnull=False)
    if a:
        qs = qs.filter(start__gte=a)
    if b:
        qs = qs.filter(start__lt=b)
    if line:
        qs = qs.filter(line=line)
    qs = qs.annotate(ran=Exists(ran), runs=Count("morun", distinct=True))
    nobody = Q(person__isnull=True, user_name="") & (Q(count__gt=0) | Q(ran=True))
    noparts = ~Q(user_name="") & Q(count__gt=0, runs=0)
    out = []
    for x in qs.filter(nobody | noparts).order_by("-start"):
        x.need = "operator" if not x.user_name else "parts"
        out.append(x)
    return out


def hours(sh):
    """Hour by hour, what the machine recorded on this log: to match against the paper log."""
    rows = {}
    for t, units, run, planned, recipe in PpmReading.objects.filter(shift=sh).order_by("t").values_list("t", "units", "run", "planned", "recipe"):
        h = local(t - timedelta(seconds=1)).replace(minute=0, second=0, microsecond=0)
        r = rows.setdefault(h, {"h": h, "units": 0, "run": 0, "down": 0, "planned": 0, "recipes": []})
        r["units"] += units or 0
        if run:
            r["run"] += 1
        elif planned:
            r["planned"] += 1
        else:
            r["down"] += 1
        if recipe and recipe not in r["recipes"]:
            r["recipes"].append(recipe)
    return [rows[k] for k in sorted(rows)]
