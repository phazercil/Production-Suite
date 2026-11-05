"""Operator Kiosk > Admin, built on the server.

Each tab is a template under templates/hmi/k/admin/. Settings change field by field (each field posts on change);
forms with several fields (a database login, an alert contact, the plant network, a query) post as a whole and get
their panel back with the result. Nothing is decided in the browser: building SQL from picked columns, checking a
query, testing a connection and every rule are done here or in hmi/engine.py.
"""
import json
import time
from urllib.parse import quote

from django.http import HttpResponse
from django.template.loader import render_to_string
from django.utils.html import escape

from plant.models import MO, Machine, Person
from suite import thin

from . import engine, integrations

LEAD_TABS = {"shifts", "reasons", "checklists", "maintenance", "parts", "rules"}   # a shift lead can change these; the rest need an Admin
TABS = [("machines", "Machines"), ("connections", "Connections"), ("connector", "Connector builder"), ("codata", "Changeover data"), ("shifts", "Shifts & breaks"),
        ("reasons", "Downtime reasons"), ("checklists", "Checklists"), ("qc", "QC checks"), ("maintenance", "Maintenance lists"), ("parts", "Parts & specs"),
        ("rules", "Rules"), ("alerts", "Alerts"), ("network", "Network"), ("updates", "Updates"), ("backup", "Backup"),
        ("permissions", "Permissions")]
LISTS = {"reasons": [("reasons", "Downtime reasons", "The list operators pick from when the downtime prompt asks why the line stopped. Add, rename, "
                      "reorder or remove reasons here; the order here is the order on the screen. Maintenance and shift leads can also type a "
                      "reason that isn't listed, or change a reason already entered. Removing a reason keeps it on stops already logged. "
                      "When the prompt appears is set in Rules.")],
         "checklists": [("safety", "QC and safety checklist", "Every item must be checked before the line can start."),
                        ("pm", "Shift PM checklist", "Recorded at start of shift; not required to start."),
                        ("coSteps", "Changeover steps", "Shown in order during a changeover. All must be checked before running.")],
         "maintenance": [("issueTypes", "Issue types", "Choices on a maintenance ticket."),
                         ("equipment", "Other equipment", "Listed after the machines when someone opens a ticket.")]}
FEED_LABEL = [("db", "Database table"), ("http", "Read from URL"), ("push", "Pushed to server"), ("influx", "InfluxDB query"),
              ("demo", "Demo replay"), ("none", "Not connected")]
TABLE_DBS = [("machines", "Machine data database"), ("assets", "Asset database")]
S2K_ROLES = [("mo", "MO number", True), ("item", "Item / part number", True), ("qty", "Quantity open", True), ("due", "Due date", False),
             ("process", "Process code", False), ("mfg", "MFG location", False), ("hot", "Hot flag", False)]
# Commands a field or button can run as they are (the engine checks the person and every value)
PASSTHROUGH = {"listAdd", "listDel", "listMove", "listSet", "lineAdd", "lineSet", "lineDel", "shiftAdd", "shiftSet", "shiftDel", "breakAdd",
               "breakSet", "breakDel", "partAdd", "partSet", "partDel", "ruleSet", "settingSet", "configReset", "dataReset", "testAll",
               "s2kPull", "peoplePull", "paycomImport", "networkFirewallSync", "srcDelete", "alertContactDelete", "updCancel", "updGoBack",
               "updFolder", "updWhen", "permRole", "permPerson", "permReset", "qcItemAdd", "qcItemDel", "qcItemMove"}
_tables_cache = {}


def q(name):
    return "`" + str(name).replace("`", "``") + "`"


def run(request, s, name, p):
    return engine.execute(name, p, engine.Ctx(user=s.actor, line=s.station, client_ip=request.META.get("REMOTE_ADDR", "")))


def tables(db):
    """Tables and columns of a read-only database, for the pickers (kept a minute; never fails the page)."""
    hit = _tables_cache.get(db)
    if hit and time.monotonic() - hit[0] < 60:
        return hit[1]
    try:
        integrations.liveconfig.refresh()
        if db not in integrations.connections.databases:
            out = {"err": "", "tables": [], "ready": False}
        else:
            out = {"err": "", "tables": integrations.schema(db)["tables"], "ready": True}
    except Exception as e:  # noqa: BLE001 - a database being down never stops Admin from opening
        out = {"err": str(e)[:200], "tables": [], "ready": True}
    _tables_cache[db] = (time.monotonic(), out)
    return out


def columns_of(t, table):
    return [c["name"] for c in next((x["columns"] for x in t["tables"] if x["name"] == table), [])]


# ------------------------------------------------------------------ tab data
def tab_data(s, tab, request):
    cfg = s.cfg
    if tab in LISTS:
        return {"lists": [{"k": k, "title": t, "help": h, "items": cfg.get(k, [])} for k, t, h in LISTS[tab]]}
    if tab == "machines":
        base = request.build_absolute_uri("/station/")
        rows = []
        for m in s.lines:
            rows.append({"m": m, "queued": MO.objects.filter(line=m, status="OPEN").count(), "trained": m.trained_people.count(),
                         "url": base + quote(m.name) + "/"})
        return {"rows": rows}
    if tab == "shifts":
        out = []
        from plant.util import hm
        from plant.models import Shift
        shifts = [{"name": x.name, "start": x.start, "end": x.end, "days": x.days,
                   "breaks": [{"name": b.label, "start": b.start, "mins": (hm(b.end) - hm(b.start)) % 1440} for b in x.breaks.all()]}
                  for x in Shift.objects.prefetch_related("breaks")]   # same order the shift commands number them in
        for i, sh in enumerate(shifts):
            ln = hm(sh["end"]) - hm(sh["start"])
            ln = ln if ln > 0 else ln + 1440
            paid = ln - sum(int(b.get("mins") or 0) for b in sh.get("breaks") or [])
            out.append({"i": i, "sh": sh, "len": ln, "paid": paid})
        return {"shifts": out}
    if tab == "parts":
        from plant.models import PartSpec
        P = {x.key: x.as_dict() for x in PartSpec.objects.all()}
        fields = [("pleats", "Pleats/unit", "1"), ("len", "Length", ".01"), ("wid", "Width", ".01"), ("ph", "Pleat ht", ".01"),
                  ("sb", "Side band", ".01"), ("tol", "Tol ±", ".01")]
        return {"parts": [{"k": k, "p": P[k], "vals": [(f, lbl, st, P[k].get(f)) for f, lbl, st in fields]} for k in sorted(P)],
                "fields": fields}
    if tab == "rules":
        r = s.rules
        nums = [("stopPpm", "Line counts as stopped at or below (P/m)", "Speed readings at or below this count as downtime, unless the machine has a run signal (Admin › Connections › Machine data).", 0),
                ("minDown", "Log a stop as downtime after (min)", "Shorter stops still count against uptime but need no reason.", 1),
                ("reasonAfter", "Ask for a reason once unexplained downtime adds up to (min)", "Logged stops without a reason are collected; when they reach this many minutes the operator must pick a reason. 0 asks at every stop.", 0),
                ("startGrace", "Start-up time at the start of each shift (min)", "Stops in this many minutes after the shift's scheduled start are not downtime. Breaks and lunches from the Scheduler's shifts never count as downtime.", 0),
                ("coMins", "Changeover allowance (min)", "Time reserved on the schedule between different parts.", 0),
                ("adminTimeout", "Log out shift lead after idle (min)", "0 keeps admin mode on until someone logs out.", 0),
                ("photoDays", "Keep pictures on closed maintenance tickets (days)", "Pictures are kept in the records database. 0 keeps them for good.", 0),
                ("qcLate", "QC check late after (min)", "A QC check saved this many minutes after it was due needs a reason (the first check after a changeover is due as soon as the machine runs).", 0)]
        return {"nums": [(k, label, help_, mn, r.get(k)) for k, label, help_, mn in nums]}
    if tab == "backup":
        from .snapshot import build
        snap = build(s.me)
        exp = {"app": "pleat-line-hmi", "exported": s.t.isoformat(), "config": snap["config"], "lines": snap["lines"],
               "users": [{k: v for k, v in u.items() if k != "pin"} for u in snap["users"]]}
        return {"export": json.dumps(exp, indent=1, default=str)}
    if tab == "network":
        from .snapshot import network_info
        return {"N": network_info(s.st), "ip": request.META.get("REMOTE_ADDR", ""), "port": request.get_port()}
    if tab == "updates":
        try:
            from plant import updates
            return {"U": updates.info(), "GH": github_data(s)}
        except Exception as e:  # noqa: BLE001
            return {"U": None, "err": str(e)}
    if tab == "alerts":
        r = run(request, s, "alertInfo", {}) if s.can("alerts.edit") else {"ok": False}
        return {"I": r.get("data") if r.get("ok") else None}
    if tab == "connections":
        return connections_data(s, request)
    if tab == "connector":
        return connector_data(s, request.GET.get("db", ""), request.GET.get("table", ""), focus=request.GET.get("focus", ""))
    if tab == "permissions":
        return permissions_data(s, request)
    if tab == "qc":
        from plant.models import STAGES

        from . import qc
        return {"stages": [{"k": k, "label": lbl, "items": qc.items_for(k, cfg),
                            "free": [(b, d["label"]) for b, d in qc.BUILTIN.items() if b not in {i["key"] for i in qc.items_for(k, cfg)}]}
                           for k, lbl in STAGES], "types": qc.TYPES}
    if tab == "codata":
        return codata_data(s, request)
    return {}


def permissions_data(s, request):
    from plant import perms
    from .kviews import page_option
    cfg = perms._cfg()
    people = list(Person.objects.filter(active=True).order_by("name"))
    overrides = {str(k): v for k, v in (cfg.get("people") or {}).items() if v}
    groups = []
    for key, d in perms.PERMS.items():
        row = {"key": key, "label": d["label"], "help": d["help"],
               "roles": [{"role": r, "on": perms.role_has(key, r, cfg), "locked": (key, r) in perms.LOCKED,
                          "default": r in d["roles"]} for r, _ in perms.ROLES]}
        if not groups or groups[-1]["name"] != d["group"]:
            groups.append({"name": d["group"], "rows": []})
        groups[-1]["rows"].append(row)
    who = page_option(request, "who", {str(p.pk) for p in people} | {""}, "")
    person = next((p for p in people if str(p.pk) == who), None)
    detail = None
    if person:
        detail = [{"name": g["name"], "rows": [{**r, "rule": perms.person_rule(r["key"], person.pk, cfg),
                                                 "role": perms.role_has(r["key"], person.role, cfg), "eff": perms.can(person, r["key"], cfg),
                                                 "locked": (r["key"], person.role) in perms.LOCKED} for r in g["rows"]]} for g in groups]
    return {"groups": groups, "roles": perms.ROLES, "people": people, "person": person, "detail": detail,
            "withRules": [{"p": p, "n": len(overrides.get(str(p.pk), {}))} for p in people if overrides.get(str(p.pk))],
            "roleLabel": dict(perms.ROLES)}


def connections_data(s, request):
    from plant import liveconfig

    from .snapshot import server_info
    srv = server_info()
    st = s.settings
    g = st.get("grafana") or {}
    feed_label = dict(FEED_LABEL)
    rows = [("store", "Records database", "MariaDB (DB_* in .env)" if srv["store"] == "mariadb" else "The server's database file")]
    for m in s.lines:
        if m.feed_type == "http":
            addr = m.feed_url
        elif m.feed_type == "db":
            addr = dict(TABLE_DBS).get((m.feed_src or {}).get("db") or "machines", "") + (f" · {(m.feed_src or {}).get('table')}" if (m.feed_src or {}).get("table") else "")
        else:
            addr = feed_label.get(m.feed_type, "")
        rows.append(("feed:" + m.name, "Machine data · " + m.name, addr))
    if srv["machinesdb"]:
        rows.append(("machines", "Machine data database", "Set up in .env"))
    if srv["influx"]["url"]:
        rows.append(("influx", "InfluxDB", srv["influx"]["url"]))
    if srv["influx"]["out"]:
        rows.append(("influxOut", "Suite records copied to InfluxDB", "bucket " + srv["influx"]["out"]))
    pc = st.get("paycom") or {}
    rows += [("grafana", "Grafana", g.get("url") if g.get("on") and g.get("url") else ""), ("assets", "Asset database (MariaDB)", "Set up in .env" if srv["assets"] else ""),
             ("paycom", "Paycom", (f"automatic every {pc.get('every')} min" if pc.get("auto") else "manual") if srv["paycom"] else ""),
             ("s2k", "S2K schedule", ("S2K database (read-only)" if srv["s2kdb"] else "") if st.get("s2kSource") == "db" else st.get("s2kUrl", "")),
             ("people", "People & training list", st.get("peopleUrl", "")), ("webhook", "Copy records to another system", st.get("webhook", ""))]
    conn = s.st.conn or {}
    status = [{"k": k, "label": lbl, "addr": addr, "c": conn.get(k)} for k, lbl, addr in rows]
    out = {"status": status, "srv": srv, "g": g, "kiosk": st.get("kiosk") or {}, "pc": pc, "st": st,
           "panelsText": "\n".join(f"{x.get('title')} = {x.get('panel')}" for x in (st.get("kiosk") or {}).get("panels", [])),
           "queries": grafana_queries(s, srv)}
    if s.can("connections.edit"):
        info = liveconfig.info()
        out["I"] = info
        out["dbBoxes"] = [db_box(info, "s2k", "S2K database (read-only)", "Open MOs for the Scheduler. After saving, set up the query under S2K schedule below."),
                          db_box(info, "machines", "Machine data database (read-only)", "The database the machines (PLCs, data loggers) write their readings to. Pick each machine's table under Machine data below."),
                          db_box(info, "assets", "Asset database (read-only)", "Changeover steps, machine settings and drawing specs. Queries are set up on the Changeover data tab.")]
        out["influxBox"] = influx_box(info)
        out["sources"] = sources_data(s, request)
        out["s2k"] = s2k_data(s, request)
    out["machineCards"] = [machine_card(s, m) for m in s.lines]
    return out


def db_box(info, g, title, help_, form=None, msg=None):
    d = info[g]
    f = form or {"host": d["host"], "port": d["port"] or "3306", "name": d["name"], "user": d["user"], "internet": d.get("internet"),
                 "kind": d.get("kind") or "auto"}
    return {"g": g, "title": title, "help": help_, "d": d, "f": f, "msg": msg}


def influx_box(info, form=None, msg=None):
    d = info["influx"]
    f = form or {"url": d["url"], "version": d["version"], "org": d["org"], "user": d["user"], "db": d["db"], "out": d["out"]}
    return {"d": d, "f": f, "v2": str(f.get("version")) != "1", "msg": msg}


def grafana_queries(s, srv):
    out = [("Uptime % (leaves out breaks, lunch and start-up, like the suite)", "SELECT $__timeGroupAlias(t, '1h'),\n  100 * SUM(run) / NULLIF(SUM(run OR planned = 0), 0) AS \"Uptime %\"\nFROM hmi_ppm\nWHERE line = '$line' AND $__timeFilter(t)\nGROUP BY 1 ORDER BY 1"),
           ("Downtime minutes (planned time is never in this table)", "SELECT $__timeGroupAlias(start_time, '1d'), SUM(minutes) AS \"Down min\"\nFROM hmi_downtime\nWHERE line = '$line' AND $__timeFilter(start_time)\nGROUP BY 1 ORDER BY 1"),
           ("Downtime by reason", "SELECT COALESCE(reason, 'No reason given') AS reason, SUM(minutes) AS \"Minutes\"\nFROM hmi_downtime\nWHERE line = '$line' AND $__timeFilter(start_time)\nGROUP BY 1 ORDER BY 2 DESC"),
           ("Speed without planned stops", "SELECT t AS time, ppm AS \"P/m\"\nFROM hmi_ppm\nWHERE line = '$line' AND planned = 0 AND $__timeFilter(t)\nORDER BY t"),
           ("Filters made", "SELECT $__timeGroupAlias(t, '1h'), SUM(units) AS \"Filters\"\nFROM hmi_ppm\nWHERE line = '$line' AND $__timeFilter(t)\nGROUP BY 1 ORDER BY 1"),
           ("Planned breaks as shaded regions (dashboard annotation)", "SELECT start AS time, end AS timeend, kind AS text\nFROM hmi_planned\nWHERE line = '$line' AND $__timeFilter(start)"),
           ("Machine errors and alarms (table panel)", "SELECT start AS time, code AS \"Code\", text AS \"Message\", TIMESTAMPDIFF(MINUTE, start, COALESCE(end, NOW())) AS \"Min\"\nFROM hmi_machine_errors\nWHERE line = '$line' AND $__timeFilter(start)\nORDER BY start DESC"),
           ("Machine vision QC pass %", "SELECT $__timeGroupAlias(t, '1h'), 100 * AVG(passed) AS \"Pass %\"\nFROM hmi_qc_auto\nWHERE line = '$line' AND $__timeFilter(t)\nGROUP BY 1 ORDER BY 1")]
    b = srv["influx"]["out"]
    if b:
        if srv["influx"]["version"] != "1":
            out += [("InfluxDB · Uptime % (same rule as the suite)", f'from(bucket: "{b}")\n  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n  |> filter(fn: (r) => r._measurement == "suite_minute" and r.line == "${{line}}")\n  |> filter(fn: (r) => r._field == "run" or r._field == "counted")\n  |> group(columns: ["_field"])\n  |> aggregateWindow(every: 1h, fn: sum, createEmpty: false)\n  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n  |> map(fn: (r) => ({{_time: r._time, "Uptime %": if r.counted > 0 then 100.0 * float(v: r.run) / float(v: r.counted) else 0.0}}))'),
                    ("InfluxDB · Downtime minutes (planned time left out)", f'from(bucket: "{b}")\n  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n  |> filter(fn: (r) => r._measurement == "suite_minute" and r.line == "${{line}}" and r._field == "down")\n  |> group()\n  |> aggregateWindow(every: 1h, fn: sum, createEmpty: false)'),
                    ("InfluxDB · Downtime by reason", f'from(bucket: "{b}")\n  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n  |> filter(fn: (r) => r._measurement == "suite_downtime" and r.line == "${{line}}")\n  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n  |> group(columns: ["reason"])\n  |> sum(column: "minutes")'),
                    ("InfluxDB · Speed without planned stops", f'from(bucket: "{b}")\n  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n  |> filter(fn: (r) => r._measurement == "suite_minute" and r.line == "${{line}}")\n  |> filter(fn: (r) => r._field == "ppm" or r._field == "planned")\n  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n  |> filter(fn: (r) => r.planned == 0)\n  |> keep(columns: ["_time", "ppm"])')]
        else:
            out += [("InfluxDB · Uptime % (same rule as the suite)", 'SELECT 100 * sum("run") / sum("counted") AS "Uptime %" FROM "suite_minute"\nWHERE "line" = \'$line\' AND $timeFilter GROUP BY time(1h) fill(none)'),
                    ("InfluxDB · Downtime minutes (planned time left out)", 'SELECT sum("down") AS "Down min" FROM "suite_minute"\nWHERE "line" = \'$line\' AND $timeFilter GROUP BY time(1h) fill(none)'),
                    ("InfluxDB · Speed without planned stops", 'SELECT "ppm" FROM "suite_minute"\nWHERE "line" = \'$line\' AND "planned" = 0 AND $timeFilter')]
    return [{"i": i, "t": t, "sql": sql} for i, (t, sql) in enumerate(out)]


def machine_card(s, m, test=None):
    """Admin > Connections > Machine data: one machine's sources, with table and column choices from the database."""
    src, up, es = {"maxAge": 3, **(m.feed_src or {})}, {"maxAge": 0, **(m.uptime_src or {})}, m.error_src or {}
    feed_db = src.get("db") or "machines"
    T = tables(feed_db) if m.feed_type == "db" else None
    cols = columns_of(T, src.get("table")) if T else []
    UT = tables(up.get("db") or "machines") if up.get("type") == "db" else None
    ET = tables(es.get("db") or "machines") if es.get("type") == "db" else None
    from .models import LineState
    ls = LineState.objects.filter(machine=m).first()
    live = (ls.feed if ls else {}) or {}
    conn = (s.st.conn or {}).get("feed:" + m.name)
    return {"m": m, "src": src, "up": up, "es": es, "db": m.feed_type == "db", "T": T, "cols": cols,
            "tables": [t["name"] for t in T["tables"]] if T else [], "UT": UT, "ucols": columns_of(UT, up.get("table")) if UT else [],
            "utables": [t["name"] for t in UT["tables"]] if UT else [], "ET": ET, "ecols": columns_of(ET, es.get("table")) if ET else [],
            "etables": [t["name"] for t in ET["tables"]] if ET else [], "live": live, "conn": conn, "test": test or {},
            "feedLabels": FEED_LABEL, "tableDbs": TABLE_DBS, "stop": s.rules.get("stopPpm"),
            "influxOn": bool(integrations.settings.SUITE.get("INFLUX_URL"))}


def sources_data(s, request, edit=None, key=None, test=None, err=None):
    r = run(request, s, "srcList", {})
    return {"list": r.get("data") or [] if r.get("ok") else [], "edit": edit, "key": key, "test": test, "err": err,
            "origin": request.build_absolute_uri("/").rstrip("/")}


def s2k_data(s, request, sql=None, msg=None, err=False, res=None, show=False, bld=None):
    from .models import SavedQuery
    saved = SavedQuery.objects.filter(name="s2k_mos").first()
    out = {"saved": saved, "msg": msg, "err": err, "res": res, "show": show, "bld": bld or {}, "roles": S2K_ROLES,
           "sql": sql if sql is not None else (saved.sql if saved else "SELECT mo_number AS mo, item_number AS item, qty_open AS qty, due_date AS due, process_code AS process\nFROM open_orders\nWHERE status = 'OPEN'")}
    if show or bld:
        T = tables("s2k")
        out["T"] = T
        out["cols"] = columns_of(T, (bld or {}).get("table"))
    return out


def s2k_build_sql(b, kind=None):
    from plant import dbkinds
    kind = kind or integrations.kind_of("s2k")
    q = lambda n: dbkinds.quote(kind, n)   # noqa: E731 - quoting for that database's dialect
    qt = lambda n: dbkinds.table(kind, n)  # noqa: E731
    if not b.get("table"):
        raise ValueError("Pick a table.")
    miss = [lbl for k, lbl, req in S2K_ROLES if req and not b.get(k)]
    if miss:
        raise ValueError("Pick a column for: " + ", ".join(miss))
    sel = ",\n       ".join(f"{q(b[k])} AS {k}" for k, _, _ in S2K_ROLES if b.get(k))
    where = ""
    if b.get("fcol"):
        v = str(b.get("fval") or "").strip()
        if not v:
            raise ValueError('Type the value to keep, or clear the "only rows where" column.')
        where = f"\nWHERE {q(b['fcol'])} = '" + v.replace("\\", "\\\\" if kind == "mysql" else "\\").replace("'", "''") + "'"
    return f"SELECT {sel}\nFROM {qt(b['table'])}{where}"


# ------------------------------------------------------------------ changeover data (asset database queries)
GUESS = {"step": ["step", "instruction", "step_text", "description", "text"], "seq": ["seq", "step_no", "step_number", "sort", "sort_order", "sequence"],
         "detail": ["detail", "details", "note", "notes"], "setting": ["setting", "setting_name", "parameter"], "value": ["value", "setting_value", "target"],
         "kind": ["kind", "tool_type", "tooling_type", "category", "type"], "screws": ["screws", "screw", "screw_set"], "helix": ["helix", "helix_set"],
         "sideband": ["sideband", "sidebands", "side_band", "side_band_material"],
         "unit": ["unit", "units", "uom"], "asset": ["asset", "asset_id", "tool", "tool_id"], "image": ["image", "image_url", "photo", "photo_url"],
         "name": ["name", "setting", "setting_name", "parameter"], "rev": ["rev", "revision", "drawing_rev"], "pleats": ["pleats", "pleat_count", "pleats_per_unit"],
         "len": ["length", "len", "length_in"], "wid": ["width", "wid", "width_in"], "ph": ["pleat_height", "ph"], "sb": ["side_band", "side_band_height", "sb"],
         "tol": ["tol", "tolerance"], "media": ["media", "media_type", "material"], "f_part": ["part", "part_no", "part_number", "item", "sku"],
         "f_machine": ["machine", "machine_name", "line", "asset_line"], "f_from": ["from_part", "from_part_no", "previous_part"]}


def guess_columns(qname, cols):
    from .snapshot import QDEF
    roles = [r[0] for r in QDEF[qname]["roles"]]
    low = [c.lower() for c in cols]
    out = {}
    for k, names in GUESS.items():
        if k == "f_from" and qname != "changeover_steps":
            continue
        if k == "f_machine" and qname == "part_spec":
            continue
        if not k.startswith("f_") and k not in roles:
            continue
        hit = next((n for n in names if n in low), None)
        if hit:
            out[k] = cols[low.index(hit)]
    return out


def build_codata_sql(qname, b, kind=None):
    from plant import dbkinds
    kind = kind or integrations.kind_of("assets")
    q = lambda n: dbkinds.quote(kind, n)   # noqa: E731 - quoting for that database's dialect
    qt = lambda n: dbkinds.table(kind, n)  # noqa: E731
    from .snapshot import QDEF
    D = QDEF[qname]
    if not b.get("table"):
        raise ValueError("Pick a table.")
    miss = [r[1] for r in D["roles"] if len(r) > 2 and r[2] and not b.get(r[0])]
    if miss:
        raise ValueError("Pick a column for: " + ", ".join(miss))
    if not b.get("f_part"):
        raise ValueError("Pick the column that holds the part number.")
    if not any(b.get(r[0]) for r in D["roles"]):
        raise ValueError("Pick at least one column to show.")
    sel = ",\n       ".join(f"{q(b[r[0]])} AS {r[0]}" for r in D["roles"] if b.get(r[0]))
    w = [f"{q(b['f_part'])} = :part"]
    if b.get("f_machine"):
        w.append(f"({q(b['f_machine'])} = :machine OR {q(b['f_machine'])} IS NULL)")
    if b.get("f_from"):
        w.append(f"({q(b['f_from'])} = :from_part OR {q(b['f_from'])} IS NULL)")
    order = f"\nORDER BY {q(b['seq'])}" if D.get("order") and b.get("seq") else ""
    return f"SELECT {sel}\nFROM {qt(b['table'])}\nWHERE " + "\n  AND ".join(w) + order


def norm_steps(rows):
    out = []
    for i, r in enumerate(rows or []):
        o = {str(k).lower(): v for k, v in r.items()}
        out.append((o, i))
    out.sort(key=lambda x: ((float(x[0]["seq"]) if x[0].get("seq") not in (None, "") and str(x[0]["seq"]).replace(".", "", 1).isdigit() else 0), x[1]))
    steps = []
    for o, _ in out:
        text = str(o.get("step") or o.get("instruction") or o.get("text") or "").strip()
        if text:
            steps.append({"text": text, "detail": o.get("detail") or "", "setting": o.get("setting") or "", "value": o.get("value") if o.get("value") is not None else "",
                          "unit": o.get("unit") or "", "asset": o.get("asset") or "", "image": o.get("image") or ""})
    return steps


def norm_settings(rows):
    out = []
    for r in rows or []:
        o = {str(k).lower(): v for k, v in r.items()}
        name = str(o.get("name") or o.get("setting") or "").strip()
        if name:
            out.append({"name": name, "value": o.get("value") or "", "unit": o.get("unit") or "", "asset": o.get("asset") or ""})
    return out


def codata_data(s, request, states=None):
    from .models import SavedQuery
    from .snapshot import QDEF
    srv_assets = integrations.assets_ready()
    out = {"assets": srv_assets, "useDb": s.settings.get("useDb")}
    if not srv_assets or not s.can("connections.edit"):
        return out
    T = tables("assets")
    out["T"] = T
    saved = {x.name: x for x in SavedQuery.objects.filter(name__in=list(QDEF))}
    cur = s.cur_mo
    nm = s.co_mo("newMo")
    om = s.co_mo("oldMo")
    from plant.models import part_key
    tp_default = {"part": part_key((nm or cur).part) if (nm or cur) else "MG4062HP", "machine": s.station or (s.lines[0].name if s.lines else ""),
                  "from_part": part_key(om.part) if om else ""}
    states = states or {}
    qs = []
    for name, D in QDEF.items():
        st = states.get(name) or {}
        b = st.get("bld") or {}
        sv = saved.get(name)
        q_ = {"name": name, "D": D, "saved": sv, "sql": st.get("sql") if st.get("sql") is not None else (sv.sql if sv else ""),
              "tp": st.get("tp") or (sv.params if sv and isinstance(getattr(sv, "params", None), dict) and sv.params else tp_default),
              "bld": b, "cols": columns_of(T, b.get("table")), "res": st.get("res"), "msg": st.get("msg"),
              "required": ", ".join(r[0] for r in D["roles"] if len(r) > 2 and r[2])}
        res = st.get("res")
        if res and not res.get("error"):
            if name == "part_tooling":
                from .runinfo import norm_tooling
                q_["tooling"] = [g for g in norm_tooling(res["rows"]) if g["items"]]
            q_["preview"] = norm_steps(res["rows"]) if name == "changeover_steps" else norm_settings(res["rows"]) if name == "asset_settings" else res["rows"]
        qs.append(q_)
    out["queries"] = qs
    out["browse"] = (states or {}).get("_browse")
    out["hold"] = bool(states)
    return out


# ------------------------------------------------------------------ actions
def render_panel(request, template, **ctx):
    resp = HttpResponse(render_to_string(template, ctx, request=request))
    return resp


def act_run(request, s, p):
    """A field or button that runs one engine command as it is."""
    name = p.pop("c", "")
    if name not in PASSTHROUGH:
        return thin.refused("Unknown action.")
    if "v:j" in request.POST:
        pass
    r = run(request, s, name, p)
    if not r["ok"]:
        if r.get("admin") or r.get("login"):
            from .kviews import login_needed
            return login_needed(request, s, r)
        return thin.refused(r["error"])
    return thin.done(toast=r.get("msg"))


ACTIONS = {}


def action(name):
    def deco(fn):
        ACTIONS[name] = fn
        return fn
    return deco


ACTIONS["run"] = act_run


def _need(request, s, perm):
    """None if the person on this screen has permission `perm`; otherwise the login dialog for someone who has."""
    if s.can(perm):
        return None
    from .kviews import login_needed
    return login_needed(request, s, {"perm": perm})


def connector_data(s, db="", table="", msg=None, focus=""):
    from . import connector
    dbs = connector.ready_dbs()
    gs = connector.groups()
    if not db or db not in dbs:   # start where the first unfinished need's data lives
        first = next((g for g in gs if g["status"] in ("missing", "partial") and any(d in dbs for d in g["dbs"])), None)
        db = (first["table"] and first["db"]) if first and first["table"] else next((d for d in (first or {}).get("dbs", []) if d in dbs), dbs[0] if dbs else "")
        if first and not table:
            table = first["table"]
    return {"dbs": [(d, connector.DB_LABEL[d]) for d in dbs], "allDbs": connector.DB_LABEL, "src": connector.source(db, table) if db else None,
            "groups": gs, "sum": connector.summary(gs), "msg": msg, "focus": focus}


@action("connectorSave")
def a_connector_save(request, s, p):
    import json as _json
    deny = _need(request, s, "connections.edit")
    if deny:
        return deny
    try:
        mp = _json.loads(p.get("map") or "{}")
    except ValueError:
        mp = {}
    r = run(request, s, "connectorSave", {"group": p.get("group", ""), "db": p.get("db", ""), "table": p.get("table", ""), "map": mp,
                                           "keyVal": p.get("keyVal", ""), "fval": p.get("fval", "")})
    msg = {"ok": r["ok"], "text": r.get("msg") if r["ok"] else r["error"], "group": p.get("group", "")}
    if r["ok"]:
        _tables_cache.clear()
    return render_panel(request, "hmi/k/admin/_connector.html", S=s,
                        C=connector_data(s, p.get("srcDb", ""), p.get("srcTable", ""), msg=msg, focus=p.get("group", "")), tabCan=True)


def accounts_panel(request, s, g=None, form=None, msg=None, key=None):
    from plant import liveconfig
    info = liveconfig.info()
    boxes = []
    for k, t, h in (("s2k", "S2K database (read-only)", "Open MOs for the Scheduler. After saving, set up the query under S2K schedule below."),
                    ("machines", "Machine data database (read-only)", "The database the machines (PLCs, data loggers) write their readings to. Pick each machine's table under Machine data below."),
                    ("assets", "Asset database (read-only)", "Changeover steps, machine settings and drawing specs. Queries are set up on the Changeover data tab.")):
        boxes.append(db_box(info, k, t, h, form if g == k else None, msg if g == k else None))
    ctx = {"S": s, "I": info, "dbBoxes": boxes, "influxBox": influx_box(info, form if g == "influx" else None, msg if g == "influx" else None),
           "msgs": {g: msg} if g and msg else {}, "hold": bool(msg or key or form), "pcForm": form if g == "paycom" else None, "newKey": key}
    return render_panel(request, "hmi/k/admin/_accounts.html", **ctx)


def _conn_fields(p, g):
    if g == "influx":
        return {k: p.get(k, "") for k in ("url", "version", "org", "token", "user", "db", "out")}
    if g == "paycom":
        return {k: p.get(k, "") for k in ("sid", "token")}
    if g == "grafana":
        return {"token": p.get("token", "")}
    return {k: p.get(k, "") for k in ("host", "port", "name", "user", "password", "kind")} | {"internet": bool(p.get("internet"))}


@action("connTest")
def a_conn_test(request, s, p):
    deny = _need(request, s, "connections.edit")
    if deny:
        return deny
    g = p.get("group", "")
    f = _conn_fields(p, g)
    r = run(request, s, "connTest", {"group": g, "fields": f})
    if r["ok"]:
        d = r["data"]
        msg = {"ok": d.get("ok"), "text": d.get("msg", "") + (" Note: this login is allowed to change data; a read-only login is safer." if d.get("writable") else "")}
    else:
        msg = {"ok": False, "text": r["error"]}
    f.pop("password", None), f.pop("token", None)
    return accounts_panel(request, s, g, f, msg)


@action("connSave")
def a_conn_save(request, s, p):
    deny = _need(request, s, "connections.edit")
    if deny:
        return deny
    g = p.get("group", "")
    f = _conn_fields(p, g)
    if g in ("machines", "assets"):
        _tables_cache.pop(g, None)
    r = run(request, s, "connSave", {"group": g, "fields": f if g != "feed" else {}})
    f.pop("password", None), f.pop("token", None)
    if not r["ok"]:
        return accounts_panel(request, s, g, f, {"ok": False, "text": "Not saved. " + r["error"]})
    return thin.trigger(accounts_panel(request, s, g, None, {"ok": True, "text": r.get("msg", "Saved")}, key=r.get("key") if g == "feed" else None),
                        changed=True)


@action("connRedraw")
def a_conn_redraw(request, s, p):
    """A choice that changes which fields a login form has (InfluxDB version): the same form again, nothing saved."""
    deny = _need(request, s, "connections.edit")
    if deny:
        return deny
    g = p.get("group", "")
    f = _conn_fields(p, g)
    f.pop("password", None), f.pop("token", None)
    return accounts_panel(request, s, g, f)


@action("mdSet")
def a_md_set(request, s, p):
    """Admin > Machine data: one field of a machine's sources. The card comes back, since a choice can change its fields."""
    m = Machine.objects.filter(name=p.get("id", ""), stage="s1").first()
    if not m:
        return thin.refused("Machine not found.")
    v = p.get("v:b") if "v:b" in p else p.get("v", "")
    r = run(request, s, "lineSet", {"id": m.name, "f": p.get("f", ""), "v": v})
    if not r["ok"]:
        if r.get("admin") or r.get("login"):
            from .kviews import login_needed
            return login_needed(request, s, r)
        return thin.refused(r["error"])
    m.refresh_from_db()
    return thin.trigger(render_panel(request, "hmi/k/admin/_machine_card.html", S=s, c=machine_card(s, m)), changed=True)


@action("kioskPanels")
def a_kiosk_panels(request, s, p):
    """Data Kiosk panels typed as "Title = panel ID", one per line."""
    panels, bad = [], []
    for n, line in enumerate(str(p.get("text") or "").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        title, sep, pid = line.rpartition("=")
        if not sep or not title.strip() or not pid.strip().isdigit():
            bad.append(str(n))
            continue
        panels.append({"title": title.strip()[:60], "panel": int(pid.strip())})
    if bad:
        return thin.refused(f"Line {', '.join(bad)} isn't \"Title = panel ID\" (for example: Speed = 2). Nothing saved.")
    r = run(request, s, "settingSet", {"k": "kiosk.panels", "v": panels})
    if not r["ok"]:
        return thin.refused(r["error"])
    return thin.done(toast=f"Saved {len(panels)} panel{'s' if len(panels) != 1 else ''}.")


@action("connRemove")
def a_conn_remove(request, s, p):
    deny = _need(request, s, "connections.edit")
    if deny:
        return deny
    g = p.get("group", "")
    _tables_cache.pop(g, None)
    r = run(request, s, "connRemove", {"group": g})
    return thin.trigger(accounts_panel(request, s, g, None, {"ok": r["ok"], "text": r.get("msg") if r["ok"] else r["error"]}), changed=True)


@action("testConn")
def a_test_conn(request, s, p):
    r = run(request, s, "testConn", {"k": p.get("k", "")})
    if not r["ok"]:
        return thin.refused(r["error"])
    d = r.get("data") or {}
    return thin.done(toast=("OK · " if d.get("ok") else "Failed · ") + str(d.get("msg", "")), kind=None if d.get("ok") else "bad")


@action("mdTest")
def a_md_test(request, s, p):
    """Admin > Machine data > Test: the newest row of a machine's table, run signal or alarm log."""
    m = Machine.objects.filter(name=p.get("id", ""), stage="s1").first()
    if not m:
        return thin.refused("Machine not found.")
    w = p.get("which") or "feed"
    r = run(request, s, "lineFeedTest", {"id": m.name, "which": w})
    if not r["ok"]:
        test = {w: {"ok": False, "text": r["error"]}}
    else:
        d = r["data"]
        if w == "err":
            text = ("Newest alarm rows:\n" + "\n".join(f"{x.get('at') or 'row ' + str(x.get('row'))}  {x.get('code')}{' · ' if x.get('code') and x.get('text') else ''}{x.get('text')}"
                                                       for x in d.get("rows", []))) if d.get("rows") else "The alarm log table is empty."
        elif w == "uptime":
            text = f"Newest row {d.get('newest')}: {'running' if d.get('run') else 'stopped'}."
        else:
            text = (f"Newest row {d.get('newest')}: {d.get('ppm')} P/m" + (f" · filters cut counter {d['count']}" if d.get("count") is not None else "")
                    + (f" · recipe {d['recipe']}" if d.get("recipe") else "") + (f" · {'running' if d['run'] else 'stopped'}" if isinstance(d.get("run"), bool) else "")
                    + (f" · {d['runErr']}" if d.get("runErr") else "") + ".")
        test = {w: {"ok": True, "text": text}}
    return render_panel(request, "hmi/k/admin/_machine_card.html", S=s, c=machine_card(s, m, test))


# ---------------------------------------------------------------- alerts
def alerts_panel(request, s, edit=None, msgs=None, forms=None, err=None):
    r = run(request, s, "alertInfo", {})
    return render_panel(request, "hmi/k/admin/_alerts_panel.html", S=s, I=r.get("data") if r.get("ok") else None, edit=edit, msgs=msgs or {},
                        forms=forms or {}, err=err, lines=[m.name for m in s.lines])


@action("alertEdit")
def a_alert_edit(request, s, p):
    deny = _need(request, s, "alerts.edit")
    if deny:
        return deny
    edit = {"name": "", "email": "", "phone": "", "lines": [], "downAfter": 15, "notifyBack": True, "active": True}
    if p.get("id"):
        r = run(request, s, "alertInfo", {})
        edit = next((c for c in (r.get("data") or {}).get("contacts", []) if str(c["id"]) == str(p["id"])), edit)
    return alerts_panel(request, s, edit=edit)


@action("alertCancel")
def a_alert_cancel(request, s, p):
    return alerts_panel(request, s)


@action("alertSave")
def a_alert_save(request, s, p):
    c = {"id": int(p["id"]) if str(p.get("id") or "").isdigit() else None, "name": p.get("name", ""), "email": p.get("email", ""),
         "phone": p.get("phone", ""), "lines": request.POST.getlist("lines"), "downAfter": p.get("downAfter") or 15,
         "notifyBack": bool(p.get("notifyBack")), "active": bool(p.get("active"))}
    r = run(request, s, "alertContactSave", {"contact": c})
    if not r["ok"]:
        return alerts_panel(request, s, edit=c, err=r["error"])
    return alerts_panel(request, s)


@action("alertTest")
def a_alert_test(request, s, p):
    r = run(request, s, "alertTest", {"id": int(p.get("id") or 0)})
    return alerts_panel(request, s, msgs={"people": {"ok": r["ok"], "text": r.get("msg") if r["ok"] else r["error"]}})


@action("alertDelete")
def a_alert_delete(request, s, p):
    r = run(request, s, "alertContactDelete", {"id": int(p.get("id") or 0)})
    return thin.trigger(alerts_panel(request, s, msgs={} if r["ok"] else {"people": {"ok": False, "text": r["error"]}}), close=True)


@action("alertConfig")
def a_alert_config(request, s, p):
    g = p.get("group", "")
    if g == "smtp":
        f = {k: p.get(k, "") for k in ("host", "port", "security", "user", "password", "from")} | {"internet": bool(p.get("internet"))}
    elif g == "sms":
        f = {k: p.get(k, "") for k in ("sid", "token", "from")}
    else:
        f = {}
    r = run(request, s, "alertConfigSave", {"group": g, "fields": f})
    key = "smtp" if g.startswith("smtp") else "sms"
    f.pop("password", None), f.pop("token", None)
    return thin.trigger(alerts_panel(request, s, msgs={key: {"ok": r["ok"], "text": r.get("msg") if r["ok"] else r["error"]}},
                                     forms={} if r["ok"] else {key: f}), close=True)


# ---------------------------------------------------------------- network
def network_panel(request, s, draft=None, err=None):
    from .snapshot import network_info
    N = network_info(s.st)
    saved = [a["range"] for a in N["allowed"]]
    draft = draft if draft is not None else saved
    desc = {a["range"]: a["desc"] for a in N["allowed"]}
    return render_panel(request, "hmi/k/admin/_network_form.html", S=s, N=N, rows=[{"i": i, "r": r, "desc": desc.get(r, "")} for i, r in enumerate(draft)],
                        changed=draft != saved, err=err, ip=request.META.get("REMOTE_ADDR", ""), port=request.get_port(),
                        notDefault=draft != [N["default"]])


@action("netEdit")
def a_net_edit(request, s, p):
    """Add, remove or reset ranges in the list being edited (nothing is saved until Save)."""
    rows = [str(x).strip() for x in request.POST.getlist("range")]
    op = p.get("op", "")
    if op == "add":
        v = (p.get("add") or "").strip()
        if v and v not in rows:
            rows.append(v)
    elif op.startswith("del:"):
        i = int(op[4:]) if op[4:].isdigit() else -1
        if 0 <= i < len(rows) and len(rows) > 1:
            rows.pop(i)
    elif op == "default":
        from .snapshot import network_info
        rows = [network_info(s.st)["default"]]
    elif op == "undo":
        rows = None
    return network_panel(request, s, rows)


@action("netSave")
def a_net_save(request, s, p):
    rows = [str(x).strip() for x in request.POST.getlist("range") if str(x).strip()]
    if not rows:
        return network_panel(request, s, rows, err="Keep at least one range.")
    r = run(request, s, "networkSet", {"allowed": rows})
    if not r["ok"]:
        return network_panel(request, s, rows, err=r["error"])
    return thin.trigger(network_panel(request, s), toast=r.get("msg"), changed=True)


# ---------------------------------------------------------------- incoming data sources
def sources_panel(request, s, **kw):
    return render_panel(request, "hmi/k/admin/_sources.html", S=s, X=sources_data(s, request, **kw), lines=[m.name for m in s.lines])


def _src_from_form(request, p):
    mapping = {k: p.get("map_" + k, "") for k in ("time", "line", "part", "mo", "len", "wid", "ph", "sbl", "sbr", "passed", "id")}
    mapping["units"] = p.get("map_units") or "in"
    mapping["fields"] = [x.strip() for x in (p.get("map_fields") or "").split(",") if x.strip()]
    return {"id": int(p["id"]) if str(p.get("id") or "").isdigit() else None, "name": p.get("name", ""), "kind": p.get("kind") or "qc",
            "line": p.get("line", ""), "mode": p.get("mode") or "push", "url": p.get("url", ""), "every": p.get("every") or 60,
            "enabled": bool(p.get("enabled")), "mapping": {k: v for k, v in mapping.items() if v not in ("", [])} | {"units": mapping["units"]},
            "fields": json.loads(p.get("seen") or "[]") if p.get("seen") else []}


@action("srcEdit")
def a_src_edit(request, s, p):
    deny = _need(request, s, "connections.edit")
    if deny:
        return deny
    if p.get("id"):
        lst = run(request, s, "srcList", {}).get("data") or []
        e = next((x for x in lst if str(x["id"]) == str(p["id"])), None)
    else:
        e = {"name": "", "kind": "qc", "line": "", "mode": "push", "url": "", "every": 60, "enabled": True, "mapping": {"units": "in"}, "fields": []}
    if p.get("redraw"):   # a choice that changes which fields the form has (kind, mode, line)
        e = _src_from_form(request, p)
    return sources_panel(request, s, edit=e)


@action("srcCancel")
def a_src_cancel(request, s, p):
    return sources_panel(request, s)


@action("srcSave")
def a_src_save(request, s, p):
    e = _src_from_form(request, p)
    r = run(request, s, "srcSave", {"src": e})
    if not r["ok"]:
        return sources_panel(request, s, edit=e, err=r["error"])
    key = {"name": r["data"]["name"], "key": r["key"]} if r.get("key") else None
    return thin.trigger(sources_panel(request, s, key=key), toast=r.get("msg"))


@action("srcKey")
def a_src_key(request, s, p):
    r = run(request, s, "srcNewKey", {"id": int(p.get("id") or 0)})
    if not r["ok"]:
        return thin.refused(r["error"])
    return thin.trigger(sources_panel(request, s, key={"name": r["data"]["name"], "key": r["key"]}), close=True)


@action("srcTest")
def a_src_test(request, s, p):
    r = run(request, s, "srcTest", {"id": int(p.get("id") or 0)})
    if not r["ok"]:
        return thin.refused(r["error"])
    d = r["data"]
    return sources_panel(request, s, test={"name": d["src"]["name"], **d})


@action("srcDelete")
def a_src_delete(request, s, p):
    r = run(request, s, "srcDelete", {"id": int(p.get("id") or 0)})
    return thin.trigger(sources_panel(request, s), toast=None if r["ok"] else r["error"], close=True)


# ---------------------------------------------------------------- S2K query
def s2k_panel(request, s, **kw):
    return render_panel(request, "hmi/k/admin/_s2k.html", S=s, K=s2k_data(s, request, **kw), st=s.settings, srv_s2kdb=integrations.s2k_db_ready())


def _s2k_bld(request, p):
    return {k: p.get("b_" + k, "") for k in ["table", "fcol", "fval"] + [r[0] for r in S2K_ROLES]}


@action("s2k")
def a_s2k(request, s, p):
    deny = _need(request, s, "connections.edit")
    if deny:
        return deny
    op, sql, bld = p.get("op", ""), p.get("sql", ""), _s2k_bld(request, p)
    if op == "source":   # where open MOs come from changes which fields the panel has
        r = run(request, s, "settingSet", {"k": "s2kSource", "v": p.get("v", "")})
        if not r["ok"]:
            return thin.refused(r["error"])
        from plant.models import PlantSettings
        s.st = PlantSettings.load()
        s.settings, s.cfg = s.st.settings, s.st.config
        return thin.trigger(s2k_panel(request, s), changed=True)
    if op == "tables":
        return s2k_panel(request, s, sql=sql, show=not p.get("shown"), bld=bld if p.get("shown") is None else bld)
    if op == "pick":   # a table or column was picked in the builder
        if p.get("changed") == "b_table":
            bld = {"table": bld["table"]}
        return s2k_panel(request, s, sql=sql, show=True, bld=bld)
    if op == "build":
        try:
            sql = s2k_build_sql(bld)
            return s2k_panel(request, s, sql=sql, show=True, bld=bld, msg="SQL written. Run test to check it.")
        except ValueError as e:
            return s2k_panel(request, s, sql=sql, show=True, bld=bld, msg=str(e), err=True)
    if op == "test":
        r = run(request, s, "dbTest", {"db": "s2k", "sql": sql})
        if r["ok"]:
            d = r["data"]
            return s2k_panel(request, s, sql=sql, bld=bld, show=bool(bld.get("table")), res={"columns": d["columns"], "rows": d["rows"][:20]},
                             msg=f"{len(d['rows'])} row{'s' if len(d['rows']) != 1 else ''} in {d['ms']} ms{' (first 500)' if d['truncated'] else ''}. Showing the first 20.")
        return s2k_panel(request, s, sql=sql, bld=bld, show=bool(bld.get("table")), msg=r["error"], err=True)
    if op == "save":
        r = run(request, s, "dbSave", {"name": "s2k_mos", "sql": sql})
        return s2k_panel(request, s, sql=sql, msg="Saved. Pulls now use this query." if r["ok"] else "Not saved: " + r["error"], err=not r["ok"])
    return s2k_panel(request, s, sql=sql)


# ---------------------------------------------------------------- changeover data queries (asset database)
def codata_panel(request, s, name=None, st=None, browse=None):
    states = {name: st} if name else {}
    if browse is not None:
        states["_browse"] = browse
    return render_panel(request, "hmi/k/admin/_codata.html", S=s, C=codata_data(s, request, states))


@action("codata")
def a_codata(request, s, p):
    from .snapshot import QDEF
    deny = _need(request, s, "connections.edit")
    if deny:
        return deny
    op = p.get("op", "")
    if op == "reload":
        _tables_cache.pop("assets", None)
        return codata_panel(request, s)
    if op == "browse":
        t = p.get("table", "")
        prev = None
        if t:
            r = run(request, s, "dbPreview", {"table": t})
            prev = r["data"] if r["ok"] else {"columns": ["error"], "rows": [{"error": r["error"]}]}
        return codata_panel(request, s, browse={"table": t, "cols": columns_of(tables("assets"), t), "prev": prev})
    name = p.get("q", "")
    if name not in QDEF:
        return thin.refused("Unknown query.")
    bld = {k: p.get("b_" + k, "") for k in ["table", "f_part", "f_machine", "f_from"] + [r[0] for r in QDEF[name]["roles"]]}
    tp = {k: (p.get("tp_" + k) or "").strip() for k in ("part", "machine", "from_part")}
    st = {"bld": bld, "tp": tp, "sql": p.get("sql", "")}
    if op == "pick":
        if p.get("changed") == "b_table":
            st["bld"] = {"table": bld["table"], **guess_columns(name, columns_of(tables("assets"), bld["table"]))}
        return codata_panel(request, s, name, st)
    if op == "build":
        try:
            st["sql"] = build_codata_sql(name, bld)
        except ValueError as e:
            st["msg"] = {"ok": False, "text": str(e)}
        return codata_panel(request, s, name, st)
    if op == "test":
        m = Machine.objects.filter(name=tp["machine"]).first()
        r = run(request, s, "dbTest", {"sql": st["sql"], "params": {**tp, "mo": "", "line_code": m.code if m else ""}})
        st["res"] = r["data"] if r["ok"] else {"error": r["error"]}
        return codata_panel(request, s, name, st)
    if op == "save":
        r = run(request, s, "dbSave", {"name": name, "sql": st["sql"], "params": tp})
        st["msg"] = {"ok": r["ok"], "text": "Saved on the server." if r["ok"] else "Not saved: " + r["error"]}
        return codata_panel(request, s, name, st)
    if op == "drop":
        run(request, s, "dbSave", {"name": name, "sql": None})
        return codata_panel(request, s, name, {"bld": bld, "tp": tp, "sql": ""})
    return codata_panel(request, s, name, st)


# ---------------------------------------------------------------- updates
@action("updUpload")
def a_upd_upload(request, s, p):
    from plant import updates
    deny = _need(request, s, "updates.install")
    if deny:
        return deny
    f = request.FILES.get("file")
    if not f:
        return thin.refused("Choose the update file first.")
    if f.size > 300 * 1024 * 1024:
        return thin.refused("That file is too large to be an update file.")
    when, at = p.get("when") or "now", p.get("at") or ""
    if when == "at" and not at:
        return thin.refused("Pick the date and time.")
    tmp = updates.root() / "run" / "upload.tmp"
    with open(tmp, "wb") as out:
        for chunk in f.chunks():
            out.write(chunk)
    try:
        m = updates.receive(tmp, f.name, s.actor.name, when, at)
    except updates.UpdError as e:
        return thin.refused(str(e))
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    engine.audit(engine.Ctx(user=s.actor), f"Update v{m['version']} rev {m['revision']} uploaded ({f.name})")
    return thin.done(toast=f"v{m['version']} rev {m['revision']} checked and ready.")


# ---------------------------------------------------------------- updates from GitHub
def github_data(s, msg=None, form=None):
    from plant import ghupdates, liveconfig
    return {"G": liveconfig.info()["github"], "last": ghupdates.last(), "msg": msg, "form": form}


def github_panel(request, s, msg=None, form=None, hold=False):
    return render_panel(request, "hmi/k/admin/_github.html", S=s, GH=github_data(s, msg, form), hold=hold or bool(msg))


@action("ghSave")
def a_gh_save(request, s, p):
    deny = _need(request, s, "updates.install")
    if deny:
        return deny
    f = {"repo": p.get("repo", ""), "token": p.get("token", ""), "daily": bool(p.get("daily"))}
    r = run(request, s, "connSave", {"group": "github", "fields": f})
    f.pop("token")
    return github_panel(request, s, {"ok": r["ok"], "text": r.get("msg") if r["ok"] else r["error"]}, None if r["ok"] else f)


@action("ghRemove")
def a_gh_remove(request, s, p):
    deny = _need(request, s, "updates.install")
    if deny:
        return deny
    r = run(request, s, "connRemove", {"group": "github"})
    return github_panel(request, s, {"ok": r["ok"], "text": "Updates from GitHub turned off; the token was removed." if r["ok"] else r["error"]})


@action("ghCheck")
def a_gh_check(request, s, p):
    from plant import ghupdates
    deny = _need(request, s, "updates.install")
    if deny:
        return deny
    try:
        ghupdates.check()
    except ghupdates.GHError as e:
        return github_panel(request, s, {"ok": False, "text": str(e)})
    return github_panel(request, s, hold=True)


@action("ghInstall")
def a_gh_install(request, s, p):
    from plant import ghupdates
    deny = _need(request, s, "updates.install")
    if deny:
        return deny
    when, at = p.get("when") or "idle", p.get("at") or ""
    if when == "at" and not at:
        return thin.refused("Pick the date and time.")
    try:
        m = ghupdates.install(s.actor.name, when, at)
    except ghupdates.GHError as e:
        return github_panel(request, s, {"ok": False, "text": str(e)})
    engine.audit(engine.Ctx(user=s.actor), f"Update v{m['version']} rev {m['revision']} downloaded from GitHub")
    return thin.trigger(github_panel(request, s, {"ok": True, "text": f"v{m['version']} rev {m['revision']} downloaded, checked and waiting to install (see above)."}),
                        changed=True, toast=f"v{m['version']} rev {m['revision']} downloaded from GitHub and checked.")


# ---------------------------------------------------------------- settings copy
@action("configImport")
def a_config_import(request, s, p):
    try:
        o = json.loads(p.get("data") or "")
    except ValueError:
        return thin.refused("That is not valid settings text. Copy it again from the other station.")
    if not isinstance(o, dict) or not isinstance(o.get("config"), dict) or not isinstance(o.get("lines"), list):
        return thin.refused("Settings text is missing the machines or config section.")
    r = run(request, s, "configImport", {"data": o})
    return thin.done(toast=r.get("msg") or "Settings loaded", close=True) if r["ok"] else thin.refused(r["error"])


CONFIRM = {
    "permReset": ("Reset permissions to the defaults?", "Every role gets its standard permissions back, and every allow or deny set for a person is removed.", "", "Reset permissions"),
    "lineDel": ("Remove {id}?", "{queued}Training and feed rates for this machine are deleted. Past downtime, QC and reports keep the name.",
                "To stop it temporarily, untick In service instead.", "Remove machine"),
    "shiftDel": ("Remove {name}?", "{name} and its breaks come off the schedule timeline. Past reports keep the name.", "", "Remove shift"),
    "configReset": ("Restore default settings?", "Reasons, checklists, shifts, parts and rules go back to the defaults. Machines, people, MOs and records are not changed.", "", "Restore defaults"),
    "dataReset:demo": ("Load demo data?", "Replaces everything on the server with the example plant: MOs, shifts, records and people. Use this only for training.", "", "Load demo data"),
    "dataReset:clear": ("Clear demo data and go live?", "Removes all MOs, shifts, downtime, QC, rolls, tickets and notes, and turns off the demo machine feeds. Machines, people, training, settings and connections stay.",
                        "If the server was running on demo data, its demo history is deleted too.", "Clear and go live"),
    "srcDelete": ("Remove this data source?", "The data it already sent is kept.", "", "Remove"),
    "srcKey": ("Make a new key?", "The system sending data must be given the new key; the old one stops working now.", "", "Make a new key"),
    "alertDelete": ("Remove this person from downtime alerts?", "", "", "Remove"),
    "alertConfig:smtp-remove": ("Remove the mail server?", "Email alerts stop.", "", "Remove"),
    "alertConfig:sms-remove": ("Remove Twilio?", "Text alerts stop.", "", "Remove"),
    "connRemove": ("Remove this connection?", "Anything that uses it stops until it is set up again.", "", "Remove"),
    "feedKey": ("Make a new feed key?", "Gateways using the old key stop being accepted until they get the new one.", "", "Make a new key"),
    "updWhen:now": ("Install the update now?", "The suite stops for a minute or two while it installs.", "", "Install now"),
    "updCancel": ("Remove the waiting update?", "", "", "Remove"),
    "updGoBack": ("Go back to the previous version?", "The suite stops for a minute or two.", "", "Go back"),
}
