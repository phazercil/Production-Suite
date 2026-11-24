"""Data sent to the suite by other systems (Admin > Connections > Incoming data).

A source either pushes JSON to POST /api/ingest/<name> with its key in the X-Source-Key header, or the
server collects JSON from the source's URL every N seconds (plant network only, like every other connection).
The JSON can be one object or a list of objects. Each object is read with the source's field map:

  QC measurements (machine vision): time, line, part, MO, length, width, pleat height, side band left/right,
  and a pass/fail field. Missing pass/fail is worked out from the part's drawing specs and tolerance.
  Values in millimetres are converted to inches when the map says so.
  Other readings: time, line, and the number fields to keep (all top-level numbers if none are listed).
"""
import hashlib
import hmac
import math
import re
import secrets
from datetime import datetime, timedelta, timezone as dt_tz

from django.db import transaction
from django.utils import timezone

from plant.models import MO, Machine, PartSpec, part_key

from . import qc
from .models import AutoQC, DataSource, InboundRecord, ShiftRun

MAX_ITEMS = 1000
MEASURES = ("len", "wid", "ph", "sbl", "sbr")
MEASURE_NAMES = {"len": "Length", "wid": "Width", "ph": "Pleat height", "sbl": "Side band L", "sbr": "Side band R"}
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{2,60}$")
PASS_WORDS = {"pass", "passed", "ok", "good", "true", "1", "yes", "accept", "accepted", "p"}
FAIL_WORDS = {"fail", "failed", "ng", "bad", "false", "0", "no", "reject", "rejected", "f"}


class InboundError(Exception):
    pass


def new_key():
    return secrets.token_urlsafe(24)


def key_hash(key):
    return hashlib.sha256(str(key).encode()).hexdigest()


def key_ok(src, given):
    return bool(src.key_hash and given) and hmac.compare_digest(src.key_hash, key_hash(given))


def get_path(obj, path):
    """Value at a dotted path such as "result.dims.length" or "items.0.value"."""
    if not path:
        return None
    cur = obj
    for part in str(path).split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def field_paths(obj, prefix="", depth=0):
    """Every field path in a sample object, to pick from on the Admin page."""
    out = []
    if isinstance(obj, dict) and depth < 4:
        for k, v in list(obj.items())[:80]:
            p = f"{prefix}{k}"
            if isinstance(v, (dict, list)):
                out += field_paths(v, p + ".", depth + 1)
            else:
                out.append(p)
    elif isinstance(obj, list) and obj and depth < 4:
        out += field_paths(obj[0], prefix + "0.", depth + 1)
    return out[:200]


def _num(v):
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(str(v).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _time(v, now):
    """A time from the data: ISO text, or seconds / milliseconds since 1970. Missing or odd -> now."""
    return time_why(v, now)[0]


def time_why(v, now):
    """(time, why it was replaced by the time received: "" when the record's own time was used, else
    "missing", "unreadable", "ahead" (more than 10 minutes in the future) or "old" (over a year ago))."""
    if v in (None, ""):
        return now, "missing"
    n = _num(v)
    try:
        if n is not None:
            t = datetime.fromtimestamp(n / 1000 if n > 1e11 else n, tz=dt_tz.utc)
        else:
            t = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
            if timezone.is_naive(t):
                t = timezone.make_aware(t)
    except (ValueError, OverflowError, OSError):
        return now, "unreadable"
    # never trust a clock far in the future, or records older than a year
    if t > now + timedelta(minutes=10):
        return now, "ahead"
    if t < now - timedelta(days=366):
        return now, "old"
    return t, ""


def _line(src, item, lines):
    if src.line:
        return src.line
    raw = str(get_path(item, (src.mapping or {}).get("line")) or "").strip()
    return lines.get(raw.lower(), "")


def map_item(src, item, now, lines, specs):
    """One incoming object -> what will be stored, or raises InboundError with the reason it was skipped."""
    if not isinstance(item, dict):
        raise InboundError("not a JSON object")
    m = src.mapping or {}
    sent = get_path(item, m.get("time")) if m.get("time") else None
    t, why = time_why(sent, now)
    if not m.get("time"):
        why = ""      # no time field mapped: the time received is the record's time
    rid = get_path(item, m.get("id")) if m.get("id") else None
    stamp = {"sent_t": "" if sent is None else str(sent)[:40], "t_fixed": why, "ext_id": "" if rid in (None, "") else str(rid)[:80]}
    line = _line(src, item, lines)
    if not line:
        raise InboundError("no line: set a fixed line for this source, or map the field that names the line")
    if src.kind == "readings":
        keep = [f for f in (m.get("fields") or []) if f]
        vals = {}
        if keep:
            for f in keep[:50]:
                n = _num(get_path(item, f))
                if n is not None:
                    vals[f] = n
        else:
            vals = {k: _num(v) for k, v in list(item.items())[:50] if _num(v) is not None}
        if not vals:
            raise InboundError("no number fields to keep")
        return {**stamp, "kind": "readings", "t": t, "line": line, "data": vals}
    scale = 1 / 25.4 if str(m.get("units") or "in").lower() == "mm" else 1.0
    vals = {}
    for k in MEASURES:
        n = _num(get_path(item, m.get(k)))
        vals[k] = round(n * scale, 4) if n is not None else None
    if all(v is None for v in vals.values()) and not m.get("passed"):
        raise InboundError("no measurements and no pass/fail in it")
    part = str(get_path(item, m.get("part")) or "").strip()[:40]
    mo = str(get_path(item, m.get("mo")) or "").strip()[:40]
    passed = None
    raw = get_path(item, m.get("passed")) if m.get("passed") else None
    if raw is not None:
        w = str(raw).strip().lower()
        passed = True if w in PASS_WORDS else False if w in FAIL_WORDS else None
    return {**stamp, "kind": "qc", "t": t, "line": line, "part": part, "mo": mo, "vals": vals, "passed": passed, "raw": item}


def _spec(item, specs):
    """The part's specs: its own, else the longest spec key it starts with (MG3672 before MG36)."""
    k = part_key(item)
    if not k:
        return None
    return specs.get(k) or next((specs[key] for key in sorted(specs, key=len, reverse=True) if key and k.startswith(key)), None)


def ingest(src, payload):
    """Store everything usable in payload. Returns {"stored", "skipped", "errors"}."""
    items = payload if isinstance(payload, list) else [payload]
    if len(items) > MAX_ITEMS:
        raise InboundError(f"Send at most {MAX_ITEMS} records at a time.")
    now = timezone.now()
    lines = {m.name.lower(): m.name for m in Machine.objects.all()}
    specs = {s.key: s for s in PartSpec.objects.all()}
    tols = None   # Quality › Tolerances, read when a record needs judging
    stored, errors, fixed = 0, [], 0
    with transaction.atomic():
        active = {sh.line: sh for sh in ShiftRun.objects.filter(active=True).select_related("mo")}
        # replays: a record whose own id (the mapped id field) this source already sent is skipped
        idf = (src.mapping or {}).get("id")
        ids = [str(get_path(x, idf))[:80] for x in items if idf and isinstance(x, dict) and get_path(x, idf) not in (None, "")]
        seen = set()
        if ids:
            for model in (InboundRecord, AutoQC):
                for k in range(0, len(ids), 500):
                    seen |= set(model.objects.filter(source=src, ext_id__in=ids[k:k + 500]).values_list("ext_id", flat=True))
        repeats = 0
        measured = {}
        for i, item in enumerate(items):
            try:
                r = map_item(src, item, now, lines, specs)
            except InboundError as e:
                if len(errors) < 5:
                    errors.append(f"record {i + 1}: {e}")
                continue
            if r["ext_id"]:
                if r["ext_id"] in seen:
                    repeats += 1
                    continue
                seen.add(r["ext_id"])
            if r["kind"] == "readings":
                InboundRecord.objects.create(source=src, source_name=src.name, t=r["t"], line=r["line"], data=r["data"],
                                             sent_t=r["sent_t"], t_fixed=r["t_fixed"], ext_id=r["ext_id"])
            else:
                sh = active.get(r["line"])
                part = r["part"] or (sh.mo.part if sh and sh.mo else "")
                mo = r["mo"] or (sh.mo.number if sh and sh.mo else "")
                passed = r["passed"]
                if passed is None:
                    sp = _spec(part, specs)
                    if not sp:
                        if len(errors) < 5:
                            errors.append(f"record {i + 1}: no pass/fail field and no drawing specs for part {part or '(none)'}")
                        continue
                    target = {"len": sp.len, "wid": sp.wid, "ph": sp.ph, "sbl": sp.sb, "sbr": sp.sb}
                    tols = tols if tols is not None else qc.plant_tols()
                    spec = {"len": "len", "wid": "wid", "ph": "ph", "sbl": "sb", "sbr": "sb"}
                    passed = all(qc.in_tol(v, target[k], spec[k], {"tol": sp.tol}, tols) for k, v in r["vals"].items()
                                 if v is not None and target.get(k) is not None)   # a dimension the specs don't have isn't judged
                extra = {k: v for k, v in r["raw"].items() if isinstance(v, (int, float, str, bool)) and len(str(v)) < 200} if isinstance(r["raw"], dict) else {}
                measured.setdefault((r["line"], part), set()).update(k for k, v in r["vals"].items() if v is not None)
                AutoQC.objects.create(source=src, source_name=src.name, shift=sh, t=r["t"], line=r["line"], mo=mo, item=part,
                                      sent_t=r["sent_t"], t_fixed=r["t_fixed"], ext_id=r["ext_id"],
                                      passed=bool(passed), data=dict(list(extra.items())[:40]), **r["vals"])
            stored += 1
            if r["t_fixed"]:
                fixed += 1
        src.received += stored
        src.last_at = now
        src.last_ok = stored > 0 or not items or repeats == len(items)
        src.last_msg = (f"{stored} stored" + (f", {len(items) - stored} skipped ({'; '.join(errors[:2])})" if stored < len(items) else "")
                        + (f"; {repeats} already received (same id) skipped" if repeats else "")
                        + (f"; {fixed} with a missing or wrong time kept at the time received (its own time is kept as sent)" if fixed else ""))[:300]
        if items and isinstance(items[-1], dict):
            src.sample = {k: v for k, v in list(items[-1].items())[:60]}
        src.save(update_fields=["received", "last_at", "last_ok", "last_msg", "sample"])
    if measured:
        from quality import spc
        for (line, part), keys in measured.items():
            spc.check_new(line, part, sorted(keys))
    try:   # sensors that read a field of this source (Admin › Sensors & alarms)
        from sensors.engine import from_source
        from_source(src, items)
    except Exception:  # noqa: BLE001 - never lose the stored records over a sensor
        import logging
        logging.getLogger("plant").exception("sensors from %s", src.name)
    return {"stored": stored, "skipped": len(items) - stored, "errors": errors, "timeFixed": fixed, "repeats": repeats}


def _later(a, b):
    """Is mark a after mark b? Numbers compare as numbers, anything else (ISO times, ids) as text."""
    if not a:
        return False
    if not b:
        return True
    na, nb = _num(a), _num(b)
    return na > nb if na is not None and nb is not None else a > b


def poll(src):
    """Collect from a source's URL. Only records newer than the last one collected are stored."""
    from . import integrations
    data = integrations.get_json(src.url)
    items = data if isinstance(data, list) else (data.get("items") or data.get("data") or data.get("results") or [data]) \
        if isinstance(data, dict) else []
    if not isinstance(items, list):
        items = [items]
    idf = (src.mapping or {}).get("id") or (src.mapping or {}).get("time")

    def mark(x):
        return str(get_path(x, idf) or "") if isinstance(x, dict) and idf else ""
    if idf and src.poll_mark:
        items = [x for x in items if _later(mark(x), src.poll_mark)]
    items = items[-MAX_ITEMS:]
    res = ingest(src, items) if items else {"stored": 0, "skipped": 0, "errors": []}
    if idf and items:
        best = ""
        for x in items:
            if _later(mark(x), best):
                best = mark(x)
        if best:
            DataSource.objects.filter(pk=src.pk).update(poll_mark=best[:80])
    if not items:
        DataSource.objects.filter(pk=src.pk).update(last_at=timezone.now(), last_ok=True, last_msg="Nothing new")
    return res


def preview(src, payload, limit=10):
    """What the field map would make of a sample, without storing anything."""
    items = payload if isinstance(payload, list) else [payload]
    now = timezone.now()
    lines = {m.name.lower(): m.name for m in Machine.objects.all()}
    out = []
    for i, item in enumerate(items[:limit]):
        try:
            r = map_item(src, item, now, lines, {})
            r.pop("raw", None)
            r["t"] = r["t"].isoformat()
            out.append({"ok": True, **r})
        except InboundError as e:
            out.append({"ok": False, "error": f"record {i + 1}: {e}"})
    return out
