"""Admin › Health: is everything the suite depends on working, and since when not?

One page for IT and shift leads to spot silent failures: the minute ticker, each machine's data feed (and whether a
machine is stopped or just not reporting), every connection's last answer, incoming data sources, the outgoing
webhook queue, cameras and live video, sensors, backups, disk space, sign-in failures and recent server errors.

Recent errors: a logging handler (in LOGGING, every process) keeps the last 60 warnings and errors of the suite's own
logger in the shared cache, so this page shows what the web server and the ticker have logged without opening a file.
"""
import logging
import shutil
import threading
import time
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

KEY = "health:errors"
KEEP = 60
_busy = threading.local()


class RecentErrors(logging.Handler):
    """Keeps the newest warnings and errors in the cache (never raises, never logs about itself)."""

    def emit(self, record):
        if getattr(_busy, "on", False) or record.levelno < logging.WARNING:
            return
        _busy.on = True
        try:
            msg = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                msg += f" ({type(record.exc_info[1]).__name__}: {record.exc_info[1]})"
            item = {"t": time.time(), "level": record.levelname, "where": record.name, "msg": msg[:400]}
            items = (cache.get(KEY) or [])[-(KEEP - 1):] + [item]
            cache.set(KEY, items, 7 * 86400)
        except Exception:  # noqa: BLE001 - the database (and its cache table) may be the thing that's down
            pass
        finally:
            _busy.on = False


def count(kind, n=1):
    """Count something for today (sign-in failures, refused keys): health:count:<kind>:<day>."""
    k = f"health:count:{kind}:{timezone.localdate().isoformat()}"
    try:
        cache.add(k, 0, 3 * 86400)
        cache.incr(k, n)
    except Exception:  # noqa: BLE001
        pass


def counted(kind, day=None):
    try:
        return cache.get(f"health:count:{kind}:{(day or timezone.localdate()).isoformat()}") or 0
    except Exception:  # noqa: BLE001
        return 0


def _ago(t, now):
    if not t:
        return None
    return max(0, int((now - t).total_seconds()))


def ago_text(s):
    s = int(s or 0)
    return f"{s} s" if s < 120 else f"{s // 60} min" if s < 7200 else f"{s // 3600} h" if s < 172800 else f"{s // 86400} days"


def machine_state(m, ls, sh, now):
    """running | stopped | changeover | not reporting | no feed | no shift: stopped and not reporting are never mixed up."""
    feed = (ls.feed if ls else {}) or {}
    t = feed.get("t")
    age = (now.timestamp() * 1000 - t) / 1000 if t else None
    if (m.feed_type or "none") == "none":
        return "no feed", "No machine feed is set (Admin › Machines)."
    stale = not feed or age is None or age > 180
    if not sh:   # nothing running on it: no data is expected to matter
        return "no shift", ("No log open, and no reading lately." if stale or not feed.get("ok") else "Data is coming in; no log is open.")
    if stale:
        return "not reporting", "No reading in the last 3 minutes" + (f" (last {ago_text(age)} ago)" if age else "") + "."
    if not feed.get("ok"):
        return "not reporting", feed.get("err") or "The feed answered with an error."
    if sh.state == "changeover":
        return "changeover", ""
    return ("stopped", "") if sh.state == "stopped" else ("running", "")


def build():
    from cameras import live
    from cameras.models import Camera
    from hmi.models import DataSource, Event, LineState, ShiftRun
    from plant import dbbackup
    from plant.models import Machine, PlantSettings
    now = timezone.now()
    st = PlantSettings.load()
    out = {"now": now}
    seen = st.ticker_seen
    out["ticker"] = {"owner": st.ticker_owner, "seen": seen, "ago": ago_text(_ago(seen, now)),
                     "ok": bool(seen and (now - seen).total_seconds() < 60)}
    lss = {x.machine_id: x for x in LineState.objects.all()}
    shs = {x.machine_id: x for x in ShiftRun.objects.filter(active=True)}
    ms = []
    for m in Machine.objects.filter(stage="s1", active=True):
        ls, sh = lss.get(m.pk), shs.get(m.pk)
        state, why = machine_state(m, ls, sh, now)
        feed = (ls.feed if ls else {}) or {}
        ms.append({"name": m.name, "feed": m.feed_type, "state": state, "why": why,
                   "last": datetime.fromtimestamp(feed["t"] / 1000, tz=dt_timezone.utc) if feed.get("t") else None,
                   "runSrc": feed.get("runErr") or "", "error": feed.get("error") or ""})
    out["machines"] = ms
    out["machinesBad"] = sum(1 for x in ms if x["state"] in ("not reporting",))
    labels = {"s2k": "S2K", "people": "People list", "paycom": "Paycom", "assets": "Asset database", "influx": "InfluxDB",
              "grafana": "Grafana", "smtp": "Email (SMTP)", "machinedb": "Machine database"}
    out["conns"] = sorted(({"key": k, "label": labels.get(k, k), "ok": bool(v.get("ok")), "msg": v.get("msg", ""),
                            "t": datetime.fromtimestamp(v["t"] / 1000, tz=dt_timezone.utc) if v.get("t") else None}
                           for k, v in (st.conn or {}).items() if isinstance(v, dict)), key=lambda x: (x["ok"], x["label"]))
    out["sources"] = list(DataSource.objects.order_by("name").values("name", "mode", "enabled", "last_at", "last_ok", "last_msg", "received"))
    unsent = Event.objects.filter(sent=False)
    oldest = unsent.order_by("t").values_list("t", flat=True).first()
    out["webhook"] = {"on": bool(st.settings.get("webhook")), "queue": unsent.count(), "failing": unsent.exclude(last_error="").count(),
                      "maxTries": max([0] + list(unsent.values_list("attempts", flat=True)[:500])),
                      "lastError": unsent.exclude(last_error="").order_by("-last_try").values_list("last_error", flat=True).first() or "",
                      "oldestAgo": ago_text(_ago(oldest, now)) if oldest else None}
    try:
        lv = live.status()
    except Exception:  # noqa: BLE001
        lv = {}
    out["cameras"] = {"n": Camera.objects.filter(enabled=True).count(), "live": Camera.objects.filter(enabled=True, live=True).count(),
                      "video": lv}
    try:
        from sensors.models import Sensor, SensorAlarm
        out["sensors"] = {"n": Sensor.objects.filter(enabled=True).count(), "fault": Sensor.objects.filter(enabled=True, fault=True).count(),
                          "alarms": SensorAlarm.objects.filter(end__isnull=True).count()}
    except Exception:  # noqa: BLE001 - fields differ in older databases
        out["sensors"] = None
    bl = dbbackup.listing()
    out["backup"] = {"last": bl[0]["at"] if bl else None, "n": len(bl),
                     "old": not bl or (now - bl[0]["at"]) > timedelta(days=2), "offsite": offsite_status()}
    disks = []
    for label, p in (("Suite folder", Path(settings.BASE_DIR)), ("Backups", dbbackup.folder())):
        try:
            p.mkdir(parents=True, exist_ok=True)
            u = shutil.disk_usage(p)
            disks.append({"label": label, "path": str(p), "free": u.free, "total": u.total, "pct": round(100 * u.free / u.total, 1),
                          "low": u.free < 5 * 1024 ** 3 or u.free / u.total < 0.1})
        except OSError as e:
            disks.append({"label": label, "path": str(p), "err": str(e)})
    out["disks"] = disks
    out["auth"] = {"pin": counted("pin"), "feed": counted("feedkey"), "ingest": counted("ingestkey"), "station": counted("stationkey")}
    try:
        errs = list(reversed(cache.get(KEY) or []))
    except Exception:  # noqa: BLE001
        errs = []
    for e in errs:
        e["at"] = datetime.fromtimestamp(e["t"], tz=dt_timezone.utc)
    out["errors"] = errs[:40]
    bad = [x for x in (not out["ticker"]["ok"], out["machinesBad"], any(not c["ok"] for c in out["conns"]),
                       out["webhook"]["failing"], out["backup"]["old"], any(d.get("low") or d.get("err") for d in disks)) if x]
    out["summary"] = "ok" if not bad else "warn"
    return out


def offsite_status():
    from plant import offsite
    return offsite.status()
