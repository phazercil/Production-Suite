"""The settings file (.env): read and written by setup and by Operator Kiosk > Admin > Connections."""
import os

ENV_KEYS_ORDER = [
    ("# --- Written by the Production Suite installer", None),
    ("DJANGO_SECRET_KEY", None), ("DJANGO_DEBUG", None), ("ALLOWED_HOSTS", None), ("PLANT_TZ", None),
    ("WEB_PORT", None), ("WEB_HOST", None), ("BEHIND_PROXY", None), ("PUBLIC_PORT", None), ("STATIC_ROOT", None), ("PLANT_NETWORKS", None), ("TICKER_IN_PROCESS", None), ("SCHEDULER_REQUIRE_LEAD", None), ("STATION_KEY", None),
    ("# --- Records database (MariaDB)", None),
    ("DB_ENGINE", None), ("DB_HOST", None), ("DB_PORT", None), ("DB_NAME", None), ("DB_USER", None), ("DB_PASSWORD", None),
    ("# --- Asset database with changeover instructions (read-only account, optional)", None),
    ("ASSET_DB_HOST", None), ("ASSET_DB_PORT", None), ("ASSET_DB_NAME", None), ("ASSET_DB_USER", None), ("ASSET_DB_PASSWORD", None), ("ASSET_DB_INTERNET", None),
    ("# --- S2K database (read-only: the suite never writes to it, optional)", None),
    ("S2K_DB_HOST", None), ("S2K_DB_PORT", None), ("S2K_DB_NAME", None), ("S2K_DB_USER", None), ("S2K_DB_PASSWORD", None), ("S2K_DB_INTERNET", None),
    ("# --- Machine data database: tables the machines (PLCs, loggers) write readings to (read-only, optional)", None),
    ("MACHINE_DB_HOST", None), ("MACHINE_DB_PORT", None), ("MACHINE_DB_NAME", None), ("MACHINE_DB_USER", None), ("MACHINE_DB_PASSWORD", None), ("MACHINE_DB_INTERNET", None),
    ("# --- Paycom employee directory (optional)", None),
    ("PAYCOM_SID", None), ("PAYCOM_TOKEN", None),
    ("# --- Machines that push readings send this in the X-Feed-Key header (optional)", None),
    ("FEED_KEY", None),
    ("# --- Downtime alerts: mail server, and Twilio for text messages (optional)", None),
    ("ALERT_SMTP_HOST", None), ("ALERT_SMTP_PORT", None), ("ALERT_SMTP_SECURITY", None), ("ALERT_SMTP_USER", None),
    ("ALERT_SMTP_PASSWORD", None), ("ALERT_FROM", None), ("ALERT_SMTP_INTERNET", None),
    ("TWILIO_SID", None), ("TWILIO_TOKEN", None), ("TWILIO_FROM", None),
    ("# --- Grafana service account token, for Reports (optional)", None),
    ("GRAFANA_TOKEN", None),
    ("# --- InfluxDB: machine data and/or a copy of the suite's records (optional)", None),
    ("INFLUX_URL", None), ("INFLUX_VERSION", None), ("INFLUX_ORG", None), ("INFLUX_TOKEN", None), ("INFLUX_USER", None),
    ("INFLUX_DB", None), ("INFLUX_WRITE_BUCKET", None),
]

ENV_FOOTER = """
# --- Optional, add when ready (see README.md) ---------------------------------
# Asset and S2K databases, Paycom and the machine feed key can also be set in Operator Kiosk > Admin > Connections.
"""

def env_quote(v):
    """The settings reader strips one layer of matching quotes, so wrapping every value is always safe."""
    return '"' + str(v) + '"'


def read_env(path):
    out = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            out[k] = v
    return out


def write_env(path, values):
    lines = []
    written = set()
    for key, _ in ENV_KEYS_ORDER:
        if key.startswith("#"):
            lines += ["", key]
            continue
        if key in values:
            lines.append(f"{key}={env_quote(values[key])}")
            written.add(key)
    extra = [k for k in values if k not in written]
    if extra:
        lines += ["", "# --- Kept from the previous settings"]
        lines += [f"{k}={env_quote(values[k])}" for k in extra]
    text = "\n".join(lines).lstrip("\n") + "\n" + ENV_FOOTER
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")  # no BOM
    os.replace(tmp, path)


