"""Back end of the Production Suite Admin desktop program (desktop/SuiteAdmin.ps1) on the server.

The desktop program runs this with the suite's Python and reads one JSON line back (@@RESULT {...}).
Connection changes go through the same commands and checks as Operator Kiosk > Admin, recorded in the
audit log as "Server desktop (<Windows user>)". Only administrators can run it: the settings file and the
install folder are readable by administrators and the system account only.

    manage.py suiteadmin info
    manage.py suiteadmin cmd connSave --params p.json      any Admin > Connections / Alerts / Network command
    manage.py suiteadmin records-save --params p.json      records database (after install_plant --check db)
    manage.py suiteadmin records-undo                      put the previous records database settings back
    manage.py suiteadmin server-save --params p.json       port, time zone, station key
"""
import getpass
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from django.conf import settings
from django.core.management.base import BaseCommand

ALLOWED = {"connInfo", "connTest", "connSave", "connRemove", "alertInfo", "alertConfigSave", "networkSet", "networkFirewallSync",
           "testConn", "updInfo"}
RECORDS_KEYS = ("DB_ENGINE", "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")


def result(cmd, data):
    cmd.stdout.write("@@RESULT " + json.dumps(data, default=str))


class Command(BaseCommand):
    help = "Back end of the Production Suite Admin desktop program."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["info", "cmd", "records-save", "records-undo", "server-save", "pin-check"])
        parser.add_argument("name", nargs="?", default="")
        parser.add_argument("--params", help="JSON file with the fields")

    def handle(self, *args, **o):
        p = {}
        if o.get("params"):
            p = json.loads(Path(o["params"]).read_text(encoding="utf-8-sig") or "{}")
            try:
                Path(o["params"]).unlink()  # it can hold passwords: don't leave it behind
            except OSError:
                pass
        self.as_name = str(p.pop("_as", "") or "")[:80] if isinstance(p, dict) else ""
        try:
            out = getattr(self, "do_" + o["action"].replace("-", "_"))(o.get("name"), p)
        except Exception as e:  # always answer the window with something it can show
            out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        result(self, out)

    # ------------------------------------------------------------------ helpers
    def ctx(self):
        from hmi import engine
        if self.as_name:  # a shift lead who entered their PIN in Production Suite Admin
            who = f"{self.as_name} (server desktop)"
        else:
            try:
                who = f"Server desktop ({getpass.getuser()})"
            except Exception:
                who = "Server desktop"
        return engine.Ctx(user=SimpleNamespace(name=who[:80], role="admin", id=None, pk=None), client_ip="127.0.0.1")

    def env(self):
        from plant import liveconfig
        from plant.envfile import read_env
        return read_env(liveconfig.env_path())

    def backup_env(self, tag):
        from plant import liveconfig
        p = liveconfig.env_path()
        if p.exists():
            shutil.copyfile(p, p.with_name(f".env.backup-{tag}-{datetime.now():%Y%m%d-%H%M%S}"))

    # ------------------------------------------------------------------ actions
    def do_info(self, name, p):
        from plant import liveconfig, netsafety
        v = self.env()
        info = liveconfig.info()
        try:
            nets = [str(n) for n in netsafety.current()]
        except Exception:
            nets = [x.strip() for x in (v.get("PLANT_NETWORKS") or "192.168.100.0/24").split(",") if x.strip()]
        return {"ok": True, "version": settings.SUITE["VERSION"], "revision": settings.SUITE["REVISION"], "conn": info,
                "network": nets,
                "server": {"port": v.get("WEB_PORT", "8000") or "8000", "tz": v.get("PLANT_TZ", "") or settings.TIME_ZONE,
                           "stationKey": v.get("STATION_KEY", ""), "debug": v.get("DJANGO_DEBUG", "0") == "1"},
                "records": {"host": v.get("DB_HOST", ""), "port": v.get("DB_PORT", "3306") or "3306", "name": v.get("DB_NAME", ""),
                            "user": v.get("DB_USER", ""), "hasPassword": bool(v.get("DB_PASSWORD")),
                            "engine": "MariaDB" if (v.get("DB_ENGINE") or "") in ("mariadb", "mysql") else "SQLite (trial)"},
                "folder": str(Path(settings.BASE_DIR))}

    def do_cmd(self, name, p):
        from hmi import engine
        if name not in ALLOWED:
            return {"ok": False, "error": f"Not available from the desktop: {name}"}
        r = engine.execute(name, p, self.ctx())
        r.pop("state", None)
        return r

    def do_pin_check(self, name, p):
        """Is this an active Admin's PIN? Used by the control service before Stop or any change."""
        from plant.models import Person
        pin = str(p.get("pin") or "").strip()
        if not pin:
            return {"ok": False, "error": "Type an Admin PIN."}
        m = [u for u in Person.objects.filter(active=True, role="admin", pin=pin) if u.pin == pin]
        if not m:
            return {"ok": False, "error": "That is not an Admin PIN."}
        return {"ok": True, "name": m[0].name}

    def do_records_save(self, name, p):
        """Write the records database settings (already tested by install_plant --check db). The window then
        runs migrate in a new process and restarts the suite; records-undo puts the old settings back."""
        from plant import liveconfig
        v = self.env()
        f = {k: str(p.get(k) or "").strip() for k in ("host", "port", "name", "user")}
        pw = str(p.get("password") or "") or v.get("DB_PASSWORD", "")
        if not (f["host"] and f["name"] and f["user"]):
            return {"ok": False, "error": "Fill in the server, database and user."}
        if not f["port"].isdigit():
            return {"ok": False, "error": "Use a port number like 3306."}
        if any(c in pw for c in '\r\n"'):
            return {"ok": False, "error": "That password has a character the settings file can't hold (a line break or a double quote)."}
        undo = {k: v.get(k) for k in RECORDS_KEYS}
        (Path(settings.BASE_DIR) / "logs").mkdir(exist_ok=True)
        (Path(settings.BASE_DIR) / "logs" / "records-db-undo.json").write_text(json.dumps(undo), encoding="utf-8")
        self.backup_env("records")
        liveconfig.save({"DB_ENGINE": "mariadb", "DB_HOST": f["host"], "DB_PORT": f["port"], "DB_NAME": f["name"],
                         "DB_USER": f["user"], "DB_PASSWORD": pw})
        return {"ok": True, "msg": f"Records database set to {f['name']} on {f['host']}:{f['port']} as {f['user']}."}

    def do_records_undo(self, name, p):
        from plant import liveconfig
        path = Path(settings.BASE_DIR) / "logs" / "records-db-undo.json"
        try:
            undo = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"ok": False, "error": "There are no earlier records database settings to put back."}
        liveconfig.save({k: undo.get(k) for k in RECORDS_KEYS})
        path.unlink()
        return {"ok": True, "msg": "The previous records database settings are back."}

    def do_server_save(self, name, p):
        from zoneinfo import ZoneInfo

        from plant import liveconfig
        v = self.env()
        port = str(p.get("port") or "").strip()
        tz = str(p.get("tz") or "").strip()
        key = str(p.get("stationKey") or "").strip()
        if not port.isdigit() or not 0 < int(port) < 65536:
            return {"ok": False, "error": "Use a port number like 8000."}
        try:
            ZoneInfo(tz)
        except Exception:
            return {"ok": False, "error": "Use a time zone name like America/New_York or America/Chicago."}
        if not re.match(r"^[A-Za-z0-9_-]{0,64}$", key):
            return {"ok": False, "error": "The station key can only use letters, numbers, - and _."}
        old_port = v.get("WEB_PORT", "8000") or "8000"
        if port != old_port:
            from plant.management.commands.install_plant import port_free
            if not port_free(int(port)):
                return {"ok": False, "error": f"Something else on this server already uses port {port}. Pick another (for example 8080)."}
        self.backup_env("server")
        liveconfig.save({"WEB_PORT": port, "PLANT_TZ": tz, "STATION_KEY": key or None})
        restart = port != old_port or tz != (v.get("PLANT_TZ") or "") or key != (v.get("STATION_KEY") or "")
        return {"ok": True, "restart": restart, "portChanged": port != old_port, "oldPort": old_port,
                "msg": "Saved." + (" Restart the suite to use the new settings." if restart else "")}
