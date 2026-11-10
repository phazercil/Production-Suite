"""Corrections to past records from Supervisor › History: shift logs, downtime, QC checks, changeover pieces and MO
runs. Each change is an engine command (permission "Correct history"), and every field changed is kept in
HistoryEdit with who, when, the old and new value and why."""
from datetime import datetime

from django.utils import timezone

from hmi import engine
from hmi.engine import Need, audit, cmd, fmt_t, need
from hmi.models import ChangeoverPiece, Downtime, MoRun, QCCheck, ShiftRun
from plant.models import Person
from plant.util import local

from .models import HistoryEdit

PERM = "history.edit"
KINDS = {"log": ShiftRun, "downtime": Downtime, "qc": QCCheck, "co": ChangeoverPiece, "morun": MoRun}
LABEL = {"log": "Shift log", "downtime": "Downtime", "qc": "QC check", "co": "Changeover", "morun": "MO run"}


def parse_dt(v, what):
    v = str(v or "").strip()
    try:
        d = datetime.fromisoformat(v)
    except ValueError:
        raise Need(f"Pick the {what} date and time.")
    return timezone.make_aware(d) if timezone.is_naive(d) else d


def keep(v, old, what):
    """A date-time from the form (to the minute): the stored time, seconds and all, when the box wasn't changed."""
    if old is not None and str(v or "").strip() == local(old).strftime("%Y-%m-%dT%H:%M"):
        return old
    return parse_dt(v, what)


def show(v):
    if isinstance(v, datetime):
        return local(v).strftime("%m/%d/%Y %I:%M %p")
    if v is None:
        return ""
    return str(v)


def get(kind, pk):
    need(kind in KINDS, "Unknown record.")
    x = KINDS[kind].objects.filter(pk=pk).first()
    need(x, "That record is no longer on file.")
    return x


def log_of(kind, x):
    if kind == "log":
        return x
    return getattr(x, "shift", None)


def restat(sh):
    """A closed log's stored downtime and uptime follow its downtime entries after a correction."""
    if not sh or sh.active:
        return
    down = sum(d.minutes or 0 for d in Downtime.objects.filter(shift=sh))
    n = engine.shift_stats(sh)["mins"]
    sh.down_mins = down
    sh.uptime = max(0, min(100, round((n - down) / n * 100))) if n else 100
    sh.save(update_fields=["down_mins", "uptime"])


def note(ctx, kind, x, changes, why):
    line = getattr(x, "line", "") or ""
    for f, old, new in changes:
        HistoryEdit.objects.create(t=ctx.now, by=ctx.by, kind=kind, obj_id=x.pk, line=line, field=f, old=show(old), new=show(new), why=why)
    if changes:
        audit(ctx, f"Corrected {LABEL[kind].lower()} #{x.pk} on {line}: " + "; ".join(f"{f} {show(o) or '—'} → {show(n) or '—'}" for f, o, n in changes)
              + (f" ({why})" if why else ""))


def setf(x, changes, attr, label, new):
    old = getattr(x, attr)
    same = (old or "") == (new or "") if isinstance(new, str) or new is None else old == new
    if not same:
        changes.append((label, old, new))
        setattr(x, attr, new)


# ------------------------------------------------------------------ the fields of each kind
def edit_downtime(x, p, ch):
    reason = str(p.get("reason") or "").strip()[:120]
    setf(x, ch, "reason", "Reason", reason or None)
    setf(x, ch, "notes", "Notes", str(p.get("notes") or "").strip())
    setf(x, ch, "mo", "MO", str(p.get("mo") or "").strip()[:40])
    a, b = keep(p.get("start"), x.start_time, "start"), keep(p.get("end"), x.end_time, "end")
    need(b >= a, "The end is before the start.")
    moved = (a, b) != (x.start_time, x.end_time)
    setf(x, ch, "start_time", "Start", a)
    setf(x, ch, "end_time", "End", b)
    if moved:   # (an untouched stop keeps its minutes as they were counted)
        setf(x, ch, "minutes", "Minutes", round((b - a).total_seconds() / 60))


def edit_log(x, p, ch):
    who = str(p.get("operator") or "").strip()[:120]
    if who != (x.user_name or ""):
        person = Person.objects.filter(name=who).first() if who else None
        ch.append(("Operator", x.user_name, who))
        x.user_name, x.person = who, person
    setf(x, ch, "shift_name", "Shift", str(p.get("shift_name") or "").strip()[:60])
    try:
        count = int(float(p.get("count") or 0))
    except ValueError:
        raise Need("Type the units as a number.")
    need(count >= 0, "Units can't be below 0.")
    setf(x, ch, "count", "Units", count)
    a = keep(p.get("start"), x.start, "start")
    setf(x, ch, "start", "Start", a)
    if not x.active:
        b = keep(p.get("end"), x.end, "end")
        need(b > a, "The end is before the start.")
        setf(x, ch, "end", "End", b)
    setf(x, ch, "issues", "Start-of-shift issue", str(p.get("issues") or "").strip())
    setf(x, ch, "end_issues", "End-of-shift issue", str(p.get("end_issues") or "").strip())


def edit_qc(x, p, ch):
    from hmi import qc
    items = qc.items_for(x.shift.machine.stage if x.shift and x.shift.machine else "s1")
    try:
        vals = qc.parse(items, p)
    except ValueError as e:
        raise Need(str(e))
    ok, fails = qc.judge(items, vals, engine.spec_for(x.item))
    old_vals = dict(x.values or {}) or {k: getattr(x, k) for k in ("len", "wid", "ph", "sbl", "sbr", "glue", "cut", "scrap")}
    for it in items:
        k = it["key"]
        if str(old_vals.get(k, "")) != str(vals.get(k, "")):
            ch.append((it["label"], old_vals.get(k, ""), vals.get(k, "")))
    for k in ("len", "wid", "ph", "sbl", "sbr"):
        setattr(x, k, vals.get(k))
    x.glue, x.cut = vals.get("glue", ""), vals.get("cut", "")
    setf(x, ch, "scrap", "Scrap", int(vals.get("scrap") or 0))
    x.values, x.fails = vals, fails
    setf(x, ch, "passed", "Result", ok)
    review = str(p.get("review") or "")
    need(review in ("", "pending", "approved", "disapproved"), "Pick a review status.")
    if not ok and not review:
        review = "pending"
    setf(x, ch, "review", "Review", review)
    setf(x, ch, "by", "Checked by", str(p.get("by") or x.by).strip()[:120])
    setf(x, ch, "late_reason", "Late reason", str(p.get("late_reason") or "").strip())


def edit_co(x, p, ch):
    setf(x, ch, "by", "Who", str(p.get("by") or "").strip()[:120])
    setf(x, ch, "shift_name", "Shift", str(p.get("shift_name") or "").strip()[:60])
    a, b = keep(p.get("start"), x.start, "start"), keep(p.get("end"), x.end, "end")
    need(b >= a, "The end is before the start.")
    moved = (a, b) != (x.start, x.end)
    setf(x, ch, "start", "Start", a)
    setf(x, ch, "end", "End", b)
    if moved:
        setf(x, ch, "minutes", "Minutes", round((b - a).total_seconds() / 60))
    setf(x, ch, "ended", "How it ended", str(p.get("ended") or "").strip()[:40])
    setf(x, ch, "note", "Note", str(p.get("note") or "").strip())


def edit_morun(x, p, ch):
    setf(x, ch, "mo", "MO", str(p.get("mo") or "").strip()[:40])
    setf(x, ch, "item", "Item", str(p.get("item") or "").strip()[:40])
    for f, lbl in (("qty", "Qty"), ("scrap", "Scrap")):
        try:
            n = int(float(p.get(f) or 0))
        except ValueError:
            raise Need(f"Type the {lbl.lower()} as a number.")
        need(n >= 0, f"{lbl} can't be below 0.")
        setf(x, ch, f, lbl, n)
    a, b = keep(p.get("start"), x.start_time, "start"), keep(p.get("end"), x.end_time, "end")
    need(b >= a, "The end is before the start.")
    setf(x, ch, "start_time", "Start", a)
    setf(x, ch, "end_time", "End", b)


EDIT = {"downtime": edit_downtime, "log": edit_log, "qc": edit_qc, "co": edit_co, "morun": edit_morun}


@cmd("histEdit", PERM)
def hist_edit(p, ctx):
    kind = p.get("kind")
    x = get(kind, p.get("id"))
    ch = []
    EDIT[kind](x, p, ch)
    if not ch:
        return {"msg": "Nothing changed."}
    x.save()
    note(ctx, kind, x, ch, str(p.get("why") or "").strip())
    restat(log_of(kind, x))
    return {"msg": f"{LABEL[kind]} corrected: {len(ch)} change{'s' if len(ch) != 1 else ''}."}


@cmd("histDelete", PERM)
def hist_delete(p, ctx):
    kind = p.get("kind")
    x = get(kind, p.get("id"))
    need(not (kind == "log" and x.active), "That log is still running: end it at the kiosk first.")
    sh = log_of(kind, x)
    why = str(p.get("why") or "").strip()
    HistoryEdit.objects.create(t=ctx.now, by=ctx.by, kind=kind, obj_id=x.pk, line=getattr(x, "line", "") or "", field="Deleted",
                               old=summary(kind, x), new="", why=why)
    audit(ctx, f"Deleted {LABEL[kind].lower()} #{x.pk} on {getattr(x, 'line', '')}: {summary(kind, x)}" + (f" ({why})" if why else ""))
    x.delete()
    if kind != "log":
        restat(sh)
    return {"msg": f"{LABEL[kind]} deleted."}


@cmd("downSplit", PERM)
def down_split(p, ctx):
    """Split a downtime entry in two at a time (e.g. a stop left open overnight): each part gets its own reason."""
    x = get("downtime", p.get("id"))
    at = parse_dt(p.get("at"), "split")
    need(x.start_time < at < x.end_time, "Pick a time between the stop's start and end.")
    old_end = x.end_time
    x.end_time = at
    x.minutes = round((at - x.start_time).total_seconds() / 60)
    x.save()
    y = Downtime.objects.create(shift=x.shift, line=x.line, mo=x.mo, start_time=at, end_time=old_end,
                                minutes=round((old_end - at).total_seconds() / 60), reason=x.reason, notes=x.notes, by=x.by)
    why = str(p.get("why") or "").strip()
    note(ctx, "downtime", x, [("Split at", None, at), ("End", old_end, at)], why)
    HistoryEdit.objects.create(t=ctx.now, by=ctx.by, kind="downtime", obj_id=y.pk, line=y.line, field="Split from",
                               old=f"#{x.pk}", new=f"{fmt_t(at)} – {fmt_t(old_end)}", why=why)
    restat(x.shift)
    return {"msg": f"Split into {x.minutes} and {y.minutes} min.", "data": {"id": y.pk}}


def summary(kind, x):
    if kind == "downtime":
        return f"{show(x.start_time)} – {show(x.end_time)}, {x.minutes} min, {x.reason or 'no reason'}"
    if kind == "log":
        return f"{x.shift_name} {x.user_name or 'nobody'} {show(x.start)} – {show(x.end)}, {x.count} units"
    if kind == "qc":
        return f"{show(x.t)} {x.mo} {x.item} by {x.by}, {'pass' if x.passed else 'fail'}"
    if kind == "co":
        return f"{x.by or 'nobody'} {show(x.start)} – {show(x.end)}, {x.minutes} min"
    return f"MO {x.mo} {x.item} {x.qty} pcs {show(x.start_time)} – {show(x.end_time)}"
