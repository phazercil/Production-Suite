"""Supervisor › Timeline: one searchable history of what happened, from every record the suite keeps.

Each event says when, which line, who or what caused it, what happened and, for a correction, the value before and
after. Sources: shift logs (start and end), downtime, machine errors, QC fails, changeovers, sensor alarms,
maintenance requests and their notes, parts used, supervisor corrections (Supervisor › History edits, with the old and
new value and why), settings and admin changes (the audit log), and server warnings about connections (Admin › Health).
"""
from datetime import datetime
from datetime import timezone as dt_timezone

from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

KINDS = [("", "Everything"), ("shift", "Shift logs"), ("down", "Downtime"), ("error", "Machine errors"), ("qc", "QC fails"),
         ("co", "Changeovers"), ("alarm", "Sensor alarms"), ("maint", "Maintenance"), ("parts", "Parts used"),
         ("edit", "Corrections"), ("config", "Settings & admin"), ("system", "Connections & server")]
LIMIT = 3000   # per source, so a year of everything never loads at once


def _ev(t, kind, line, who, what, old="", new="", why="", href=""):
    return {"t": t, "kind": kind, "label": dict(KINDS)[kind], "line": line or "", "who": who or "", "what": what,
            "old": old or "", "new": new or "", "why": why or "", "href": href}


def events(f, kind="", q="", info=None):
    """Everything that happened in the dates, newest first. Each source loads at most LIMIT of its newest entries; if
    one has more, the list is cut back to the time from which every source is complete, and info (a dict, if given)
    gets {"cutoff": that time, "sources": the ones cut}, so the page can say so instead of silently leaving some out."""
    from hmi.models import AuditLog, ChangeoverPiece, Downtime, MachineError, QCCheck, ShiftRun, Ticket, TicketNote
    from maintenance.models import InventoryMove

    from .models import HistoryEdit
    A, B, line, who = f["A"], f["B"], f["line"], f["who"]
    want = (lambda k: not kind or kind == k)
    out = []

    def by_line(qs, field="line"):
        return qs.filter(**{field: line}) if line else qs
    cut = {}

    def take(qs, field, label):
        rows = list(qs.order_by(f"-{field}")[:LIMIT + 1])
        if len(rows) > LIMIT:
            rows = rows[:LIMIT]
            cut[label] = getattr(rows[-1], field)
        return rows
    if want("shift"):
        for s in take(by_line(ShiftRun.objects.filter(Q(start__gte=A, start__lt=B) | Q(end__gte=A, end__lt=B))), "start", "Shifts"):
            if A <= s.start < B:
                out.append(_ev(s.start, "shift", s.line, s.user_name or "No one logged in", f"Log started · {s.shift_name or 'shift'}",
                               href=f"/supervisor/log/{s.pk}/"))
            if s.end and A <= s.end < B and not s.active:
                out.append(_ev(s.end, "shift", s.line, s.user_name, f"Log ended · {s.count:,} made",
                               why=(s.end_issues or "")[:200], href=f"/supervisor/log/{s.pk}/"))
    if want("down"):
        for d in take(by_line(Downtime.objects.filter(start_time__gte=A, start_time__lt=B).select_related("shift")), "start_time", "Downtime"):
            out.append(_ev(d.start_time, "down", d.line, d.by or (d.shift.user_name if d.shift else ""),
                           f"Stopped {d.minutes} min · {d.reason or 'no reason given'}", why=d.notes))
    if want("error"):
        for e in take(by_line(MachineError.objects.filter(start__gte=A, start__lt=B)), "start", "Machine faults"):
            out.append(_ev(e.start, "error", e.line, "Machine", f"Error {e.code}" + (f": {e.text}" if getattr(e, "text", "") else "")))
    if want("qc"):
        for c in take(by_line(QCCheck.objects.filter(t__gte=A, t__lt=B, passed=False)), "t", "QC fails"):
            out.append(_ev(c.t, "qc", c.line, c.by, f"QC check failed · MO {c.mo} {c.item}", why=getattr(c, "late_reason", "")))
    if want("co"):
        for c in take(by_line(ChangeoverPiece.objects.filter(start__gte=A, start__lt=B)), "start", "Changeovers"):
            out.append(_ev(c.start, "co", c.line, c.by, f"Changeover {('MO ' + c.old_mo) if c.old_mo else ''} → {('MO ' + c.new_mo) if c.new_mo else '?'} · {c.minutes} min",
                           why=c.note))
    if want("alarm"):
        try:
            from sensors.models import SensorAlarm
            for a in take(by_line(SensorAlarm.objects.filter(start__gte=A, start__lt=B), "machine"), "start", "Sensor alarms"):
                out.append(_ev(a.start, "alarm", getattr(a, "machine", ""), "Sensor", getattr(a, "text", "") or "Sensor alarm",
                               why=f"cleared {timezone.localtime(a.end):%m/%d %H:%M}" if a.end else "still on"))
        except Exception:  # noqa: BLE001 - older databases
            pass
    if want("maint"):
        for t in take(by_line(Ticket.objects.filter(t__gte=A, t__lt=B), "machine"), "t", "Maintenance requests"):
            out.append(_ev(t.t, "maint", t.machine, t.by, f"Maintenance request #{t.pk}: {t.type} · {t.priority}", why=t.notes[:200]))
        for n in take(TicketNote.objects.filter(t__gte=A, t__lt=B).select_related("ticket").filter(**({"ticket__machine": line} if line else {})), "t", "Repair notes"):
            out.append(_ev(n.t, "maint", n.ticket.machine, n.by, f"Request #{n.ticket_id}: {n.text[:200]}"))
    if want("parts"):
        for m in take(by_line(InventoryMove.objects.filter(t__gte=A, t__lt=B).select_related("item"), "machine"), "t", "Parts"):
            verb = {"used": "Used", "added": "Added", "count": "Counted", "undone": "Used (taken back)", "back": "Took back"}.get(m.kind, m.kind)
            out.append(_ev(m.t, "parts", m.machine, m.by, f"{verb} {m.qty:g} {m.item.unit} {m.item.name}", why=m.note))
    if want("edit"):
        for e in take(by_line(HistoryEdit.objects.filter(t__gte=A, t__lt=B)), "t", "Corrections"):
            out.append(_ev(e.t, "edit", e.line, e.by, f"Corrected {e.kind} #{e.obj_id}: {e.field}", old=e.old, new=e.new, why=e.why))
    if want("config") and not line:
        for a in take(AuditLog.objects.filter(t__gte=A, t__lt=B), "t", "Settings changes"):
            out.append(_ev(a.t, "config", "", a.by, a.what))
    if want("system") and not line:
        try:
            from plant.health import KEY
            for e in cache.get(KEY) or []:
                t = datetime.fromtimestamp(e["t"], tz=dt_timezone.utc)
                if A <= t < B:
                    out.append(_ev(t, "system", "", e.get("where", "server"), f"{e['level'].title()}: {e['msg']}"))
        except Exception:  # noqa: BLE001
            pass
    if who:
        w = who.lower()
        out = [e for e in out if w in e["who"].lower()]
    if q:
        s = q.lower()
        out = [e for e in out if s in f"{e['what']} {e['why']} {e['old']} {e['new']} {e['who']} {e['line']}".lower()]
    if cut:
        cutoff = max(cut.values())
        out = [e for e in out if e["t"] >= cutoff]
        if info is not None:
            info.update(cutoff=cutoff, sources=sorted(cut))
    out.sort(key=lambda e: e["t"], reverse=True)
    return out


def csv_rows(rows):
    from plant.util import local
    yield ["Time", "What kind", "Line", "Who / what", "What happened", "Before", "After", "Why / note"]
    for e in rows:
        yield [local(e["t"]).strftime("%Y-%m-%d %H:%M:%S"), e["label"], e["line"], e["who"], e["what"], e["old"], e["new"], e["why"]]
