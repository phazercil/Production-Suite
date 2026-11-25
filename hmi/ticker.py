"""The plant ticker: once a minute per machine it takes a speed reading and runs `engine.tick`.
It also runs the timed pulls (S2K, people list, Paycom) and flushes the webhook outbox.

Run it as its own service with `python manage.py runplant`, or let `runserver` start it in a
background thread (TICKER_IN_PROCESS=1, the default for demos).
"""
import logging
import os
import socket
import threading
import time
from datetime import timedelta

from django.db import close_old_connections, models, transaction

from plant.models import Machine, PlantSettings
from plant.util import ms, now

from . import engine, integrations
from .models import Event, LineState

log = logging.getLogger("plant")

# Demo feed: the PPM sheet from "Digital App Development.xlsx", one reading per minute
DEMO_PPM = [190,190,190,190,191,189,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,189,190,190,190,190,190,190,190,190,190,190,190,190,189,190,190,190,189,190,190,190,190,190,190,190,142,0,0,0,0,0,42,163,164,140,0,0,0,156,94,20,27,107,163,163,164,164,164,164,164,164,164,163,163,163,164,164,164,163,149,0,0,0,0,12,0,65,147,0,125,84,89,0,0,0,0,80,190,5,0,0,80,136,90,191,165,139,87,190,190,190,190,190,190,190,190,190,190,381,189,190,190,189,190,189,190,190,190,190,190,190,190,190,190,190,190,190,190,190,381,190,190,190,191,189,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,191,189,190,190,190,104,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,96,190,190,190,190,190,189,189,190,190,189,190,190,190,190,190,190,190,190,190,190,190,189,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,191,189,190,190,190,190,189,190,190,190,189,190,190,190,190,9,0,0,0,21,14,105,190,190,190,190,136,0,7,122,37,0,0,0,0,0,0,0,35,190,86,0,98,190,190,190,190,190,190,190,190,190,190,190,191,189,190,190,190,190,190,190,190,190,52,0,0,0,99,190,190,190,190,190,190,191,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,138,0,0,42,190,15,190,85,190,97,0,32,0,134,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,189,190,190,190,191,189,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,135,13,190,190,190,190,190,190,190,190,190,190,190,190,190,191,189,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,191,189,190,190,190,190,189,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,191,189,190,190,190,190,190,190,190,190,191,189,190,190,190,190,190,190,190,190,190,190,190,190,190,151,0,0,0,0,0,0,0,0,98,190,189,190,190,190,190,190,190,190,190,190,191,189,190,190,190,190,190,190,190,190,189,190,190,190,190,190,70,134,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,190,189,190,190,190,190,190,190,96,94,190,190,190,18,89,190,190,190,190,190,190,12,152,190,190,190,190,190,130,0,0,0,103,190,187,179,179,179,179,179,48,0,88,0,0,0,0,0,0,0,0,96,38,179,179,178,24,179,179]


def demo_sample(m, ls):
    v = DEMO_PPM[(ls.demo_idx + m.demo_offset) % len(DEMO_PPM)]
    ls.demo_idx += 1
    return {"ppm": round(v * (m.demo_scale or 1))}


def machinedata_text(code, text):
    from . import machinedata
    c, x = machinedata.text(code), machinedata.text(text, 120)
    return f"{c} · {x}" if x and x != c else c


def _feed_sample(m, ls, t):
    """Returns (sample or None, feed status dict) from the machine's main source."""
    if m.feed_type == "demo":
        s = demo_sample(m, ls)
        return s, {"type": "demo", "ok": True, "err": "", "ppm": s["ppm"], "t": ms(t)}
    if m.feed_type == "http":
        try:
            s = integrations.fetch_feed(m)
            return s, {"type": "http", "ok": True, "err": "", "ppm": s["ppm"], "t": ms(t)}
        except integrations.ConnError as e:
            ok = ls.gap < 2  # a short blip is not an alarm; 2½ minutes without data is
            return None, {"type": "http", "ok": ok, "err": str(e), "ppm": None, "t": ms(t)}
    if m.feed_type == "influx":
        from . import influx
        try:
            s = influx.fetch_feed(m, t)
            return s, {"type": "influx", "ok": True, "err": "", "ppm": s["ppm"], "t": ms(t)}
        except influx.InfluxError as e:
            ok = ls.gap < 2  # a short blip is not an alarm; 2½ minutes without data is
            return None, {"type": "influx", "ok": ok, "err": str(e), "ppm": None, "t": ms(t)}
    if m.feed_type == "db":
        from . import machinedata
        prev = (ls.feed or {}).get("src") or {}
        try:
            s, state = machinedata.fetch_feed(m, prev, ms(t))
            return s, {"type": "db", "ok": True, "err": "", "ppm": s["ppm"], "t": ms(t), "src": state}
        except integrations.ConnError as e:
            ok = ls.gap < 2
            return None, {"type": "db", "ok": ok, "err": str(e), "ppm": None, "t": ms(t), "src": getattr(e, "state", None) or prev}
        except Exception as e:  # noqa: BLE001 - a broken table must never stop the ticker
            log.warning("machine table for %s: %s", m.name, e)
            return None, {"type": "db", "ok": ls.gap < 2, "err": str(e)[:200], "ppm": None, "t": ms(t), "src": prev}
    if m.feed_type == "push":
        if ls.pushed is not None and ls.pushed_at and -60 < (t - ls.pushed_at).total_seconds() < 150:   # (not a reading from after a missed minute)
            from . import machinedata
            base = {"ppm": ls.pushed.get("ppm")}
            if ls.pushed.get("count") not in (None, ""):
                base["count"] = ls.pushed["count"]
            s = machinedata.add_extras(m, base, lambda f: integrations.pick(ls.pushed, f))
            return s, {"type": "push", "ok": True, "err": "", "ppm": ls.pushed.get("ppm"), "t": ms(t)}
        return None, {"type": "push", "ok": False, "err": "No reading pushed in the last 2½ minutes", "ppm": None, "t": ms(t)}
    return None, {"type": "none", "ok": False, "err": "No machine feed set", "ppm": None, "t": ms(t)}


def sample_for(m, ls, t):
    """Returns (sample or None, feed status dict). The sample has the speed and, when the machine gives them,
    the filters-cut counter, the recipe and the run signal (High/Low) from its second source."""
    prev_run = (ls.feed or {}).get("runSrc") or {}
    s, feed = _feed_sample(m, ls, t)
    u = m.uptime_src or {}
    if s is not None and u.get("type") == "db":
        from . import machinedata
        try:
            run, state = machinedata.fetch_run(m, prev_run, ms(t))
            s["run"] = run
            feed["runSrc"] = state
        except Exception as e:  # noqa: BLE001 - without the signal, uptime falls back to the speed for this minute
            s.pop("run", None)
            s["runErr"] = str(e)[:200]
            feed["runSrc"] = getattr(e, "state", None) or prev_run
    if s is not None:
        if s.get("recipe") is not None:
            feed["recipe"] = s["recipe"]
        if u.get("type") in ("feed", "db"):
            feed["run"] = s.get("run")
            if s.get("runErr"):
                feed["runErr"] = s["runErr"]
    return s, feed


def _sensors_from_feed(m, s, minute):
    """Sensors that are extra fields of the machine's own reading (Admin › Sensors & alarms)."""
    try:
        from sensors.engine import from_feed
        with transaction.atomic():
            from_feed(m, s, minute)
    except Exception as e:  # noqa: BLE001 - a sensor never stops the minute from counting
        log.warning("sensors for %s: %s", m.name, e)
    if s is not None:
        s.pop("sensors", None)


class LeaseLost(Exception):
    pass


def _tick_one_minute(m, t, owner=None):
    """Process the next due minute for one machine in its own transaction. Returns False when it is up to date.
    `owner`: the ticker's lease name. The minute is only written while that lease is still held (and the lease is
    renewed in the same transaction), so a ticker that stalled past its lease and was replaced never writes."""
    with transaction.atomic():
        ls, _ = LineState.objects.select_for_update().get_or_create(machine=m)
        if ls.next_tick is None or t - ls.next_tick > timedelta(minutes=5):
            # first run, or the ticker was off for a while: start from this minute (missed minutes are not invented)
            ls.next_tick = t.replace(second=0, microsecond=0)
            ls.save(update_fields=["next_tick"])
        if t < ls.next_tick:
            return False
        minute = ls.next_tick
    # The machine's readings (a URL, InfluxDB or a database, with timeouts of several seconds) are read with no
    # transaction open, so no lock (on SQLite: the whole database's) is held while waiting on the network.
    if m.feed_type == "http" and (t - minute).total_seconds() > 90:
        # a minute being caught up: a URL only gives what the machine reads now, which isn't that minute's reading
        s, feed = None, {"type": "http", "ok": True, "err": "", "ppm": None, "t": ms(minute), "missed": True}
    else:
        s, feed = sample_for(m, ls, minute)
    if (m.error_src or {}).get("type"):
        from . import machineerrors
        try:   # a broken alarm log never stops this minute from counting
            machineerrors.collect(m, ls, s, minute)
        except Exception as e:  # noqa: BLE001
            log.warning("machine errors for %s: %s", m.name, e)
        if s is not None and machineerrors.is_error(m, s.get("error")):
            feed["error"] = machinedata_text(s.get("error"), s.get("errorText"))
    with transaction.atomic():
        cur = LineState.objects.select_for_update().get(pk=ls.pk)
        if cur.next_tick != minute:
            return True        # done meanwhile by another pass: nothing to count twice
        if owner and not PlantSettings.objects.filter(pk=1, ticker_owner=owner).update(ticker_seen=now()):
            raise LeaseLost(owner)   # fenced: another ticker holds the lease now
        cur.feed, cur.demo_idx = feed, ls.demo_idx
        cur.save(update_fields=["feed", "demo_idx"])
        _sensors_from_feed(m, s, minute)
        engine.tick(m, s, minute)
        if s:
            from maintenance.cmms import meters_from_sample
            meters_from_sample(m, s, minute)   # meters read from the machine's data (Maintenance › Assets)
        LineState.objects.filter(pk=cur.pk).update(next_tick=minute + timedelta(minutes=1))
        return True


def _skip_minute(m, t, owner=None):
    """A minute that could not be processed is skipped and flagged on that machine's screen (only while this ticker
    still holds the lease, like a counted minute)."""
    with transaction.atomic():
        if owner and not PlantSettings.objects.filter(pk=1, ticker_owner=owner).exists():
            raise LeaseLost(owner)
        ls, _ = LineState.objects.select_for_update().get_or_create(machine=m)
        ls.next_tick = (ls.next_tick or t.replace(second=0, microsecond=0)) + timedelta(minutes=1)
        ls.feed = {"type": m.feed_type, "ok": False, "err": "The server could not process this machine's last reading", "ppm": None, "t": ms(t)}
        ls.save(update_fields=["next_tick", "feed"])


def tick_machines(t=None, renew=None, owner=None):
    """Advance every running pleater to the current minute. Each machine and each minute is handled on its
    own, so a bad reading or a failing feed on one machine never stops counting on the others.
    Returns False when the ticker lost its lease during the pass (then nothing else of this pass may run)."""
    t = t or now()
    any_tick = False
    held = True
    for m in Machine.objects.filter(stage="s1", active=True):
        if renew and not renew():  # keep the single-ticker lock alive through a slow pass; stop if it was lost
            log.warning("ticker lock lost; stopping this pass")
            held = False
            break
        for _ in range(10):  # at most 10 catch-up minutes per pass
            try:
                if not _tick_one_minute(m, t, owner):
                    break
                any_tick = True
            except LeaseLost:
                log.warning("ticker lease taken over while reading %s; stopping this pass", m.name)
                return False
            except Exception:
                log.exception("ticker failed for %s", m.name)
                try:
                    _skip_minute(m, t, owner)
                    any_tick = True
                except LeaseLost:
                    return False
                except Exception:
                    log.exception("could not record ticker failure for %s", m.name)
                    break
    if any_tick:
        PlantSettings.bump()
    return held


LEASE_SECONDS = 30


def claim_lease(owner, t=None):
    """Only one ticker may run against a database. A second `runplant` (or `runserver` while `runplant`
    is running) waits instead of counting every minute twice."""
    t = t or now()
    PlantSettings.load()
    stale = t - timedelta(seconds=LEASE_SECONDS)
    with transaction.atomic():
        n = (PlantSettings.objects.filter(pk=1)
             .filter(models.Q(ticker_owner=owner) | models.Q(ticker_owner="") | models.Q(ticker_seen__isnull=True) | models.Q(ticker_seen__lt=stale))
             .update(ticker_owner=owner, ticker_seen=t))
    return n == 1


def timed_pulls(state, t):
    st = PlantSettings.load()
    s = st.settings
    ctx = engine.Ctx(t=t)

    def due(key, minutes):
        if not minutes:
            return False
        last = state.get(key)
        if last and (t - last).total_seconds() < minutes * 60:
            return False
        state[key] = t
        return True

    def run(name, fn):
        try:
            with transaction.atomic():
                fn()
        except Exception as e:  # one failing source never stops the others
            log.warning("%s pull failed: %s", name, e)
            try:
                integrations.set_conn(name, False, str(e))
            except Exception:
                pass

    if engine.s2k_on(s) and due("s2k", s.get("s2kEvery")):
        def s2k():
            integrations.set_conn("s2k", True, f"automatic pull · {engine.import_mos(ctx, engine.s2k_rows())} new")
        run("s2k", s2k)
    if s.get("peopleUrl") and due("people", s.get("peopleEvery")):
        run("people", lambda: integrations.set_conn("people", True, engine.apply_people(ctx, integrations.get_json(s["peopleUrl"]))))
    if integrations.paycom_ready() and s["paycom"].get("auto") and due("paycom", s["paycom"].get("every", 60)):
        run("paycom", lambda: engine.apply_paycom(ctx, integrations.paycom_employees()))
    # Incoming data sources that the server collects from (machine vision QC and others)
    from . import inbound
    from .models import DataSource
    for src in DataSource.objects.filter(enabled=True, mode="poll").exclude(url=""):
        if src.next_poll and src.next_poll > t:
            continue
        DataSource.objects.filter(pk=src.pk).update(next_poll=t + timedelta(seconds=max(10, src.every or 60)))
        try:
            inbound.poll(src)
        except Exception as e:  # one source failing never stops the others
            log.warning("data source %s: %s", src.name, e)
            DataSource.objects.filter(pk=src.pk).update(last_at=t, last_ok=False, last_msg=str(e)[:300])
    # Pictures on tickets closed long ago (Admin > Rules > photoDays), once a day
    if due("photos", 24 * 60):
        def purge_photos():
            from . import photos
            n = photos.purge(t, engine.rules().get("photoDays", 365))
            if n:
                log.info("removed %s pictures of old closed maintenance tickets", n)
        run("photos", purge_photos)
    # Roll tag pictures never saved with a roll (the operator took one but didn't add the roll), once a day
    if due("rollphotos", 24 * 60):
        def purge_roll_photos():
            from .models import RollTagPhoto
            RollTagPhoto.objects.filter(roll__isnull=True, kind="", t__lt=t - timedelta(days=1)).delete()
        run("rollphotos", purge_roll_photos)
    # Scheduled database backups and their off-server copy (Admin › Backup), looked at every 10 minutes
    if due("dbbackup", 10):
        try:
            from plant import offsite
            r = offsite.tick(state, t)
            if r:
                log.info("scheduled backup: %s", r)
        except Exception as e:  # noqa: BLE001 - shows on Admin › Health; never stops the ticker
            log.warning("scheduled backup failed: %s", e)
    # Planned maintenance (Maintenance › PM schedule): open a request for each task that has fallen due
    if due("pm", 5):
        def pm_check():
            from . import pm
            for x in pm.check(t):
                log.info("planned maintenance due: %s · %s", x.machine, x.type)
        run("pm", pm_check)
    # Updates: files dropped in updates/inbox, and an update that is due (Admin > Updates)
    if not state.get("updates") or (t - state["updates"]).total_seconds() >= 15:
        state["updates"] = t
        from plant import updates
        updates.tick(t)
    # Updates from GitHub (Admin > Updates): when switched on, look for a newer release once a day
    if due("github", 60):
        def github_check():
            from plant import ghupdates
            ghupdates.daily(t)
        run("github", github_check)
    # Copy the suite's records to InfluxDB (Admin > Connections > InfluxDB), every influxEvery minutes (1 by default)
    if not state.get("influx") or (t - state["influx"]).total_seconds() >= 60 * max(1, float(s.get("influxEvery") or 1)):
        state["influx"] = t
        try:
            from . import influx
            if influx.out_ready():
                influx.push(t)
        except Exception as e:  # InfluxDB being down never stops counting; it catches up later
            log.warning("InfluxDB copy failed: %s", e)
    # Sensors & custom alarms (Admin > Sensors & alarms): read what's due, check the rules, every pass
    try:
        from sensors import engine as sensors
        sensors.tick(t)
    except Exception:
        log.exception("sensors check failed")
    # Downtime alerts by email / text (Admin > Alerts), checked every 30 seconds
    if not state.get("alerts") or (t - state["alerts"]).total_seconds() >= 30:
        state["alerts"] = t
        try:
            from . import alerts
            alerts.check(t)
        except Exception:
            log.exception("downtime alerts check failed")
    if s.get("webhook"):
        batch = list(Event.objects.filter(sent=False).order_by("id")[:200])
        if batch:
            ids = [e.id for e in batch]
            Event.objects.filter(id__in=ids).update(attempts=models.F("attempts") + 1, last_try=t)
            try:
                # each event carries its own id (the same on every try): if the answer was lost after the receiver
                # stored them, the next try sends the same ids and the receiver can skip what it already has
                integrations.post_webhook(s["webhook"], [{"id": str(e.uid or e.id), "kind": e.kind, "t": ms(e.t), **e.data} for e in batch])
                Event.objects.filter(id__in=ids).update(sent=True, last_error="")
                Event.objects.filter(sent=True, t__lt=t - timedelta(days=7)).delete()
            except integrations.ConnError as e:
                log.warning("webhook failed: %s", e)
                Event.objects.filter(id__in=ids).update(last_error=str(e)[:300])


def loop(stop=None, interval=5):
    state = {}
    owner = f"{socket.gethostname()}:{os.getpid()}"
    waiting = False
    log.info("plant ticker starting as %s", owner)
    while not (stop and stop.is_set()):
        close_old_connections()
        try:
            t = now()
            if claim_lease(owner, t):
                if waiting:
                    log.info("plant ticker took over")
                waiting = False
                if not tick_machines(t, renew=lambda: claim_lease(owner), owner=owner):
                    waiting = True   # another ticker has the lease: its pulls, backups and video, not this one's
                else:
                    timed_pulls(state, t)
                    try:
                        from cameras import live
                        live.keep()          # live camera video (go2rtc) while an RTSP camera is set to it
                    except Exception:  # noqa: BLE001 - never let video stop the plant clock
                        log.exception("live video")
            elif not waiting:
                waiting = True
                log.warning("another plant ticker is already running; this one is standing by")
        except Exception:  # keep ticking; one bad minute should not stop the plant clock
            log.exception("ticker error")
        time.sleep(interval)


_started = False


def start_background():
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=loop, name="plant-ticker", daemon=True).start()
