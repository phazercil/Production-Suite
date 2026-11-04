"""QC checks: which items each stage checks (Admin › QC checks), when a check is due, and pass / fail.

Each stage has its own list of items. Built-in items are the drawing measurements (checked against the
part's specs ± its tolerance), the glue and cut checks and the scrap count; Admin can add its own pass/fail
checks and numbers (with an allowed range). Stored in PlantSettings.config["qcItems"] = {stage: [item, ...]},
where an item is {"key": built-in key} or {"key": "c...", "label", "type": "check" | "number", "min", "max"}.

A check is due right after a changeover, as soon as the machine runs, and then every "QC check interval"
minutes. One saved more than "QC late after" minutes after it was due needs a reason. A check that fails
waits for a supervisor to approve it, or to disapprove it and scrap filters from the count.
"""
from collections import OrderedDict
from datetime import timedelta

BUILTIN = OrderedDict([
    ("len", {"label": "Length", "type": "measure", "spec": "len"}),
    ("wid", {"label": "Width", "type": "measure", "spec": "wid"}),
    ("ph", {"label": "Pleat height", "type": "measure", "spec": "ph"}),
    ("sbl", {"label": "Side band height L", "type": "measure", "spec": "sb"}),
    ("sbr", {"label": "Side band height R", "type": "measure", "spec": "sb"}),
    ("glue", {"label": "Glue", "type": "check"}),
    ("cut", {"label": "Cut", "type": "check"}),
    ("scrap", {"label": "Scrap count, past 30 min", "type": "count"}),
])
DEFAULTS = {"s1": list(BUILTIN), "eb": ["glue", "scrap"], "ac": ["scrap"], "gb": ["glue", "scrap"]}
TYPES = {"check": "Pass / fail", "number": "Number (with an allowed range)"}


def items_for(stage, cfg=None):
    """The QC items a stage checks, in order, each a full item dict."""
    from plant.models import PlantSettings
    cfg = cfg if cfg is not None else PlantSettings.load().config
    raw = (cfg.get("qcItems") or {}).get(stage)
    if not isinstance(raw, list):
        raw = [{"key": k} for k in DEFAULTS.get(stage, ["scrap"])]
    out = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        k = it.get("key")
        if k in BUILTIN:
            out.append({"key": k, **BUILTIN[k], "builtin": True})
        elif k and it.get("type") in TYPES:
            out.append({"key": k, "label": it.get("label") or k, "type": it["type"], "min": it.get("min"), "max": it.get("max"), "builtin": False})
    return out


def target(item, part):
    return part.get(item["spec"]) if item["type"] == "measure" else None


def parse(items, raw):
    """{key: value} from a submitted form; raises ValueError with what to fix."""
    vals = {}
    for it in items:
        k, v = it["key"], raw.get(it["key"])
        if it["type"] == "check":
            if v not in ("Pass", "Fail"):
                raise ValueError(f"Pick Pass or Fail for {it['label']}.")
            vals[k] = v
            continue
        s = str(v if v is not None else "").strip()
        try:
            n = float(s)
        except ValueError:
            n = None
        if it["type"] == "count":
            if s == "":
                n = 0
            if n is None or n < 0 or n > 1_000_000 or n != int(n):
                raise ValueError(f"Enter {it['label'].lower()} as a whole number (0 if none).")
            vals[k] = int(n)
        else:
            if n is None or not (-1e6 < n < 1e6):
                raise ValueError(f"Enter {it['label']}" + (" in inches." if it["type"] == "measure" else "."))
            vals[k] = n
    return vals


def judge(items, vals, part):
    """(passed, [keys out of spec])."""
    fails = []
    for it in items:
        k, v = it["key"], vals.get(it["key"])
        if it["type"] == "check" and v == "Fail":
            fails.append(k)
        elif it["type"] == "measure" and v is not None:
            t = target(it, part)
            if t is not None and abs(v - t) > float(part.get("tol") or 0) + 1e-9:
                fails.append(k)
        elif it["type"] == "number" and v is not None:
            if (it.get("min") not in (None, "") and v < float(it["min"])) or (it.get("max") not in (None, "") and v > float(it["max"])):
                fails.append(k)
    return not fails, fails


def due_at(sh, settings, now):
    """When this shift's next QC check is (or was) due; None when none is due (QC reminders off, or waiting for the
    machine to run after a changeover)."""
    if not sh or not sh.active or not settings.get("autoQC"):
        return None
    if sh.qc_after_co:
        return sh.qc_due_at   # set by the minute tick when the machine first runs after the changeover
    every = timedelta(minutes=float(settings.get("qcEvery") or 30))
    return (sh.qc_last or sh.start) + every


def late_by(sh, settings, rules, now):
    """Minutes a due check is past its "QC late after" allowance (0 if it isn't late)."""
    d = due_at(sh, settings, now)
    if d is None:
        return 0
    over = (now - d).total_seconds() / 60 - float(rules.get("qcLate", 10) or 0)
    return max(0, int(over + 0.999)) if over > 0 else 0
