"""Runs the installed suite. Started by run-web.cmd / run-ticker.cmd (Windows scheduled tasks).

    python serve.py web      the web app on WEB_PORT (default 8000), served by waitress
    python serve.py ticker   the minute ticker (machine readings, timed S2K / Paycom pulls)
    python serve.py control  the control service for Production Suite Admin and the tray icon (127.0.0.1 only)

Logs go to the logs folder next to this file (rotated, 5 x 5 MB per process).
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main(argv):
    mode = argv[1] if len(argv) > 1 else ""
    if mode not in ("web", "ticker", "control"):
        print(__doc__)
        return 2
    sys.path.insert(0, str(HERE))
    os.chdir(HERE)
    if mode == "control":  # no Django: it must keep working while the suite or its database is down
        import logging
        from logging.handlers import RotatingFileHandler
        (HERE / "logs").mkdir(exist_ok=True)
        h = RotatingFileHandler(HERE / "logs" / "control.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger("plant.control").addHandler(h)
        logging.getLogger("plant.control").setLevel(logging.INFO)
        from plant import control
        control.serve(HERE)
        return 0
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "suite.settings")
    os.environ.setdefault("LOG_DIR", str(HERE / "logs"))
    os.environ["SUITE_LOG_NAME"] = mode
    os.environ["TICKER_IN_PROCESS"] = "0"  # the ticker always runs as its own process here
    import django
    django.setup()
    import logging
    log = logging.getLogger("plant")
    from django.conf import settings
    from plant import netsafety
    from plant.netsafety import NotPrivate, check_host
    # Plant network lock: refuse to start if the records database is outside the plant network.
    # (Checked against the installer's setting, because the saved setting lives in that database.)
    db = settings.DATABASES["default"]
    if db["ENGINE"].endswith("mysql"):
        try:
            check_host(db["HOST"], "The MariaDB server", netsafety.installer_networks())
        except NotPrivate as e:
            log.error("Not starting: %s Fix DB_HOST in .env.", e)
            return 3
    if mode == "ticker":
        from django.core.management import call_command
        log.info("Production Suite ticker starting")
        # Keep Windows Firewall in step with the plant network setting (also fixes it after a restore).
        try:
            ok, msg = netsafety.sync_and_record()
            log.info("Plant network %s. %s", ", ".join(map(str, netsafety.current())), msg)
        except Exception:
            log.exception("Couldn't check the firewall rule at start-up")
        call_command("runplant")
        return 0
    from waitress import serve
    from django.core.wsgi import get_wsgi_application
    port = int(os.environ.get("WEB_PORT", "8000"))
    # Listen on IPv4 only (0.0.0.0 = this server's own addresses). Requests from outside the private
    # network are refused by the app and blocked by Windows Firewall.
    host = os.environ.get("WEB_HOST", "0.0.0.0")
    if host not in ("0.0.0.0", "127.0.0.1"):
        if not netsafety.is_private_space(host):
            log.error("Not starting: WEB_HOST %s is not a private-network address.", host)
            return 3
    log.info("Production Suite web app starting on port %s", port)
    serve(get_wsgi_application(), host=host, port=port, trusted_proxy=None, threads=int(os.environ.get("WEB_THREADS", "12")),
          ident="production-suite", channel_timeout=120)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
