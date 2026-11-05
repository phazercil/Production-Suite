"""Connector builder: every piece of data the suite needs from the plant's databases, what each one is linked to now,
and saving a set of links (a "group": one machine's readings, S2K's open MOs, or one changeover data query).

It is a visual front end over the settings the rest of Admin already uses, so nothing is stored twice:
- a machine's readings go into its feed settings (feed_type "db", feed_src, ppm/count/recipe fields, run signal,
  error fields), exactly as Admin › Machines sets them;
- S2K's open MOs and the changeover queries are saved as the same SQL the S2K and Changeover data builders make.
Table and column names are always checked against the database's own list before anything is saved.
"""
import re

from plant.models import Machine

from . import integrations

DB_LABEL = {"machines": "Machine data database", "assets": "Asset database", "s2k": "S2K database"}
MACHINE_SLOTS = [("orderCol", "Newest row (time stamp or ID)", True, "Which row is the latest reading"),
                 ("keyCol", "Which machine (column)", False, "Only when one table holds several machines"),
                 ("ppm", "Speed · pleats per minute", True, "The pleater's speed"),
                 ("count", "Filters made (counter)", False, "Without it, filters come from speed ÷ pleats"),
                 ("recipe", "Recipe loaded", False, ""),
                 ("run", "Run signal (High = running)", False, "Without it, running = speed above the stop speed"),
                 ("errCode", "Error / alarm code", False, ""),
                 ("errText", "Error / alarm text", False, "")]
S2K_FILTER = ("fcol", "Only rows where (column)", False, "With the value below, e.g. status = OPEN")


def _q(name):
    return "`" + str(name).replace("`", "``") + "`"


def ready_dbs():
    integrations.liveconfig.refresh()
    return [d for d in ("machines", "assets", "s2k") if d in integrations.connections.databases]


_ID = r"(?:`(?:[^`]|``)+`|\"(?:[^\"]|\"\")+\"|\[(?:[^\]]|\]\])+\])"   # `mysql`, "postgres" or [sql server]


def _unq(x):
    x = x.strip()
    if x[:1] == "`":
        return x[1:-1].replace("``", "`")
    if x[:1] == '"':
        return x[1:-1].replace('""', '"')
    if x[:1] == "[":
        return x[1:-1].replace("]]", "]")
    return x


def parse_sql(sql):
    """Table and column links back out of SQL the suite's own builders made (any of the three dialects):
    FROM `t` / "s"."t" / [t], `col` AS role, `col` = :part / :machine / :from_part, `col` = 'value'."""
    sql = sql or ""
    out = {"table": "", "map": {}, "fval": ""}
    m = re.search(rf"FROM\s+({_ID})(?:\.({_ID}))?", sql, re.I)
    if m:
        out["table"] = _unq(m.group(1)) + ("." + _unq(m.group(2)) if m.group(2) else "")
    for col, role in re.findall(rf"({_ID})\s+AS\s+(\w+)", sql, re.I):
        out["map"][role.lower()] = _unq(col)
    for col, par in re.findall(rf"({_ID})\s*=\s*:(part|machine|from_part)\b", sql, re.I):
        out["map"]["f_" + {"part": "part", "machine": "machine", "from_part": "from"}[par.lower()]] = _unq(col)
    m = re.search(rf"WHERE\s+({_ID})\s*=\s*'((?:[^']|'')*)'", sql, re.I)
    if m:
        out["map"]["fcol"] = _unq(m.group(1))
        out["fval"] = m.group(2).replace("''", "'")
    return out


def _slots(defs, current):
    return [{"k": k, "label": lbl, "req": req, "help": hlp, "col": current.get(k, "")} for k, lbl, req, hlp in defs]


def groups():
    """Everything the suite can take from a database, with what each is linked to now."""
    from .kadmin import S2K_ROLES
    from .models import SavedQuery
    from .snapshot import QDEF
    saved = {x.name: x for x in SavedQuery.objects.all()}
    out = []
    for m in Machine.objects.filter(stage="s1", active=True).order_by("sort", "id"):
        src, up, err = dict(m.feed_src or {}), dict(m.uptime_src or {}), dict(m.error_src or {})
        cur, other = {}, ""
        if m.feed_type == "db":
            cur = {"orderCol": src.get("orderCol", ""), "keyCol": src.get("keyCol", ""), "ppm": m.feed_ppm_field or "",
                   "count": m.feed_count_field, "recipe": m.feed_recipe_field,
                   "run": up.get("field", "") if up.get("type") == "feed" else "",
                   "errCode": err.get("field", "") if err.get("type") == "feed" else "",
                   "errText": err.get("textField", "") if err.get("type") == "feed" else ""}
        elif m.feed_type not in ("none", ""):
            other = dict(Machine._meta.get_field("feed_type").choices).get(m.feed_type, m.feed_type)
        out.append({"key": f"m:{m.pk}", "kind": "machine", "title": m.name, "sub": "Machine readings", "color": m.color,
                    "dbs": ["machines", "assets"], "db": src.get("db") or "machines", "table": src.get("table", "") if m.feed_type == "db" else "",
                    "slots": _slots(MACHINE_SLOTS, cur), "keyVal": src.get("keyVal", "") or m.name, "keyValLabel": "Value for this machine",
                    "other": other})
    sv = saved.get("s2k_mos")
    p = parse_sql(sv.sql) if sv else {"table": "", "map": {}, "fval": ""}
    s2k_defs = [(k, lbl, req, "") for k, lbl, req in S2K_ROLES] + [S2K_FILTER]
    out.append({"key": "s2k", "kind": "s2k", "title": "Open MOs", "sub": "From S2K: what the Scheduler plans", "color": "",
                "dbs": ["s2k"], "db": "s2k", "table": p["table"], "slots": _slots(s2k_defs, p["map"]), "fval": p["fval"],
                "custom": bool(sv and not p["table"]), "other": ""})
    for name, D in QDEF.items():
        sv = saved.get(name)
        p = parse_sql(sv.sql) if sv else {"table": "", "map": {}, "fval": ""}
        defs = [("f_part", "Part number (to look the part up)", True, "")]
        if name != "part_spec":
            defs.append(("f_machine", "Machine (blank rows = every machine)", False, ""))
        if name == "changeover_steps":
            defs.append(("f_from", "Part changing from", False, ""))
        defs += [(r[0], r[1], bool(len(r) > 2 and r[2]), "") for r in D["roles"]]
        out.append({"key": f"q:{name}", "kind": "query", "title": D["title"], "sub": D["help"], "color": "", "dbs": ["assets"], "db": "assets",
                    "table": p["table"], "slots": _slots(defs, p["map"]), "custom": bool(sv and not p["table"]), "other": ""})
    by_table = {}
    for g in out:
        if g["kind"] == "machine" and g["table"]:
            by_table.setdefault((g["db"], g["table"]), []).append(g["title"])
    for g in out:
        g["sharedWith"] = [t for t in by_table.get((g["db"], g["table"]), []) if t != g["title"]] if g["kind"] == "machine" else []
        req = [s for s in g["slots"] if s["req"]]
        done = [s for s in g["slots"] if s["col"]]
        g["missing"] = [s["label"] for s in req if not s["col"]]
        g["status"] = ("custom" if g.get("custom") else "other" if g["other"] else
                       "done" if g["table"] and not g["missing"] else "partial" if done else "missing")
        g["state"] = {"db": g["db"], "table": g["table"], "map": {s["k"]: s["col"] for s in g["slots"] if s["col"]}}
    return out


def source(db, table):
    """The tables of a database, and the columns of one with a sample value each (from its newest few rows)."""
    from .kadmin import tables
    t = tables(db) if db else {"tables": [], "ready": False, "err": ""}
    names = [x["name"] for x in t.get("tables", [])]
    cols = []
    if table in names:
        info = next(x for x in t["tables"] if x["name"] == table)
        sample = {}
        try:
            prev = integrations.preview(table, db)
            for r in prev.get("rows", [])[:5]:
                for k, v in r.items():
                    if sample.get(k) in (None, "") and v not in (None, ""):
                        sample[k] = str(v)[:40]
        except Exception as e:  # noqa: BLE001 - the columns are still useful without samples
            sample = {"__err": str(e)[:160]}
        cols = [{"name": c["name"], "type": c.get("type", ""), "sample": sample.get(c["name"], "")} for c in info["columns"]]
        if "__err" in sample:
            cols and cols[0].update(err=sample["__err"])
    ok = table in names
    return {"db": db, "table": table, "tables": names, "cols": cols, "err": t.get("err", ""),
            "ready": t.get("ready", False), "missing": bool(table) and bool(names) and not ok, "found": ok}


def in_use(gs):
    """The tables the data needs read now, each once, in the order of the boxes (a card for each on the left)."""
    seen = []
    for g in gs:
        if g["table"] and (g["db"], g["table"]) not in seen:
            seen.append((g["db"], g["table"]))
    return seen


def summary(gs):
    need = [g for g in gs if g["status"] not in ("other", "custom")]
    done = [g for g in gs if g["status"] in ("done", "other", "custom")]
    return {"total": len(gs), "done": len(done), "pct": round(len(done) / len(gs) * 100) if gs else 100,
            "missing": [{"title": g["title"], "sub": g["sub"], "key": g["key"], "need": g["missing"] or ["a table"]}
                        for g in need if g["status"] != "done"]}
