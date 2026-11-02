"""InfluxDB (Operator Kiosk > Admin > Connections > InfluxDB).

Two jobs, both optional:
  1. Machine data: a pleater's speed (and counter) can be read from InfluxDB with a query, instead of a URL
     or pushed readings. Machine source "InfluxDB query" in Admin > Connections > Machine data.
  2. Suite records: every minute reading, downtime and planned break is copied to a bucket, so Grafana
     dashboards built on InfluxDB use the same downtime rules as the suite (planned time left out).

InfluxDB 2.x (Flux, token, org) and 1.x (InfluxQL, database, user and password) are both supported.
InfluxDB 3 works through its 1.x-compatible API (InfluxQL), with the token as the password.
Like every other connection, the server must be on the plant network; requests never use a web proxy.
Only the standard library is used, so nothing extra has to be installed on the server.
"""
import base64
import csv
import io
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone as dt_tz

from django.conf import settings

from plant import liveconfig, netsafety

log = logging.getLogger("plant")

# Flux packages a query may import. Anything else (http, sql, socket, mqtt, slack...) could send data
# somewhere else from the InfluxDB server, so it is refused.
SAFE_FLUX_IMPORTS = {"strings", "math", "date", "array", "regexp", "types", "join", "timezone", "universe",
                     "influxdata/influxdb/schema", "influxdata/influxdb/v1", "experimental/aggregate"}
FLUX_WRITE_RE = re.compile(r"(?<![A-Za-z0-9_.])(to|wideTo)\s*\(|\.to\s*\(", re.I)
FLUX_IMPORT_RE = re.compile(r'^\s*import\s+"([^"]+)"', re.M)
INFLUXQL_BAD_RE = re.compile(r"\b(INTO|DROP|DELETE|CREATE|ALTER|GRANT|REVOKE|KILL|SET\s+PASSWORD)\b", re.I)
NAME_RE = re.compile(r"^[A-Za-z0-9_./-]{1,120}$")
MAX_ROWS = 2000


class InfluxError(Exception):
    pass


# ------------------------------------------------------------------ settings
def config():
    liveconfig.refresh()
    s = settings.SUITE
    return {"url": (s.get("INFLUX_URL") or "").rstrip("/"), "version": s.get("INFLUX_VERSION") or "2", "org": s.get("INFLUX_ORG") or "",
            "token": s.get("INFLUX_TOKEN") or "", "user": s.get("INFLUX_USER") or "", "db": s.get("INFLUX_DB") or "",
            "out": s.get("INFLUX_WRITE_BUCKET") or ""}


def ready(cfg=None):
    c = cfg or config()
    if not c["url"]:
        return False
    return bool(c["token"] and c["org"]) if c["version"] == "2" else True


def out_ready(cfg=None):
    c = cfg or config()
    return ready(c) and bool(c["out"])


# ------------------------------------------------------------------ HTTP
def _open(cfg, method, path, params=None, body=None, headers=None, timeout=8):
    from .integrations import _PRIVATE_OPENER  # plant network only, no proxy, no redirects off the network
    if not cfg["url"]:
        raise InfluxError("InfluxDB is not set up (Admin > Connections > InfluxDB).")
    url = cfg["url"] + path + ("?" + urllib.parse.urlencode(params) if params else "")
    try:
        netsafety.check_url(url, "The InfluxDB address")
    except netsafety.NotPrivate as e:
        raise InfluxError(str(e))
    h = {"User-Agent": "production-suite"}
    if cfg["version"] == "2":
        if cfg["token"]:
            h["Authorization"] = "Token " + cfg["token"]
    elif cfg["user"] or cfg["token"]:
        h["Authorization"] = "Basic " + base64.b64encode(f"{cfg['user']}:{cfg['token']}".encode()).decode()
    h.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with _PRIVATE_OPENER.open(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            raw = e.read().decode(errors="replace")
            j = json.loads(raw) if raw.strip().startswith("{") else {}
            detail = j.get("message") or j.get("error") or raw.strip()[:200]
        except Exception:
            pass
        hint = {401: " Check the token or password.", 403: " The token isn't allowed to do this.", 404: " Check the bucket, database and organization."}.get(e.code, "")
        raise InfluxError(f"InfluxDB answered {e.code}{': ' + detail if detail else ''}.{hint}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise InfluxError(f"Couldn't reach InfluxDB at {cfg['url']}: {getattr(e, 'reason', e)}")


# ------------------------------------------------------------------ reading
def check_query(q, version):
    """Only queries that read. Raises InfluxError with the reason otherwise."""
    q = str(q or "").strip()
    if not q:
        raise InfluxError("Type a query.")
    if len(q) > 4000:
        raise InfluxError("That query is too long (4000 characters at most).")
    if version == "2":
        for name in FLUX_IMPORT_RE.findall(q):
            if name not in SAFE_FLUX_IMPORTS:
                raise InfluxError(f'Queries can\'t import "{name}": only reading is allowed here.')
        if FLUX_WRITE_RE.search(q):
            raise InfluxError("Queries can't write (to() is not allowed here).")
    else:
        body = re.sub(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"", "''", q)  # ignore words inside quotes
        if ";" in body.rstrip().rstrip(";"):
            raise InfluxError("Use one statement only.")
        if not re.match(r"^\s*(SELECT|SHOW)\b", body, re.I):
            raise InfluxError("Only SELECT and SHOW queries are allowed.")
        if INFLUXQL_BAD_RE.search(body):
            raise InfluxError("Only reading is allowed (no INTO, DROP, DELETE and the like).")
    return q


def _num(v):
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return v
    try:
        f = float(v)
        return int(f) if f.is_integer() and "." not in str(v) and "e" not in str(v).lower() else f
    except (TypeError, ValueError):
        return v


def parse_flux_csv(text):
    """Flux CSV (header on, no annotations) -> list of row dicts. Tables are separated by blank lines."""
    rows, header = [], None
    for rec in csv.reader(io.StringIO(text)):
        if not rec or all(not x for x in rec):
            header = None
            continue
        if header is None:
            header = rec
            continue
        if rec[0] == "#":  # an annotation row, if the server sends them anyway
            continue
        row = {k: _num(v) for k, v in zip(header, rec) if k and k not in ("result", "table")}
        if row.get("error"):
            raise InfluxError(f"InfluxDB: {row['error']}")
        rows.append(row)
        if len(rows) >= MAX_ROWS:
            break
    return rows


def query(q, cfg=None):
    c = cfg or config()
    q = check_query(q, c["version"])
    if c["version"] == "2":
        body = json.dumps({"query": q, "type": "flux", "dialect": {"header": True, "annotations": [], "delimiter": ","}}).encode()
        _, _, raw = _open(c, "POST", "/api/v2/query", {"org": c["org"]}, body,
                          {"Content-Type": "application/json", "Accept": "application/csv"})
        return parse_flux_csv(raw.decode("utf-8", errors="replace"))
    params = {"q": q, "epoch": "ms"}
    if c["db"]:
        params["db"] = c["db"]
    _, _, raw = _open(c, "GET", "/query", params, headers={"Accept": "application/json"})
    try:
        data = json.loads(raw.decode() or "{}")
    except json.JSONDecodeError:
        raise InfluxError("InfluxDB did not send JSON.")
    rows = []
    for res in data.get("results") or []:
        if res.get("error"):
            raise InfluxError(f"InfluxDB: {res['error']}")
        for ser in res.get("series") or []:
            cols, tags = ser.get("columns") or [], ser.get("tags") or {}
            for vals in ser.get("values") or []:
                rows.append({**tags, **dict(zip(cols, vals))})
                if len(rows) >= MAX_ROWS:
                    return rows
    return rows


def _row_time(row):
    v = row.get("_time", row.get("time"))
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000, tz=dt_tz.utc)
    s = str(v).strip().replace("Z", "+00:00")
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)  # Python reads microseconds, InfluxDB sends nanoseconds
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def latest_sample(rows):
    """The newest values from query rows. Rows shaped like Flux output (_field/_value) are folded into one
    reading per field; other rows (InfluxQL, or a pivoted Flux query) use their column names."""
    if not rows:
        return {}, None
    if any("_field" in r and "_value" in r for r in rows):
        out, newest = {}, {}
        for r in rows:
            f, t = r.get("_field"), _row_time(r)
            if f is None:
                continue
            if f not in newest or newest[f] is None or (t and t >= newest[f]):
                out[f], newest[f] = r.get("_value"), t
        times = [t for t in newest.values() if t]
        when = max(times) if times else None
        return out, when
    best = max(rows, key=lambda r: _row_time(r) or datetime.min.replace(tzinfo=dt_tz.utc))
    return dict(best), _row_time(best)


def default_query(machine_name, version):
    n = str(machine_name).replace('"', "").replace("'", "")
    if version == "2":
        return (f'from(bucket: "machines")\n  |> range(start: -3m)\n  |> filter(fn: (r) => r._measurement == "pleater" and r.line == "{n}")\n'
                f'  |> filter(fn: (r) => r._field == "ppm" or r._field == "count")\n  |> last()')
    return f"SELECT last(\"ppm\") AS ppm, last(\"count\") AS count FROM \"pleater\" WHERE \"line\" = '{n}' AND time > now() - 3m"


def fetch_feed(machine, now=None):
    """One reading for a machine whose source is an InfluxDB query: {"ppm": .., "count": ..}."""
    from django.utils import timezone
    cfg = config()
    if not ready(cfg):
        raise InfluxError("InfluxDB is not set up (Admin > Connections > InfluxDB).")
    q = (machine.feed_query or "").replace("{machine}", machine.name)
    rows = query(q, cfg)
    if not rows:
        raise InfluxError("The InfluxDB query returned nothing (no recent reading?)")
    sample, when = latest_sample(rows)
    now = now or timezone.now()
    if when and (now - when).total_seconds() > 150:
        raise InfluxError(f"The newest InfluxDB reading is {round((now - when).total_seconds() / 60)} min old")
    field = machine.feed_ppm_field or "ppm"
    ppm = sample.get(field)
    if ppm is None and field == "ppm" and "_value" in sample:
        ppm = sample["_value"]
    if ppm is None:
        raise InfluxError(f'No "{field}" in the InfluxDB result (it has: {", ".join(list(sample)[:8])})')
    try:
        out = {"ppm": float(ppm)}
    except (TypeError, ValueError):
        raise InfluxError(f'"{field}" is not a number ({str(ppm)[:30]})')
    if machine.feed_count_field and sample.get(machine.feed_count_field) is not None:
        out["count"] = sample[machine.feed_count_field]
    return out


def test(cfg=None):
    """Sign in and list buckets / databases. Never writes."""
    c = cfg or config()
    if c["version"] == "2":
        _, _, raw = _open(c, "GET", "/health", timeout=5)
        try:
            ver = (json.loads(raw.decode() or "{}") or {}).get("version", "")
        except json.JSONDecodeError:
            ver = ""
        try:
            names = [r.get("name") for r in query('buckets() |> keep(columns: ["name"])', c) if r.get("name")]
        except InfluxError as e:
            raise InfluxError(f"InfluxDB {ver} is answering, but: {e}")
        names = [n for n in names if not str(n).startswith("_")]
        msg = f"InfluxDB {ver} · {len(names)} bucket{'s' if len(names) != 1 else ''}".replace("InfluxDB  ", "InfluxDB ")
        if c["out"] and c["out"] not in names:
            msg += f'. Note: there is no bucket named "{c["out"]}" yet; create it in InfluxDB before the suite writes there'
        return {"ok": True, "msg": msg, "buckets": names[:100], "version": ver}
    _, hdr, _ = _open(c, "GET", "/ping", timeout=5)
    ver = hdr.get("X-Influxdb-Version") or hdr.get("x-influxdb-version") or ""
    try:
        names = [r.get("name") for r in query("SHOW DATABASES", c) if r.get("name")]
    except InfluxError as e:
        raise InfluxError(f"InfluxDB {ver} is answering, but: {e}")
    names = [n for n in names if not str(n).startswith("_")]
    msg = f"InfluxDB {ver} · {len(names)} database{'s' if len(names) != 1 else ''}"
    if c["db"] and names and c["db"] not in names:
        msg += f'. Note: there is no database named "{c["db"]}"'
    return {"ok": True, "msg": msg, "buckets": names[:100], "version": ver}


# ------------------------------------------------------------------ writing the suite's records
def _esc_key(v):
    return re.sub(r"([,= ])", r"\\\1", str(v)).replace("\n", " ")


def _esc_str(v):
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def _field(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return f"{v}i"
    if isinstance(v, float):
        return repr(v)
    return _esc_str(v)


def point(measurement, tags, fields, t):
    tg = "".join(f",{_esc_key(k)}={_esc_key(v)}" for k, v in sorted(tags.items()) if v not in (None, ""))
    fl = ",".join(f"{_esc_key(k)}={_field(v)}" for k, v in fields.items() if v is not None)
    return f"{_esc_key(measurement)}{tg} {fl} {int(t.timestamp() * 1000)}"


def write(lines, cfg=None):
    c = cfg or config()
    if not lines:
        return
    if not NAME_RE.match(c["out"] or ""):
        raise InfluxError("Set the bucket the suite writes to (Admin > Connections > InfluxDB).")
    body = ("\n".join(lines)).encode()
    if c["version"] == "2":
        _open(c, "POST", "/api/v2/write", {"org": c["org"], "bucket": c["out"], "precision": "ms"}, body,
              {"Content-Type": "text/plain; charset=utf-8"}, timeout=15)
    else:
        _open(c, "POST", "/write", {"db": c["out"], "precision": "ms"}, body, {"Content-Type": "text/plain; charset=utf-8"}, timeout=15)


def minute_point(r, stop):
    stopped = r.ppm <= stop
    return point("suite_minute", {"line": r.line, "shift": r.shift.shift_name if r.shift_id and r.shift else ""},
                 {"ppm": int(r.ppm), "units": int(r.units or 0), "run": int(not stopped), "planned": int(bool(r.planned)),
                  "down": int(stopped and not r.planned), "counted": int(not stopped or not r.planned)}, r.t)


def _state():
    from plant.models import PlantSettings
    return PlantSettings.load().conn.get("influxOut") or {}


def _save_state(**kw):
    from django.utils import timezone

    from plant.models import PlantSettings
    from plant.util import ms
    st = PlantSettings.load()
    cur = dict(st.conn.get("influxOut") or {})
    cur.update(kw, t=ms(timezone.now()))
    st.conn["influxOut"] = cur
    st.save(update_fields=["conn"])
    return cur


def push(now=None, batch=5000, rounds=4):
    """Copy new minute readings, plus downtime and planned breaks of the last 2 days, to the out bucket.
    Rewriting a point with the same time and tags replaces it, so sending a downtime again after its reason
    was filled in just updates it. Returns how many points were written."""
    from django.utils import timezone

    from .engine import rules
    from .models import Downtime, PlannedTime, PpmReading
    cfg = config()
    if not out_ready(cfg):
        return 0
    now = now or timezone.now()
    stop = rules()["stopPpm"]
    st = _state()
    mark = st.get("mark")
    if mark is None:
        first = PpmReading.objects.filter(t__gte=now - timedelta(days=7)).order_by("id").values_list("id", flat=True).first()
        mark = (first - 1) if first else (PpmReading.objects.order_by("-id").values_list("id", flat=True).first() or 0)
    sent = 0
    try:
        for _ in range(rounds):
            rows = list(PpmReading.objects.filter(id__gt=mark).select_related("shift").order_by("id")[:batch])
            if not rows:
                break
            write([minute_point(r, stop) for r in rows], cfg)
            mark, sent = rows[-1].id, sent + len(rows)
            _save_state(mark=mark)
            if len(rows) < batch:
                break
        since = now - timedelta(days=2)
        lines = [point("suite_downtime", {"line": d.line},
                       {"minutes": float(d.minutes), "reason": d.reason or "", "mo": d.mo or "", "end": int(d.end_time.timestamp() * 1000)}, d.start_time)
                 for d in Downtime.objects.filter(start_time__gte=since)]
        lines += [point("suite_planned", {"line": p.line, "kind": p.kind},
                        {"minutes": round((p.end - p.start).total_seconds() / 60, 1), "end": int(p.end.timestamp() * 1000)}, p.start)
                  for p in PlannedTime.objects.filter(start__gte=since)]
        for i in range(0, len(lines), batch):
            write(lines[i:i + batch], cfg)
        sent += len(lines)
    except InfluxError as e:
        _save_state(ok=False, msg=str(e)[:200], mark=mark)
        raise
    _save_state(ok=True, msg=f"{sent} points written to {cfg['out']}", mark=mark)
    return sent
