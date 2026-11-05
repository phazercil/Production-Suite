"""Connections the server makes on behalf of the screens: machine feeds, S2K, people lists,
Paycom, Grafana, the webhook and the read-only asset database. Screens only ever talk to Django."""
import base64
import json
import re
import ssl
import time
import urllib.error
import urllib.request

from django.conf import settings
from django.db import connections

from plant import liveconfig, netsafety
from plant.models import Machine, PlantSettings
from plant.util import ms, now


class ConnError(Exception):
    pass


def set_conn(key, ok, msg):
    st = PlantSettings.load()
    st.conn[key] = {"ok": ok, "msg": msg[:200], "t": ms(now())}
    st.save(update_fields=["conn"])
    return ok, msg


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only if it stays on the private network."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            netsafety.check_url(newurl, "A redirect")
        except netsafety.NotPrivate as e:
            raise ConnError(f"{req.full_url} tried to send the server to another address: {e}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# Private-network requests go direct: never through a web proxy, and never redirected off the network.
_PRIVATE_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _CheckedRedirect(),
                                              urllib.request.HTTPSHandler(context=ssl.create_default_context()))
# Only Paycom (a cloud service) uses the system's normal internet route, including any company proxy.
_PUBLIC_OPENER = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl.create_default_context()))


def _request(url, data=None, headers=None, timeout=6, auth=None, public_ok=False):
    if not public_ok:
        try:
            netsafety.check_url(url)
        except netsafety.NotPrivate as e:
            raise ConnError(str(e))
    h = {"Accept": "application/json", "User-Agent": "production-suite"}
    h.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data, default=str).encode()
        h["Content-Type"] = "application/json"
    if auth:
        h["Authorization"] = "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
    req = urllib.request.Request(url, data=body, headers=h, method="POST" if body is not None else "GET")
    opener = _PUBLIC_OPENER if public_ok else _PRIVATE_OPENER
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw.decode() or "null") if raw else None
    except urllib.error.HTTPError as e:
        raise ConnError(f"{url} answered {e.code} {e.reason}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ConnError(f"Could not reach {url}: {getattr(e, 'reason', e)}")
    except json.JSONDecodeError:
        raise ConnError(f"{url} did not send JSON")


def get_json(url):
    return _request(url)


def pick(obj, path):
    """Read a field by name or dotted path (data.speed)."""
    cur = obj
    for part in str(path or "").split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def fetch_feed(machine):
    data = _request(machine.feed_url, timeout=4)
    ppm = pick(data, machine.feed_ppm_field or "ppm")
    if ppm is None:
        raise ConnError(f'No "{machine.feed_ppm_field}" field in the reading')
    try:
        out = {"ppm": float(ppm)}
    except (TypeError, ValueError):
        raise ConnError(f'"{machine.feed_ppm_field}" is not a number ({str(ppm)[:30]})')
    if machine.feed_count_field:
        c = pick(data, machine.feed_count_field)
        if c is not None:
            out["count"] = c
    from . import machinedata
    return machinedata.add_extras(machine, out, lambda f: pick(data, f))


def post_webhook(url, events):
    _request(url, data={"source": "production-suite", "events": events}, timeout=8)


# ------------------------------------------------------------------ Paycom
def paycom_ready():
    liveconfig.refresh()
    s = settings.SUITE
    return bool(s["PAYCOM_SID"] and s["PAYCOM_TOKEN"])


def _paycom_raw():
    s = settings.SUITE
    if not s["PAYCOM_URL"].startswith("https://"):
        raise ConnError("PAYCOM_URL must start with https://")
    data = _request(s["PAYCOM_URL"], auth=(s["PAYCOM_SID"], s["PAYCOM_TOKEN"]), timeout=20, public_ok=True)  # the one internet connection
    rows = data.get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ConnError("Paycom did not send an employee list")
    return rows


def _first(r, names):
    for n in names:
        if n and r.get(n) not in (None, ""):
            return r[n]
    return ""


def paycom_employees():
    s = settings.SUITE
    out = []
    for r in _paycom_raw():
        code = str(_first(r, ["eecode", "employee_code", "ee_code", "clockseq"]))
        name = _first(r, ["name", "employee_name"]) or " ".join(x for x in (str(r.get("firstname") or ""), str(r.get("lastname") or "")) if x)
        status = str(_first(r, ["employee_status", "status", "empstatus"])).lower()
        out.append({"code": code, "name": name.strip() or code,
                    "active": not any(w in status for w in ("term", "inactive", "deceased")),
                    "department": _first(r, [s["PAYCOM_FIELD_DEPT"], "department_description", "department", "dept", "deptdesc"]),
                    "position": _first(r, [s["PAYCOM_FIELD_POSITION"], "position_title", "position", "positiondesc", "job_title"]),
                    "badge": str(_first(r, [s["PAYCOM_FIELD_BADGE"], "badge", "badge_number", "clocknumber"]))})
    return out


def paycom_fields():
    rows = _paycom_raw()
    fields = sorted({k for r in rows[:50] for k in r.keys()})
    return {"fields": fields, "sample": paycom_employees()[:8]}


# ------------------------------------------------------------------ read-only outside databases (asset database, S2K)
DB_NAMES = {"assets": "The asset database", "s2k": "The S2K database", "machines": "The machine data database"}


def assets_ready():
    liveconfig.refresh()
    return "assets" in connections.databases


def s2k_db_ready():
    liveconfig.refresh()
    return "s2k" in connections.databases


def machines_db_ready():
    liveconfig.refresh()
    return "machines" in connections.databases


def _db(db):
    if db not in DB_NAMES:
        raise ConnError("Unknown database.")
    liveconfig.refresh()
    if db not in connections.databases:
        raise ConnError(f"{DB_NAMES[db]} is not set up yet. Add it in Operator Kiosk > Admin > Connections.")
    if not connections.databases[db].get("SUITE_INTERNET"):   # opted in on the Admin page: may be outside the plant network
        try:
            netsafety.check_host(connections.databases[db]["HOST"], DB_NAMES[db])
        except netsafety.NotPrivate as e:
            raise ConnError(str(e))
    return liveconfig.connection(db)


def _assets_private():  # older name, kept for callers
    _db("assets")


PARAM = re.compile(r"(?<!:):([A-Za-z_]\w*)")
# Anything that could change data, lock rows in the other system, or write files is refused before it is sent.
NOT_READ_ONLY = re.compile(r"(?is)\b(insert|update|delete|create|alter|drop|truncate|rename|grant|revoke|"
                           r"into\s+(outfile|dumpfile|@)|for\s+update|for\s+share|lock\s+in\s+share\s+mode|"
                           r"get_lock|release_lock|sleep|benchmark)\b")


def _strip_literals(sql):
    """The query without quoted text and comments, so a word inside a string doesn't count."""
    sql = re.sub(r"(?s)/\*.*?\*/", " ", sql)
    sql = re.sub(r"(?m)(--\s|#).*$", " ", sql)
    return re.sub(r"'(?:[^'\\]|\\.|'')*'|\"(?:[^\"\\]|\\.)*\"|`[^`]*`", " ", sql)


def check_select(sql, what="the asset database"):
    sql = (sql or "").strip().rstrip(";").strip()
    if "/*!" in sql or "/*M!" in sql:
        raise ConnError("Version comments (/*! ... */) are not allowed.")
    if not re.match(r"(?is)^(select|with)\b", sql):
        raise ConnError(f"Only SELECT queries can run against {what}.")
    bare = _strip_literals(sql)
    if ";" in bare:
        raise ConnError("Use a single SELECT statement.")
    m = NOT_READ_ONLY.search(bare)
    if m:
        raise ConnError(f'"{m.group(0).strip().upper()}" is not allowed. The suite only reads from {what}.')
    return sql


def kind_of(db):
    """mysql, mssql or postgres: what kind of database the alias is (Admin › Connections detects it)."""
    liveconfig.refresh()
    return (connections.settings.get(db) or {}).get("SUITE_KIND") or "mysql"


def quoters(db):
    """(column, table) name quoting for that database's SQL dialect."""
    from plant import dbkinds
    k = kind_of(db)
    return (lambda n: dbkinds.quote(k, n)), (lambda n: dbkinds.table(k, n))


def run_saved(sql, params, limit=500, db="assets"):
    sql = check_select(sql, DB_NAMES.get(db, "that database").replace("The ", "the "))
    conn = _db(db)
    names = PARAM.findall(sql)
    q = PARAM.sub("%s", sql.replace("%", "%%"))
    t0 = time.time()
    with conn.cursor() as c:
        c.execute(q, [str((params or {}).get(n, "")) for n in names])
        cols = [d[0] for d in c.description or []]
        rows = c.fetchmany(limit + 1)
    out = [dict(zip(cols, [v if isinstance(v, (int, float, str)) or v is None else str(v) for v in r])) for r in rows[:limit]]
    return {"columns": cols, "rows": out, "ms": round((time.time() - t0) * 1000), "truncated": len(rows) > limit}


def schema(db="assets"):
    from plant import dbkinds
    conn = _db(db)
    name = conn.settings_dict["NAME"]
    sql, params = dbkinds.schema_sql(kind_of(db))
    with conn.cursor() as c:
        c.execute(sql, params(name))
        tables = {}
        for t, col, typ in c.fetchall():
            tables.setdefault(t, []).append({"name": col, "type": typ})
    return {"database": name, "tables": [{"name": t, "columns": cols} for t, cols in tables.items()]}


def preview(table, db="assets", where=None):
    """The first rows of a table (only those matching a source card's filters, when given)."""
    names = {t["name"] for t in schema(db)["tables"]}
    if table not in names:
        raise ConnError("Unknown table.")
    from plant import dbkinds
    k = kind_of(db)
    w = " AND ".join(f"{dbkinds.quote(k, f['col'])} = {dbkinds.literal(k, f.get('val', ''))}" for f in (where or []) if f.get("col"))
    return run_saved(dbkinds.top(k, f"* FROM {dbkinds.table(k, table)}" + (f" WHERE {w}" if w else ""), 20), {}, limit=20, db=db)


def s2k_db_rows(sql):
    """Open MOs from the S2K database, with column names lower-cased (mo, item, qty, due, process, mfg, hot)."""
    r = run_saved(sql, {}, limit=5000, db="s2k")
    rows = [{str(k).lower(): v for k, v in row.items()} for row in r["rows"]]
    if rows and "mo" not in rows[0]:
        raise ConnError("The S2K query needs a column named mo (use AS mo), plus item and qty.")
    return rows, r["truncated"]


# ------------------------------------------------------------------ connection tests
def test(key):
    st = PlantSettings.load()
    s = st.settings
    try:
        if key == "store":
            with connections["default"].cursor() as c:
                c.execute("SELECT 1")
            return set_conn(key, True, f"{connections['default'].vendor} database answering")
        if key and key.startswith("feed:"):
            m = Machine.objects.filter(name=key[5:]).first()
            if not m:
                return set_conn(key, False, "Machine not found")
            if m.feed_type == "http":
                r = fetch_feed(m)
                return set_conn(key, True, f"{r['ppm']:g} P/m")
            if m.feed_type == "influx":
                from . import influx
                try:
                    r = influx.fetch_feed(m)
                except influx.InfluxError as e:
                    return set_conn(key, False, str(e))
                return set_conn(key, True, f"{r['ppm']:g} P/m from InfluxDB")
            if m.feed_type == "db":
                from . import machinedata
                r = machinedata.preview(m)
                return set_conn(key, True, f"{r['ppm']:g} P/m" + (f" · recipe {r['recipe']}" if r.get("recipe") else "")
                                + (f" · {'running' if r['run'] else 'stopped'}" if isinstance(r.get("run"), bool) else ""))
            if m.feed_type == "push":
                live = getattr(m, "live", None)
                if live and live.pushed_at and (now() - live.pushed_at).total_seconds() < 150:
                    return set_conn(key, True, "Readings arriving")
                return set_conn(key, False, "No reading pushed in the last 2½ minutes")
            return set_conn(key, m.feed_type == "demo", "Demo replay" if m.feed_type == "demo" else "No feed set")
        if key == "grafana":
            g = s["grafana"]
            if not g.get("url"):
                return set_conn(key, False, "No Grafana URL")
            r = _request(g["url"].rstrip("/") + "/api/health")
            return set_conn(key, True, f"Grafana {(r or {}).get('version', '')} answering")
        if key in ("influx", "influxOut"):
            from . import influx
            try:
                if key == "influxOut":
                    if not influx.out_ready():
                        return set_conn(key, False, "Not writing: no bucket set")
                    n = influx.push()
                    return True, f"{n} points written"
                return set_conn(key, True, influx.test()["msg"])
            except influx.InfluxError as e:
                return set_conn(key, False, str(e)) if key == "influx" else (False, str(e))
        if key == "assets":
            if not assets_ready():
                return set_conn(key, False, "ASSET_DB_* not set in .env")
            return set_conn(key, True, f"{len(schema()['tables'])} tables")
        if key == "machines":
            if not machines_db_ready():
                return set_conn(key, False, "Not set up (Admin > Connections > Databases and accounts)")
            return set_conn(key, True, f"{len(schema('machines')['tables'])} tables (read-only)")
        if key == "paycom":
            if not paycom_ready():
                return set_conn(key, False, "PAYCOM_SID / PAYCOM_TOKEN not set in .env")
            return set_conn(key, True, f"{len(_paycom_raw())} employees")
        if key == "s2k" and s.get("s2kSource") == "db":
            if not s2k_db_ready():
                return set_conn(key, False, "S2K database not set up (run the installer again)")
            from .models import SavedQuery
            q = SavedQuery.objects.filter(name="s2k_mos").first()
            if not q:
                return set_conn(key, True, f"Connected, {len(schema('s2k')['tables'])} tables · no query saved yet")
            rows, _ = s2k_db_rows(q.sql)
            return set_conn(key, True, f"{len(rows)} open MO rows (read-only)")
        if key in ("s2k", "people"):
            url = s["s2kUrl" if key == "s2k" else "peopleUrl"]
            if not url:
                return set_conn(key, False, "No URL set")
            r = _request(url)
            return set_conn(key, isinstance(r, list), f"{len(r)} rows" if isinstance(r, list) else "Answer is not a list")
        if key == "webhook":
            if not s.get("webhook"):
                return set_conn(key, False, "No URL set")
            post_webhook(s["webhook"], [{"kind": "test", "t": ms(now())}])
            return set_conn(key, True, "Test event accepted")
        return False, "Unknown connection"
    except ConnError as e:
        return set_conn(key, False, str(e))
    except Exception as e:  # database drivers raise their own types
        return set_conn(key, False, str(e))
