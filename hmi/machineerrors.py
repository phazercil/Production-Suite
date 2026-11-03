"""Errors and alarms reported by the machines, kept so each downtime shows what the machine said while it was down.

Two ways in, set per machine in Admin > Connections > Machine data > Errors and alarms:
  - "feed": an error code (and optionally a message) that is part of the machine's reading. While the value stays
    the same it is one open error; when it changes or clears, the error is closed at that minute. Values such as
    0, blank, OK or "none" mean no error (more can be listed per machine).
  - "db":   the machine's alarm log table. Each new row (by the newest-row column) becomes one entry, with its own
    time when the table has a time column. On first use the suite starts from the newest row: old history isn't
    imported.
The table is read through the same read-only connection and rules as machine readings (hmi/machinedata.py).
"""
from datetime import datetime, timedelta, timezone as dt_tz

from django.utils import timezone

from . import integrations, machinedata

NO_ERROR = {"", "0", "0.0", "none", "ok", "no error", "noerror", "no fault", "null", "false", "-", "n/a", "normal", "clear", "cleared"}
SLACK = timedelta(minutes=2)  # an error just before a stop explains it too
LOG_BATCH = 200


def no_error_values(machine):
    extra = str((machine.error_src or {}).get("ok") or "")
    return NO_ERROR | {x.strip().lower() for x in extra.split(",") if x.strip()}


def is_error(machine, v):
    if v is None or isinstance(v, bool) and v is False:
        return False
    return machinedata.text(v).lower() not in no_error_values(machine)


# ------------------------------------------------------------------ from the reading
def track_feed(machine, code, text, t):
    """Open, change or close this machine's current error from this minute's reading."""
    from .models import MachineError
    cur = MachineError.objects.filter(line=machine.name, source="feed", end__isnull=True).order_by("-start").first()
    if not is_error(machine, code):
        if cur:
            cur.end = t
            cur.save(update_fields=["end"])
        return None
    code_s, text_s = machinedata.text(code), machinedata.text(text, 300) if is_error(machine, text) else ""
    if cur and cur.code == code_s:
        if text_s and cur.text != text_s:
            cur.text = text_s
            cur.save(update_fields=["text"])
        return cur
    if cur:
        cur.end = t
        cur.save(update_fields=["end"])
    return MachineError.objects.create(machine=machine, line=machine.name, start=t, code=code_s, text=text_s, source="feed")


# ------------------------------------------------------------------ from an alarm log table
def to_time(v, fallback):
    """A time from the log table: a DATETIME (plant time unless it says otherwise), ISO text, or epoch seconds/ms."""
    if isinstance(v, datetime):
        return timezone.make_aware(v) if timezone.is_naive(v) else v
    if isinstance(v, (int, float)) and v > 0:
        s = v / 1000 if v > 1e11 else v
        try:
            return datetime.fromtimestamp(s, dt_tz.utc)
        except (OverflowError, OSError, ValueError):
            return fallback
    if isinstance(v, str) and v.strip():
        try:
            d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
            return timezone.make_aware(d) if timezone.is_naive(d) else d
        except ValueError:
            return fallback
    return fallback


def _src(machine):
    e = dict(machine.error_src or {})
    e.setdefault("db", "machines")
    return e


def check_names(machine):
    """The alarm log settings, with every table and column name checked against the database's own list."""
    e = _src(machine)
    if not e.get("table"):
        raise integrations.ConnError("Pick the alarm log table.")
    if not e.get("orderCol"):
        raise integrations.ConnError("Pick the column that tells which alarm row is newest.")
    if not e.get("codeCol") and not e.get("textCol"):
        raise integrations.ConnError("Pick the column with the alarm code or message.")
    cols = machinedata.columns(e["db"], e["table"])
    for c in [e.get("codeCol"), e.get("textCol"), e.get("timeCol"), e["orderCol"], e.get("keyCol")]:
        if c and c not in cols:
            raise integrations.ConnError(f'The table "{e["table"]}" has no column "{c}".')
    return e


def new_log_rows(machine, mark):
    """Rows added to the alarm log since `mark` (None: just find where the log is now). Returns (rows, new mark)."""
    e = check_names(machine)
    db, table, order = e["db"], e.get("table", ""), e["orderCol"]
    want = [c for c in (e.get("codeCol"), e.get("textCol"), e.get("timeCol")) if c]
    q = machinedata._q
    where, params = [], []
    if e.get("keyCol"):
        where.append(f"{q(e['keyCol'])} = %s")
        params.append(str(e.get("keyVal", "")))
    conn = integrations._db(db)
    with conn.cursor() as c:
        if mark is None:
            c.execute(f"SELECT MAX({q(order)}) FROM {q(table)}" + (f" WHERE {' AND '.join(where)}" if where else ""), params)
            top = c.fetchone()[0]
            return [], ("" if top is None else machinedata.text(top, 60))
        if mark != "":
            where.append(f"{q(order)} > %s")
            params.append(mark)
        pick = list(dict.fromkeys([*want, order]))
        c.execute(f"SELECT {', '.join(q(x) for x in pick)} FROM {q(table)}" + (f" WHERE {' AND '.join(where)}" if where else "")
                  + f" ORDER BY {q(order)} LIMIT {LOG_BATCH}", params)
        rows = [dict(zip(pick, r)) for r in c.fetchall()]
    if rows:
        mark = machinedata.text(rows[-1][order], 60)
    return rows, mark


def read_log(machine, ls, t):
    """Store the alarm rows logged since last minute. Returns how many were new."""
    from .models import LineState, MachineError
    e = _src(machine)
    st = dict(ls.err_state or {})
    key = f"{e['db']}|{e.get('table')}|{e.get('orderCol')}|{e.get('keyCol')}|{e.get('keyVal')}"
    mark = st.get("mark") if st.get("key") == key else None
    try:
        rows, mark = new_log_rows(machine, mark)
        st.update(key=key, mark=mark, ok=True, err="", t=int(t.timestamp() * 1000))
    except Exception as ex:  # noqa: BLE001 - the alarm log never stops counting
        st.update(ok=False, err=str(ex)[:200], t=int(t.timestamp() * 1000))
        LineState.objects.filter(pk=ls.pk).update(err_state=st)
        ls.err_state = st
        return 0
    n = 0
    for r in rows:
        code = r.get(e.get("codeCol")) if e.get("codeCol") else ""
        text = r.get(e.get("textCol")) if e.get("textCol") else ""
        if not is_error(machine, code) and not is_error(machine, text):
            continue
        when = to_time(r.get(e["timeCol"]), t) if e.get("timeCol") else t
        MachineError.objects.create(machine=machine, line=machine.name, start=when, end=when, code=machinedata.text(code),
                                    text=machinedata.text(text, 300), source="log")
        n += 1
    LineState.objects.filter(pk=ls.pk).update(err_state=st)
    ls.err_state = st
    return n


def collect(machine, ls, sample, t):
    """This minute's errors for one machine (called by the ticker)."""
    e = machine.error_src or {}
    if e.get("type") == "feed" and sample is not None and "error" in sample:
        track_feed(machine, sample.get("error"), sample.get("errorText"), t)
    elif e.get("type") == "db":
        read_log(machine, ls, t)


# ------------------------------------------------------------------ for the screens
def as_json(x):
    from plant.util import ms
    return {"id": x.id, "line": x.line, "start": ms(x.start), "end": ms(x.end) if x.end else None, "code": x.code, "text": x.text, "src": x.source}


def for_downtime(downs, limit=20):
    """{downtime id: [errors the machine reported during it, or up to 2 minutes before]}"""
    from .models import MachineError
    downs = [d for d in downs if d.start_time and d.end_time]
    if not downs:
        return {}
    lo, hi = min(d.start_time for d in downs) - SLACK, max(d.end_time for d in downs)
    errs = list(MachineError.objects.filter(line__in={d.line for d in downs}, start__lte=hi)
                .exclude(end__lt=lo).order_by("start")[:5000])
    out = {}
    for d in downs:
        a = d.start_time - SLACK
        hits = [as_json(x) for x in errs if x.line == d.line and x.start <= d.end_time and (x.end is None or x.end >= a)]
        if hits:
            out[d.id] = hits[:limit]
    return out


def preview(machine, n=5):
    """Admin > Machine data > Test alarm log: the newest rows, as the suite would store them. Changes nothing."""
    e = check_names(machine)
    q = machinedata._q
    want = [c for c in (e.get("codeCol"), e.get("textCol"), e.get("timeCol")) if c]
    pick = list(dict.fromkeys([*want, e["orderCol"]]))
    where, params = "", []
    if e.get("keyCol"):
        where, params = f" WHERE {q(e['keyCol'])} = %s", [str(e.get("keyVal", ""))]
    conn = integrations._db(e["db"])
    with conn.cursor() as c:
        c.execute(f"SELECT {', '.join(q(x) for x in pick)} FROM {q(e['table'])}{where} ORDER BY {q(e['orderCol'])} DESC LIMIT {int(n)}", params)
        rows = [dict(zip(pick, r)) for r in c.fetchall()]
    now = timezone.now()
    return [{"code": machinedata.text(r.get(e.get("codeCol"))) if e.get("codeCol") else "",
             "text": machinedata.text(r.get(e.get("textCol")), 300) if e.get("textCol") else "",
             "at": (to_time(r.get(e["timeCol"]), None) or now).isoformat() if e.get("timeCol") and to_time(r.get(e["timeCol"]), None) else "",
             "row": machinedata.text(r[e["orderCol"]], 60)} for r in rows]
