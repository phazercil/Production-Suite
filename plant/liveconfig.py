"""Connections that can be added or changed after installation (Operator Kiosk > Admin > Connections):
the read-only asset and S2K databases, the Paycom login and the machine feed key.

They are kept in the settings file (.env), which only administrators and the system account can read, so
passwords never go into the database. A change is picked up by every running part of the suite (the web
app's threads and the ticker) the next time it uses that connection; no restart is needed.
"""
import os
import re
import secrets
import threading
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.db import connections

from .dbconf import read_only_mysql
from .envfile import read_env, write_env

DB_GROUPS = {"assets": ("ASSET_DB", "The asset database"), "s2k": ("S2K_DB", "The S2K database"),
             "machines": ("MACHINE_DB", "The machine data database")}
SUITE_KEYS = ("PAYCOM_SID", "PAYCOM_TOKEN", "FEED_KEY", "ALERT_SMTP_HOST", "ALERT_SMTP_PORT", "ALERT_SMTP_SECURITY", "ALERT_SMTP_USER",
              "ALERT_SMTP_PASSWORD", "ALERT_FROM", "ALERT_SMTP_INTERNET", "TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM", "GRAFANA_TOKEN",
              "INFLUX_URL", "INFLUX_VERSION", "INFLUX_ORG", "INFLUX_TOKEN", "INFLUX_USER", "INFLUX_DB", "INFLUX_WRITE_BUCKET")
_lock = threading.Lock()
_seen = {"mtime": None}


def env_path():
    return Path(settings.BASE_DIR) / ".env"


def _mtime():
    try:
        return os.stat(env_path()).st_mtime_ns
    except OSError:
        return None


def apply(values=None):
    """Use the connection settings in .env (or `values`) in this process."""
    values = read_env(env_path()) if values is None else values
    get = lambda k, d="": values.get(k, d)  # noqa: E731
    for alias, (prefix, _) in DB_GROUPS.items():
        cfg = read_only_mysql(get, prefix)
        if cfg:
            # fill in Django's defaults the same way it does at start-up
            full = connections.configure_settings({"default": dict(connections.settings["default"]), alias: cfg})
            connections.settings[alias] = full[alias]
        else:
            connections.settings.pop(alias, None)
    for k in SUITE_KEYS:
        settings.SUITE[k] = values.get(k, "")


def refresh():
    """Pick up a change made by another process (cheap: one file-time check)."""
    m = _mtime()
    if m != _seen["mtime"]:
        with _lock:
            if m != _seen["mtime"]:
                if _seen["mtime"] is not None or m is not None:
                    apply()
                _seen["mtime"] = m


def connection(alias):
    """This thread's connection for alias, reopened if its settings changed since it was opened."""
    refresh()
    conn = connections[alias]
    want = connections.settings.get(alias) or {}
    if any(conn.settings_dict.get(k) != want.get(k) for k in ("HOST", "PORT", "NAME", "USER", "PASSWORD")):
        conn.close()
        del connections[alias]
        conn = connections[alias]
    return conn


def info():
    """What the Admin page shows. Never includes passwords or keys."""
    refresh()
    v = read_env(env_path())
    out = {}
    for group, (prefix, what) in DB_GROUPS.items():
        out[group] = {"on": bool(v.get(f"{prefix}_HOST")), "host": v.get(f"{prefix}_HOST", ""), "port": v.get(f"{prefix}_PORT", "3306") or "3306",
                      "name": v.get(f"{prefix}_NAME", ""), "user": v.get(f"{prefix}_USER", ""), "hasPassword": bool(v.get(f"{prefix}_PASSWORD"))}
    out["paycom"] = {"on": bool(v.get("PAYCOM_SID") and v.get("PAYCOM_TOKEN")), "sid": v.get("PAYCOM_SID", ""), "hasToken": bool(v.get("PAYCOM_TOKEN"))}
    out["feed"] = {"on": bool(v.get("FEED_KEY"))}
    out["smtp"] = {"on": bool(v.get("ALERT_SMTP_HOST")), "host": v.get("ALERT_SMTP_HOST", ""), "port": v.get("ALERT_SMTP_PORT", "") or "25",
                   "security": v.get("ALERT_SMTP_SECURITY", "") or "starttls", "user": v.get("ALERT_SMTP_USER", ""),
                   "hasPassword": bool(v.get("ALERT_SMTP_PASSWORD")), "from": v.get("ALERT_FROM", ""), "internet": v.get("ALERT_SMTP_INTERNET") == "1"}
    out["sms"] = {"on": bool(v.get("TWILIO_SID") and v.get("TWILIO_TOKEN") and v.get("TWILIO_FROM")), "sid": v.get("TWILIO_SID", ""),
                  "hasToken": bool(v.get("TWILIO_TOKEN")), "from": v.get("TWILIO_FROM", "")}
    out["grafana"] = {"hasToken": bool(v.get("GRAFANA_TOKEN"))}
    out["influx"] = {"on": bool(v.get("INFLUX_URL")), "url": v.get("INFLUX_URL", ""), "version": v.get("INFLUX_VERSION", "") or "2",
                     "org": v.get("INFLUX_ORG", ""), "user": v.get("INFLUX_USER", ""), "db": v.get("INFLUX_DB", ""),
                     "out": v.get("INFLUX_WRITE_BUCKET", ""), "hasToken": bool(v.get("INFLUX_TOKEN"))}
    db = connections.settings.get("default", {})
    out["records"] = {"engine": "MariaDB" if "mysql" in db.get("ENGINE", "") else "SQLite (trial)", "host": db.get("HOST", ""),
                      "name": str(db.get("NAME", "")), "user": db.get("USER", "")}
    out["writable"] = os.access(env_path(), os.W_OK) or (not env_path().exists() and os.access(env_path().parent, os.W_OK))
    return out


def _backup():
    p = env_path()
    if p.exists():
        b = p.with_name(f".env.backup-{datetime.now():%Y%m%d-%H%M%S}")
        b.write_bytes(p.read_bytes())
        olds = sorted(p.parent.glob(".env.backup-*"))
        for x in olds[:-15]:  # keep the last 15
            try:
                x.unlink()
            except OSError:
                pass


def save(changes):
    """Write changes ({KEY: value, or None to remove}) to .env and use them right away."""
    with _lock:
        v = read_env(env_path())
        for k, val in changes.items():
            if val is None or val == "":
                v.pop(k, None)
            else:
                v[k] = str(val)
        _backup()
        write_env(env_path(), v)
        apply(v)
        _seen["mtime"] = _mtime()


def new_feed_key():
    return secrets.token_urlsafe(24)


TEXT_RE = re.compile(r"^[^\r\n\"]{1,200}$")
