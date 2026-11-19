"""Run screen extras: tooling for a part (asset database), how a part has run before, and recent shift reports."""
import logging
import time
from datetime import timedelta

from django.db.models import Avg, Count, Q, Sum

from plant.models import part_key

from . import integrations

log = logging.getLogger(__name__)

# What a changeover needs, in the order operators look for it.
KINDS = [("screws", "Screws"), ("helix", "Helix"), ("media", "Media"), ("sideband", "Sidebands")]
FRESH = timedelta(minutes=15)   # look the part up again after this long
RETRY = 120                     # seconds to wait after the asset database failed before asking again
_failed = {}


def _kind(text):
    t = str(text or "").lower()
    if "screw" in t:
        return "screws"
    if "helix" in t or "helic" in t:
        return "helix"
    if "media" in t or "paper" in t:
        return "media"
    if "side" in t or "band" in t:
        return "sideband"
    return t.strip() or "other"


def norm_tooling(rows):
    """Rows from the part_tooling query, one row per tool (kind/name/detail) or one row with a column per kind.
    Returns [{key, label, items: [{name, detail}]}] in the KINDS order, then anything else."""
    got = {}
    for r in rows or []:
        o = {str(k).lower(): v for k, v in r.items()}
        for k, _ in KINDS:
            v = o.get(k)
            if v not in (None, ""):
                got.setdefault(k, []).append({"name": str(v).strip(), "detail": ""})
        if o.get("kind") not in (None, "") or o.get("name") not in (None, ""):
            name = str(o.get("name") or "").strip()
            if name:
                got.setdefault(_kind(o.get("kind")), []).append({"name": name, "detail": str(o.get("detail") or "").strip()})
    labels = dict(KINDS)
    order = [k for k, _ in KINDS] + sorted(k for k in got if k not in labels)
    out = []
    for k in order:
        seen, items = set(), []
        for it in got.get(k, []):
            key = (it["name"], it["detail"])
            if key not in seen:
                seen.add(key)
                items.append(it)
        if k in labels or items:
            out.append({"key": k, "label": labels.get(k, k.title()), "items": items})
    return out


def tooling(part, machine, line_code="", t=None, use_db=True):
    """line_code: the S2K process code, or a dict of the machine's codes (engine.machine_params)."""
    """Tooling for a part on a machine from the asset database. Uses a saved copy when the database was asked
    in the last 15 minutes or can't be reached. src: db | cache | stale | off | noquery | none."""
    from django.utils import timezone

    from .models import SavedQuery, ToolingCache
    t = t or timezone.now()
    key = part_key(part or "")
    if not key:
        return {"src": "none", "groups": []}
    cache = ToolingCache.objects.filter(part=key, machine=machine).first()
    if not (use_db and (integrations.assets_ready() or integrations.query_ready("part_tooling"))):
        if cache:
            return {"src": "stale", "groups": cache.groups, "t": cache.t, "why": "off"}
        return {"src": "off", "groups": []}
    q = SavedQuery.objects.filter(name="part_tooling").first()
    if not q:
        return {"src": "noquery", "groups": []}
    if cache and t - cache.t < FRESH:
        return {"src": "cache" if cache.groups else "none", "groups": cache.groups, "t": cache.t}
    failed = _failed.get((key, machine))
    if failed and time.time() - failed[0] < RETRY:
        if cache:
            return {"src": "stale", "groups": cache.groups, "t": cache.t, "err": failed[1]}
        return {"src": "error", "groups": [], "err": failed[1]}
    try:
        rows = integrations.run_saved(q.sql, {"part": key, "machine": machine, "from_part": "", "mo": "", **(line_code if isinstance(line_code, dict) else {"line_code": line_code})})["rows"]
        groups = norm_tooling(rows)
        has = any(g["items"] for g in groups)
        _failed.pop((key, machine), None)
        ToolingCache.objects.update_or_create(part=key, machine=machine, defaults={"groups": groups if has else [], "t": t})
        return {"src": "db" if has else "none", "groups": groups if has else []}
    except Exception as e:  # database unreachable or the query failed
        log.warning("tooling lookup failed for %s on %s: %s", key, machine, e)
        _failed[(key, machine)] = (time.time(), str(e)[:200])
        if cache:
            return {"src": "stale", "groups": cache.groups, "t": cache.t, "err": str(e)[:200]}
        return {"src": "error", "groups": [], "err": str(e)[:200]}


# ---------------------------------------------------------------- how a part has run before

_avg_cache = {}
AVG_TTL = 300          # seconds
AVG_RUNS = 40          # most recent runs looked at, per scope
AVG_DAYS = 180


def _scope_stats(runs):
    from .models import Downtime, PpmReading
    good = made = scrap = 0
    mins = down = 0.0
    speeds = []
    for r in runs:
        span = (r.end_time - r.start_time).total_seconds() / 60
        if span <= 0:
            continue
        mins += span
        made += r.qty
        scrap += r.scrap
        good += max(0, r.qty - r.scrap)
        down += Downtime.objects.filter(line=r.line, start_time__lt=r.end_time, end_time__gt=r.start_time).aggregate(m=Sum("minutes"))["m"] or 0
        a = PpmReading.objects.filter(line=r.line, t__gte=r.start_time, t__lt=r.end_time, run=True, ppm__gt=0).aggregate(a=Avg("ppm"), n=Count("id"))
        if a["n"]:
            speeds.append((a["a"], a["n"]))
    if not mins:
        return None
    hrs = mins / 60
    n_speed = sum(n for _, n in speeds)
    return {"runs": len(runs), "hours": round(hrs, 1),
            "scrapRate": round(scrap / made * 100, 1) if made else 0.0,
            "perHour": round(good / hrs),
            "speed": round(sum(a * n for a, n in speeds) / n_speed) if n_speed else None,
            "downPerHour": round(min(down, mins) / hrs, 1),
            "downPct": round(min(down, mins) / mins * 100)}


def averages(part, line, t=None):
    """Finished runs of this part (any prefix, e.g. 1MG/2MG count as MG) on this machine and on every machine."""
    from django.utils import timezone

    from .models import MoRun
    t = t or timezone.now()
    key = part_key(part or "")
    if not key:
        return None
    ck = (key, line)
    hit = _avg_cache.get(ck)
    if hit and time.time() - hit[0] < AVG_TTL:
        return hit[1]
    since = t - timedelta(days=AVG_DAYS)
    cand = list(MoRun.objects.filter(end_time__gte=since).filter(Q(item__iexact=part) | Q(item__icontains=key)).order_by("-end_time")[:AVG_RUNS * 6])
    mine = [r for r in cand if part_key(r.item) == key]
    out = {"here": _scope_stats([r for r in mine if r.line == line][:AVG_RUNS]), "all": _scope_stats(mine[:AVG_RUNS]), "days": AVG_DAYS}
    _avg_cache[ck] = (time.time(), out)
    return out


def clear_cache():
    _avg_cache.clear()
    _failed.clear()


# ---------------------------------------------------------------- recent shift reports

def report_history(line, t, hours=24, limit=10):
    """What shifts on this line reported in the last day: issues at shift start and end, shift notes, maintenance calls."""
    from .models import ShiftNote, ShiftRun, Ticket
    since = t - timedelta(hours=hours)
    out = []
    for sh in ShiftRun.objects.filter(line=line, start__gte=since - timedelta(hours=12)).exclude(issues="", end_issues="").order_by("-start")[:12]:
        if sh.issues and sh.start >= since:
            out.append({"t": sh.start, "kind": "Shift start", "by": sh.user_name, "shift": sh.shift_name, "text": sh.issues})
        if sh.end_issues and sh.end and sh.end >= since:
            out.append({"t": sh.end, "kind": "End of shift", "by": sh.user_name, "shift": sh.shift_name, "text": sh.end_issues})
    for n in ShiftNote.objects.filter(line=line, t__gte=since).order_by("-t")[:limit]:
        out.append({"t": n.t, "kind": "Note", "by": n.by, "shift": n.shift.shift_name if n.shift_id and n.shift else "", "text": n.text})
    for k in Ticket.objects.filter(t__gte=since).filter(Q(line=line) | Q(machine=line)).order_by("-t")[:limit]:
        out.append({"t": k.t, "kind": "Maintenance", "by": k.by, "shift": k.shift.shift_name if k.shift_id and k.shift else "",
                    "text": f"{k.type}{': ' + k.notes if k.notes else ''}", "status": k.status, "bad": k.status != "Closed"})
    out.sort(key=lambda x: x["t"], reverse=True)
    return out[:limit]
