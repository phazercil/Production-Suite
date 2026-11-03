"""Machine readings from a database table, and the values every machine source can give.

Each pleater gives three values every minute, whatever its source (URL, push, InfluxDB or a database table):
  - pleats per minute (required),
  - filters cut: the machine's counter (optional; without it filters come from speed / pleats per filter),
  - the recipe loaded in the machine (optional; shown on the screens and kept with each minute).

A second, optional place gives the run signal for uptime: High = running, Low = stopped (or the other way round
for an active-low signal). It can be another field of the same reading, or a column of a table in a database.
Without it, the line counts as stopped when the speed is at or below the stop speed (Admin > Rules).

Database tables are read through the read-only connections (machine data database or asset database): the
connection is opened read-only, the query is a single SELECT the suite builds itself, and table and column
names are only ever ones the database itself listed, so nothing typed on the Admin page reaches the SQL.
"""
import time

from . import integrations
from .integrations import ConnError

TABLE_DBS = ("machines", "assets")  # databases a machine's table can be in
HIGH = {"1", "true", "high", "on", "run", "running", "yes", "y", "t", "start", "started", "auto"}
LOW = {"0", "false", "low", "off", "stop", "stopped", "no", "n", "f", "idle", "fault", "down"}
_cols_cache = {}


class FeedError(ConnError):
    """No usable reading. .state keeps the newest-row tracking so a stale table is still timed correctly."""

    def __init__(self, msg, state=None):
        super().__init__(msg)
        self.state = state


def signal(v):
    """High (True), Low (False), or None when the value can't be read as either."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (bytes, bytearray)):
        if len(v) == 1 and v[0] in (0, 1):  # MySQL BIT(1)
            return bool(v[0])
        v = v.decode("utf-8", "replace")
    if isinstance(v, (int, float)):
        return v > 0 if v == v else None
    s = str(v).strip().lower()
    if s in HIGH:
        return True
    if s in LOW:
        return False
    try:
        return float(s) > 0
    except ValueError:
        return None


def is_running(v, run_when="high"):
    s = signal(v)
    if s is None:
        return None
    return s if run_when != "low" else not s


def text(v, n=80):
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray)):
        v = v.decode("utf-8", "replace")
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()[:n]


def add_extras(machine, out, get):
    """Recipe, and the run signal when it is a field of the same reading. get(field) reads the source's record."""
    if machine.feed_recipe_field:
        r = get(machine.feed_recipe_field)
        if r is not None:
            out["recipe"] = text(r)
    e = machine.error_src or {}
    if e.get("type") == "feed" and e.get("field"):
        out["error"] = get(e["field"])
        if e.get("textField"):
            out["errorText"] = get(e["textField"])
    u = machine.uptime_src or {}
    if u.get("type") == "feed" and u.get("field"):
        out["run"] = is_running(get(u["field"]), u.get("runWhen"))
        if out["run"] is None:
            out["runErr"] = f'The run signal "{u["field"]}" is missing or isn\'t High/Low'
    return out


# ------------------------------------------------------------------ tables
def tables(db):
    """[{name, columns: [{name, type}]}] for the picker on the Admin page."""
    if db not in TABLE_DBS:
        raise ConnError("Pick the machine data database or the asset database.")
    return integrations.schema(db)["tables"]


def columns(db, table):
    """The table's column names, straight from the database (cached for a minute)."""
    key = (db, table)
    hit = _cols_cache.get(key)
    if hit and time.monotonic() - hit[0] < 60:
        return hit[1]
    if db not in TABLE_DBS:
        raise ConnError("Pick the machine data database or the asset database.")
    conn = integrations._db(db)
    with conn.cursor() as c:
        c.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                  [conn.settings_dict["NAME"], table])
        cols = [r[0] for r in c.fetchall()]
    if not cols:
        raise ConnError(f'There is no table "{table}" in {integrations.DB_NAMES[db].replace("The ", "the ")}.')
    _cols_cache[key] = (time.monotonic(), cols)
    return cols


def _q(name):
    return "`" + str(name).replace("`", "``") + "`"


def latest_row(db, table, want, order_col, key_col="", key_val=""):
    """The newest row (by order_col) of the table, limited to one machine when key_col is set.
    want: column names to read. Returns ({column: value}, order value)."""
    if not table:
        raise ConnError("Pick the table the machine's readings are in.")
    cols = columns(db, table)
    for c in [*want, order_col, *( [key_col] if key_col else [])]:
        if c not in cols:
            raise ConnError(f'The table "{table}" has no column "{c}".' if c else "Pick the column that tells which row is newest.")
    pick = list(dict.fromkeys([*want, order_col]))
    sql = f"SELECT {', '.join(_q(c) for c in pick)} FROM {_q(table)}"
    params = []
    if key_col:
        sql += f" WHERE {_q(key_col)} = %s"
        params.append(str(key_val))
    sql += f" ORDER BY {_q(order_col)} DESC LIMIT 1"
    conn = integrations._db(db)
    with conn.cursor() as c:
        c.execute(sql, params)
        r = c.fetchone()
    if not r:
        raise ConnError(f'No rows in "{table}"' + (f' where {key_col} is {key_val}' if key_col else ""))
    row = dict(zip(pick, r))
    return row, row[order_col]


def check_fresh(prev, order_val, t_ms, max_age):
    """A table that stops getting new rows means the machine (or its logger) stopped sending: after max_age
    minutes with the same newest row, the reading counts as missing, like any other dropped feed.
    Comparing the newest row with the one seen before avoids clock and time-zone differences between servers.
    Returns the state to keep for next minute."""
    mark = text(order_val, 60)
    since = prev["since"] if prev.get("row") == mark and isinstance(prev.get("since"), (int, float)) else t_ms
    if max_age and t_ms - since > max_age * 60000:
        raise FeedError(f"No new row for {round((t_ms - since) / 60000)} min (newest: {mark})", {"row": mark, "since": since})
    return {"row": mark, "since": since}


def _src(machine):
    s = dict(machine.feed_src or {})
    s.setdefault("db", "machines")
    try:
        s["maxAge"] = max(0, float(s.get("maxAge", 3) or 0))
    except (TypeError, ValueError):
        s["maxAge"] = 3
    return s


def fetch_feed(machine, prev, t_ms):
    """One reading from the machine's table: ({"ppm", "count", "recipe", "run"?}, freshness state)."""
    s = _src(machine)
    want = [machine.feed_ppm_field or "ppm"] + [c for c in (machine.feed_count_field, machine.feed_recipe_field) if c]
    u = machine.uptime_src or {}
    if u.get("type") == "feed" and u.get("field"):
        want.append(u["field"])
    e = machine.error_src or {}
    if e.get("type") == "feed":
        want += [c for c in (e.get("field"), e.get("textField")) if c]
    if not s.get("orderCol"):
        raise ConnError("Pick the column that tells which row is newest (a time stamp or an ID).")
    try:
        row, order_val = latest_row(s["db"], s.get("table", ""), want, s["orderCol"], s.get("keyCol", ""), s.get("keyVal", ""))
    except ConnError:
        raise
    except Exception as e:  # the database's own error (timeout, lost connection, permissions)
        raise ConnError(f"{integrations.DB_NAMES.get(s['db'], 'The database')}: {str(e)[:160]}")
    state = check_fresh(prev or {}, order_val, t_ms, s["maxAge"])
    field = machine.feed_ppm_field or "ppm"
    try:
        out = {"ppm": float(row[field])}
    except (TypeError, ValueError):
        raise FeedError(f'"{field}" is not a number ({text(row[field], 30)})', state)
    if machine.feed_count_field:
        out["count"] = row[machine.feed_count_field]
    add_extras(machine, out, row.get)
    return out, state


def fetch_run(machine, prev, t_ms):
    """The run signal from its own table: (True = running / False = stopped, freshness state)."""
    u = dict(machine.uptime_src or {})
    u.setdefault("db", "machines")
    try:
        max_age = max(0, float(u.get("maxAge", 0) or 0))  # a run signal that is only logged when it changes stays valid
    except (TypeError, ValueError):
        max_age = 0
    if not u.get("field"):
        raise ConnError("Pick the column with the run signal.")
    if not u.get("orderCol"):
        raise ConnError("Pick the column that tells which run-signal row is newest.")
    try:
        row, order_val = latest_row(u["db"], u.get("table", ""), [u["field"]], u["orderCol"], u.get("keyCol", ""), u.get("keyVal", ""))
    except ConnError:
        raise
    except Exception as e:
        raise ConnError(f"{integrations.DB_NAMES.get(u['db'], 'The database')}: {str(e)[:160]}")
    state = check_fresh(prev or {}, order_val, t_ms, max_age)
    run = is_running(row[u["field"]], u.get("runWhen"))
    if run is None:
        raise FeedError(f'The run signal "{u["field"]}" is {text(row[u["field"]], 30) or "blank"}, not High or Low', state)
    return run, state


def preview(machine_like, which="feed"):
    """Admin > Machine data > Test: the newest row and what the suite reads from it. machine_like is a Machine
    (possibly unsaved settings). Never changes anything."""
    t_ms = int(time.time() * 1000)
    if which == "uptime":
        run, st = fetch_run(machine_like, {}, t_ms)
        return {"run": run, "newest": st["row"]}
    out, st = fetch_feed(machine_like, {}, t_ms)
    return {**out, "newest": st["row"]}
