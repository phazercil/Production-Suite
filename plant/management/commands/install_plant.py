"""Interactive setup used by the Windows installer (and usable on any server):

    python manage.py install_plant

1. Asks for the MariaDB server, tests the connection and the permissions it needs, and offers to create
   the database if it doesn't exist yet. Every problem is explained in plain words and asked again.
2. Writes the settings file (.env) with a new secret key. An existing .env is kept unless you choose to
   change it, and a backup copy is always made first.
3. Builds or upgrades the tables (existing data is kept), prepares the web files, loads the cost sheet,
   and creates the first shift lead login if there is none.

Re-running it on an installed server is safe: it is also how an upgrade finishes.
"""
import getpass
import json
import os
import re
import secrets
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from plant.dbcheck import ERRORS  # noqa: E402,F401  (kept for older imports)
from plant.envfile import ENV_FOOTER, ENV_KEYS_ORDER, env_quote, read_env, write_env  # noqa: E402,F401

S2K_KEYS = ("S2K_DB_HOST", "S2K_DB_PORT", "S2K_DB_NAME", "S2K_DB_USER", "S2K_DB_PASSWORD")
ASSET_KEYS = ("ASSET_DB_HOST", "ASSET_DB_PORT", "ASSET_DB_NAME", "ASSET_DB_USER", "ASSET_DB_PASSWORD")


def mariadb_version_ok(version):
    """Django 5.2 needs MariaDB 10.5+ or MySQL 8.0.11+."""
    m = re.match(r"(\d+)\.(\d+)\.?(\d+)?", version or "")
    if not m:
        return False, f"Couldn't read the database version ({version})."
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    if "mariadb" in version.lower():
        ok = (major, minor) >= (10, 5)
        return ok, "" if ok else f"This is MariaDB {major}.{minor}. The suite needs MariaDB 10.5 or newer."
    ok = (major, minor, patch) >= (8, 0, 11)
    return ok, "" if ok else f"This is MySQL {major}.{minor}.{patch}. The suite needs MySQL 8.0.11 or newer, or MariaDB 10.5 or newer."


def local_addresses():
    names = {socket.gethostname()}
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:  # the address used to reach the rest of the network
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    ips.discard("127.0.0.1")
    return sorted(names) + sorted(ips)


def pin_problem(v, existing=()):
    """Why a shift lead PIN can't be used, or None."""
    if not re.match(r"^[0-9]{6,12}$", v or ""):
        return "Use 6 to 12 digits. Shift lead PINs approve schedule and admin changes, so make them hard to guess."
    if v.lower() in existing:
        return "Someone already uses that PIN."
    if len(set(v)) == 1 or v in "01234567890123" or v in "98765432109876":
        return "That PIN is too easy to guess."
    return None


def port_free(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


class Command(BaseCommand):
    help = "Set up or upgrade this server: database connection, settings file, tables and first shift lead."

    def add_arguments(self, parser):
        parser.add_argument("--answers", help="JSON file with answers, for unattended installs (keys match the prompts)")
        parser.add_argument("--finish", action="store_true", help="internal: second stage, run after .env is written")
        parser.add_argument("--sqlite", action="store_true", help="Use a local SQLite file instead of MariaDB (trial only)")
        parser.add_argument("--skip-port-check", action="store_true", help="internal: the web app is already running on the port")
        parser.add_argument("--apply-network", action="store_true", help="internal: store the plant network typed in stage 1")
        parser.add_argument("--progress", action="store_true", help="internal: print @@PROGRESS lines for the setup window")
        parser.add_argument("--behind-apache", action="store_true",
                            help="Linux install: the suite listens on 127.0.0.1:8008 and Apache serves the screens (packaging/linux/install.sh)")
        parser.add_argument("--check", choices=["env", "network", "db", "dbadmin", "asset", "s2k", "server", "pin"],
                            help="internal: check one page of answers for the setup window and print @@RESULT {json}")

    # ------------------------------------------------------------------ prompting
    def setup_io(self, opts):
        self.answers = {}
        if opts.get("answers"):
            self.answers = json.loads(Path(opts["answers"]).read_text(encoding="utf-8-sig"))
        self.unattended = bool(opts.get("answers"))

    def ask(self, key, prompt, default="", secret=False, check=None):
        """Ask until the answer passes `check` (a function returning an error message or None)."""
        for _ in range(20):
            if key in self.answers:
                val = str(self.answers[key])
            elif self.unattended:
                val = str(default)
            else:
                shown = f" [{default}]" if default not in ("", None) and not secret else ""
                try:
                    # passwords are kept exactly as typed (spaces included); other answers are trimmed
                    val = getpass.getpass(f"  {prompt}: ") if secret else input(f"  {prompt}{shown}: ").strip()
                except EOFError:
                    raise CommandError("Setup was cancelled.")
                if not val and not secret:
                    val = str(default)
            err = check(val) if check else None
            if not err:
                return val
            self.stdout.write(self.style.WARNING(f"  {err}"))
            self.stdout.flush()
            if self.unattended:
                raise CommandError(f"Answer for {key!r} is not usable: {err}")
        raise CommandError("Too many tries.")

    def yes(self, key, prompt, default=True):
        v = self.ask(key, f"{prompt} ({'Y/n' if default else 'y/N'})", "y" if default else "n",
                     check=lambda x: None if x.lower() in ("y", "yes", "n", "no", "1", "0", "true", "false") else "Answer y or n.")
        return v.lower() in ("y", "yes", "1", "true")

    def say(self, msg="", style=None):
        self.stdout.write(style(msg) if style else msg)
        if style is not None and getattr(self, "show_progress", False) and style == self.style.ERROR:
            self.stdout.write("@@ERROR " + " ".join(str(msg).split()))

    def progress(self, pct, text):
        """A line the setup window turns into its progress bar and status text."""
        if getattr(self, "show_progress", False):
            self.stdout.write(f"@@PROGRESS {int(pct)} {text}")
            self.stdout.flush()

    # ------------------------------------------------------------------ MariaDB
    def driver(self):
        try:
            import MySQLdb  # mysqlclient
            return MySQLdb
        except ImportError:
            raise CommandError("The MariaDB driver (PyMySQL) isn't installed in this Python. Re-run the installer; "
                               "if it keeps happening, the server may not be able to download packages (see README-INSTALL.txt).")

    def explain(self, e, port):
        code = e.args[0] if getattr(e, "args", None) and isinstance(e.args[0], int) else None
        msg = ERRORS.get(code)
        detail = e.args[1] if getattr(e, "args", None) and len(e.args) > 1 else str(e)
        return (msg.format(port=port) if msg else "MariaDB answered with an error.") + f"  ({detail})", code

    def plant_nets(self, cfg):
        from plant import netsafety
        return netsafety.parse_networks(cfg.get("PLANT_NETWORKS") or netsafety.DEFAULT_NETWORKS)

    def ask_network(self, cfg):
        """The plant network: the addresses screens connect from. Stored as PLANT_NETWORKS in .env."""
        from plant import netsafety
        self.say("  Screens, tablets and PCs can only open the suite from these addresses, and the suite only connects to")
        self.say("  servers inside them. Use a range such as 192.168.100.0/24 (192.168.100.0 to .255); separate several with commas.")

        def net_check(v):
            try:
                netsafety.parse_networks(v)
            except netsafety.BadNetwork as e:
                return str(e)
            return None
        for _ in range(5):
            v = self.ask("plant_network", "Plant network", cfg.get("PLANT_NETWORKS") or ", ".join(netsafety.DEFAULT_NETWORKS), check=net_check)
            nets = netsafety.parse_networks(v)
            mine = [a for a in local_addresses() if netsafety._ip(a) is not None and not netsafety.is_loopback(a)]
            inside = [a for a in mine if netsafety.is_allowed_ip(a, nets)]
            if mine and not inside and not self.unattended:
                self.say(self.style.WARNING(f"  This server's own address ({', '.join(mine)}) is not in {', '.join(map(str, nets))}. "
                                            "Screens are usually on the same network as the server."))
                if not self.yes("network_confirm", "Use it anyway", False):
                    continue
            cfg["PLANT_NETWORKS"] = ",".join(map(str, nets))
            self.say("  Allowed: " + "; ".join(netsafety.describe(n) for n in nets) + ". This server itself is always allowed.")
            return
        raise CommandError("Setup stopped: no plant network was chosen. Nothing was changed.")

    def test_database(self, cfg, create_ok=True):
        """Returns None when the database is ready, or a message saying what to fix."""
        from plant.netsafety import NotPrivate, check_host
        try:
            check_host(cfg["DB_HOST"], "The MariaDB server", self.plant_nets(cfg))
        except NotPrivate as e:
            return str(e)
        MySQLdb = self.driver()
        kw = dict(host=cfg["DB_HOST"], port=int(cfg["DB_PORT"]), user=cfg["DB_USER"], password=cfg["DB_PASSWORD"], connect_timeout=8,
                  charset="utf8mb4")
        try:
            conn = MySQLdb.connect(database=cfg["DB_NAME"], **kw)
        except MySQLdb.OperationalError as e:
            msg, code = self.explain(e, cfg["DB_PORT"])
            if code == 1049:
                self.db_missing = True
            if code != 1049 or not create_ok:
                return msg
            self.say(f"  The database \"{cfg['DB_NAME']}\" doesn't exist yet.")
            if not self.yes("create_database", "Create it now", True):
                return "Create the database, then try again."
            try:
                c = MySQLdb.connect(**kw)
                cur = c.cursor()
                cur.execute(f"CREATE DATABASE `{cfg['DB_NAME']}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
                c.close()
                self.say(f"  Created database {cfg['DB_NAME']}.", self.style.SUCCESS)
            except MySQLdb.Error as e2:
                msg2, _ = self.explain(e2, cfg["DB_PORT"])
                return (f"Couldn't create the database: {msg2}\n  Ask whoever manages MariaDB to run:\n"
                        f"    CREATE DATABASE `{cfg['DB_NAME']}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;\n"
                        f"    GRANT ALL PRIVILEGES ON `{cfg['DB_NAME']}`.* TO '{cfg['DB_USER']}'@'%';")
            return self.test_database(cfg, create_ok=False)
        except MySQLdb.Error as e:
            return self.explain(e, cfg["DB_PORT"])[0]
        try:
            cur = conn.cursor()
            cur.execute("SELECT VERSION()")
            version = cur.fetchone()[0]
            ok, why = mariadb_version_ok(version)
            if not ok:
                return why
            # The suite creates and changes its own tables on install and upgrade.
            try:
                cur.execute("CREATE TABLE IF NOT EXISTS `_suite_install_check` (id INT)")
                cur.execute("DROP TABLE `_suite_install_check`")
            except MySQLdb.Error as e:
                return (f"That user can connect but can't create tables ({self.explain(e, cfg['DB_PORT'])[0]}). It needs:\n"
                        f"    GRANT ALL PRIVILEGES ON `{cfg['DB_NAME']}`.* TO '{cfg['DB_USER']}'@'%';")
            self.say(f"  Connected to {version} at {cfg['DB_HOST']}:{cfg['DB_PORT']}.", self.style.SUCCESS)
            return None
        finally:
            conn.close()

    def test_read_only_db(self, cfg, prefix, what):
        """Connect to an outside database and read its table list. Nothing is created or changed there:
        no CREATE DATABASE, no test table, and the session is opened read-only (plant/dbcheck.py)."""
        from plant.dbcheck import test_read_only
        records = (cfg.get("DB_HOST", ""), cfg.get("DB_PORT", ""), cfg.get("DB_NAME", "")) if cfg.get("DB_ENGINE") in ("mariadb", "mysql") else None
        r = test_read_only(cfg.get(f"{prefix}_HOST"), cfg.get(f"{prefix}_PORT"), cfg.get(f"{prefix}_NAME"), cfg.get(f"{prefix}_USER"),
                           cfg.get(f"{prefix}_PASSWORD"), what, self.plant_nets(cfg), records)
        if not r["ok"]:
            return r["msg"]
        self.say(f"  {r['msg']}", self.style.SUCCESS)
        if r.get("writable"):
            self.say(self.style.WARNING("  Note: this account is allowed to change data. The suite never does (it only reads, "
                                        "and opens the connection read-only), but a SELECT-only account is safer."))
        return None

    def test_asset_db(self, cfg):
        return self.test_read_only_db(cfg, "ASSET_DB", "The asset database")

    def test_s2k_db(self, cfg):
        return self.test_read_only_db(cfg, "S2K_DB", "The S2K database")

    def ask_read_only_db(self, cfg, prefix, key, what, default_on):
        keys = tuple(f"{prefix}_{k}" for k in ("HOST", "PORT", "NAME", "USER", "PASSWORD"))
        if not self.yes(f"use_{key}_db", "Connect it now", default_on):
            for k in keys:
                cfg.pop(k, None)
            return
        for _ in range(10):
            cfg[keys[0]] = self.ask(f"{key}_host", "Server name or address", cfg.get(keys[0], cfg.get("DB_HOST", "localhost")),
                                    check=lambda v: None if re.match(r"^[A-Za-z0-9._:-]+$", v) else "Use a server name or IP address.")
            cfg[keys[1]] = self.ask(f"{key}_port", "Port", cfg.get(keys[1], "3306"),
                                    check=lambda v: None if v.isdigit() and 0 < int(v) < 65536 else "Use a port number like 3306.")
            cfg[keys[2]] = self.ask(f"{key}_name", "Database name", cfg.get(keys[2], ""),
                                    check=lambda v: None if re.match(r"^[A-Za-z0-9_$-]{1,64}$", v) else "Type the database name.")
            cfg[keys[3]] = self.ask(f"{key}_user", "Read-only user name", cfg.get(keys[3], ""),
                                    check=lambda v: None if v and len(v) <= 80 and "\n" not in v else "Type the user name.")
            cfg[keys[4]] = self.ask(f"{key}_password", "Password (typing is hidden)", cfg.get(keys[4], ""), secret=True,
                                    check=lambda v: None if "\n" not in v and "\r" not in v else "A password can't contain line breaks.")
            self.say("  Testing the connection (read-only)…")
            problem = self.test_read_only_db(cfg, prefix, what)
            if not problem:
                return
            self.say(f"  {problem}", self.style.ERROR)
            if self.unattended or not self.yes(f"retry_{key}", "Try again (n = skip it for now)", True):
                break
        for k in keys:
            cfg.pop(k, None)
        self.say(f"  Skipped. Add it later in Operator Kiosk > Admin > Connections, or run setup again.")

    # ------------------------------------------------------------------ stage 1: settings
    def handle(self, *args, **opts):
        self.setup_io(opts)
        self.show_progress = bool(opts.get("progress"))
        if opts.get("check"):
            return self.check_page(opts["check"], opts)
        try:
            return self.run_setup(opts)
        except CommandError as e:
            if self.show_progress:
                self.stdout.write("@@ERROR " + " ".join(str(e).split()))
            raise

    def run_setup(self, opts):
        if opts["finish"]:
            return self.finish(opts)
        env_path = Path(settings.BASE_DIR) / ".env"
        old = read_env(env_path)
        self.say("")
        self.say("Production Suite setup", self.style.MIGRATE_HEADING)
        self.say("Press Enter to accept the value in [brackets].")

        if old and old.get("DJANGO_SECRET_KEY"):
            where = f"MariaDB {old.get('DB_NAME')} on {old.get('DB_HOST')}" if old.get("DB_ENGINE") in ("mariadb", "mysql") else "a local SQLite file"
            self.say(f"\nThis server is already set up (records in {where}).")
            if self.yes("keep_settings", "Keep these settings and just upgrade", True):
                self.backup(env_path)
                cfg, changed, apply_network = dict(old), False, False
                if not old.get("PLANT_NETWORKS"):  # set up before the plant network setting existed
                    self.say("\nPlant network", self.style.MIGRATE_HEADING)
                    self.ask_network(cfg)
                    changed = apply_network = True
                if not old.get("S2K_DB_HOST"):  # set up before the S2K database option existed
                    self.say("\nS2K database for open MOs (optional, read-only)", self.style.MIGRATE_HEADING)
                    self.say("  Setup only signs in and reads the table list. It never creates or changes anything in S2K.")
                    self.ask_read_only_db(cfg, "S2K_DB", "s2k", "The S2K database", False)
                    changed = changed or bool(cfg.get("S2K_DB_HOST"))
                if opts.get("behind_apache"):
                    before = dict(cfg)
                    self.apache_settings(cfg, cfg.get("PUBLIC_PORT", "80"))
                    changed = changed or cfg != before
                if changed:
                    write_env(env_path, cfg)
                return self.run_finish(opts, apply_network=apply_network)

        cfg = dict(old)
        cfg.setdefault("DJANGO_SECRET_KEY", secrets.token_urlsafe(50))
        cfg["DJANGO_DEBUG"] = "0"
        cfg["TICKER_IN_PROCESS"] = "0"
        cfg.setdefault("ALLOWED_HOSTS", "*")  # plant network only; kiosks reach the server by name or by address

        # --- plant network
        self.progress(3, "Checking the plant network")
        self.say("\n1. Plant network", self.style.MIGRATE_HEADING)
        self.ask_network(cfg)

        # --- records database
        if opts["sqlite"]:
            cfg["DB_ENGINE"] = "sqlite"
            for k in ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"):
                cfg.pop(k, None)
        else:
            self.progress(6, "Connecting to MariaDB")
            self.say("\n2. MariaDB for the suite's records", self.style.MIGRATE_HEADING)
            cfg["DB_ENGINE"] = "mariadb"
            for _ in range(10):
                cfg["DB_HOST"] = self.ask("db_host", "MariaDB server name or address", cfg.get("DB_HOST", "localhost"),
                                          check=lambda v: None if re.match(r"^[A-Za-z0-9._:-]+$", v) else "Use a server name or IP address.")
                cfg["DB_PORT"] = self.ask("db_port", "Port", cfg.get("DB_PORT", "3306"),
                                          check=lambda v: None if v.isdigit() and 0 < int(v) < 65536 else "Use a port number like 3306.")
                cfg["DB_NAME"] = self.ask("db_name", "Database name", cfg.get("DB_NAME", "production_suite"),
                                          check=lambda v: None if re.match(r"^[A-Za-z0-9_]{1,64}$", v) else "Letters, numbers and _ only.")
                cfg["DB_USER"] = self.ask("db_user", "User name", cfg.get("DB_USER", "suite"),
                                          check=lambda v: None if v and len(v) <= 80 and "\n" not in v else "Type the MariaDB user name.")
                hint = " (Enter keeps the current one)" if old.get("DB_PASSWORD") else ""
                pw = self.ask("db_password", f"Password{hint}, typing is hidden", "", secret=True,
                              check=lambda v: None if "\n" not in v and "\r" not in v else "A password can't contain line breaks.")
                cfg["DB_PASSWORD"] = pw if pw or not old.get("DB_PASSWORD") else old["DB_PASSWORD"]
                self.say("  Testing the connection…")
                problem = self.test_database(cfg)
                if problem and not self.unattended and any(w in problem for w in ("GRANT", "isn't allowed", "refused that user name")):
                    self.say(f"  {problem}", self.style.ERROR)
                    if self.yes("use_db_admin", "Use a MariaDB administrator login (for example root) once to create the database and "
                                "give this user the rights it needs? It is not saved", True):
                        from plant.dbcheck import setup_with_admin
                        au = self.ask("admin_user", "Administrator user", "root")
                        ap = self.ask("admin_password", "Administrator password (typing is hidden)", "", secret=True)
                        r = setup_with_admin(cfg["DB_HOST"], cfg["DB_PORT"], au, ap, cfg["DB_NAME"], cfg["DB_USER"], cfg["DB_PASSWORD"])
                        self.say(f"  {r['msg']}", self.style.SUCCESS if r["ok"] else self.style.ERROR)
                        if r["ok"]:
                            problem = self.test_database(cfg)
                if not problem:
                    break
                self.say(f"  {problem}", self.style.ERROR)
                if self.unattended or not self.yes("retry_db", "Try again", True):
                    raise CommandError("Setup stopped: the database is not ready. Nothing was changed.")
            else:
                raise CommandError("Setup stopped: the database is not ready. Nothing was changed.")

        # --- asset database (optional, read-only)
        self.progress(12, "Checking the asset database")
        self.say("\n3. Asset database with changeover instructions (optional, read-only)", self.style.MIGRATE_HEADING)
        self.ask_read_only_db(cfg, "ASSET_DB", "asset", "The asset database", bool(cfg.get("ASSET_DB_HOST")))

        # --- S2K database (optional, read-only)
        self.progress(16, "Checking the S2K database (read-only)")
        self.say("\n4. S2K database for open MOs (optional, read-only)", self.style.MIGRATE_HEADING)
        self.say("  Setup only signs in and reads the table list. It never creates or changes anything in S2K.")
        self.ask_read_only_db(cfg, "S2K_DB", "s2k", "The S2K database", bool(cfg.get("S2K_DB_HOST")))

        # --- this server
        self.progress(20, "Checking this server's settings")
        self.say("\n5. This server", self.style.MIGRATE_HEADING)
        running_here = opts.get("skip_port_check")

        def port_check(v):
            if not v.isdigit() or not 0 < int(v) < 65536:
                return "Use a port number like 8000."
            if not running_here and not port_free(int(v)):
                return f"Something else on this server already uses port {v}. Pick another (for example 8080)."
            return None
        if opts.get("behind_apache"):
            # Apache listens for the screens; the suite itself only on this server (127.0.0.1:8008)
            pub = self.ask("web_port", "Port the screens connect to (Apache: 80, or 443 once HTTPS is set up)", cfg.get("PUBLIC_PORT", "80"),
                           check=lambda v: None if v.isdigit() and 0 < int(v) < 65536 else "Use a port number like 80.")
            self.apache_settings(cfg, pub)
        else:
            cfg["WEB_PORT"] = self.ask("web_port", "Port the screens connect to", cfg.get("WEB_PORT", "8000"), check=port_check)

        def tz_check(v):
            try:
                from zoneinfo import ZoneInfo
                ZoneInfo(v)
                return None
            except Exception:
                return "Use a time zone name like America/New_York or America/Chicago."
        cfg["PLANT_TZ"] = self.ask("time_zone", "Plant time zone", cfg.get("PLANT_TZ", "America/New_York"), check=tz_check)
        cfg["SCHEDULER_REQUIRE_LEAD"] = "1" if self.yes("scheduler_lead", "Require a shift lead PIN to change the schedule", True) else "0"
        cfg["STATION_KEY"] = self.ask("station_key", "Station key that every screen must enter once (Enter = none)", cfg.get("STATION_KEY", ""),
                                      check=lambda v: None if re.match(r"^[A-Za-z0-9_-]{0,64}$", v) else "Letters, numbers, - and _ only.")
        if not cfg["STATION_KEY"]:
            cfg.pop("STATION_KEY")

        self.backup(env_path)
        write_env(env_path, cfg)
        self.say(f"\nSettings saved to {env_path}", self.style.SUCCESS)
        self.progress(24, "Settings saved")
        return self.run_finish(opts, apply_network=True)

    # ------------------------------------------------------------------ page checks for the setup window
    def check_page(self, kind, opts):
        """Check one page of the setup window's answers. Prints one line: @@RESULT {"ok": .., "msg": .., ...}.
        Nothing is written: no .env, no tables, and outside databases are only read."""
        import io
        a = self.answers
        out = {"ok": True, "msg": "", "detail": ""}
        real_stdout, buf = self.stdout, io.StringIO()
        self.stdout = type(real_stdout)(buf)
        try:
            # A password left blank in the window means "keep the one already saved".
            old = read_env(Path(settings.BASE_DIR) / ".env")
            a = dict(a)
            for key, env_key in (("db_password", "DB_PASSWORD"), ("asset_password", "ASSET_DB_PASSWORD"), ("s2k_password", "S2K_DB_PASSWORD")):
                if not a.get(key) and old.get(env_key):
                    a[key] = old[env_key]
            cfg = {"PLANT_NETWORKS": a.get("plant_network", ""), "DB_ENGINE": "mariadb",
                   "DB_HOST": a.get("db_host", ""), "DB_PORT": str(a.get("db_port", "3306")), "DB_NAME": a.get("db_name", ""),
                   "DB_USER": a.get("db_user", ""), "DB_PASSWORD": a.get("db_password", "")}
            for prefix, key in (("ASSET_DB", "asset"), ("S2K_DB", "s2k")):
                cfg.update({f"{prefix}_HOST": a.get(f"{key}_host", ""), f"{prefix}_PORT": str(a.get(f"{key}_port", "3306")),
                            f"{prefix}_NAME": a.get(f"{key}_name", ""), f"{prefix}_USER": a.get(f"{key}_user", ""),
                            f"{prefix}_PASSWORD": a.get(f"{key}_password", "")})
            if kind == "env":
                out["env"] = {k: v for k, v in old.items() if "PASSWORD" not in k and k != "DJANGO_SECRET_KEY"}
                out["hasPassword"] = {k: bool(v) for k, v in old.items() if "PASSWORD" in k}
                out["addresses"] = local_addresses()
            elif kind == "network":
                from plant import netsafety
                try:
                    nets = netsafety.parse_networks(cfg["PLANT_NETWORKS"])
                except netsafety.BadNetwork as e:
                    out.update(ok=False, msg=str(e))
                else:
                    out["msg"] = "; ".join(netsafety.describe(n) for n in nets) + ". This server itself is always allowed."
                    out["networks"] = ",".join(map(str, nets))
                    mine = [x for x in local_addresses() if netsafety._ip(x) is not None and not netsafety.is_loopback(x)]
                    if mine and not any(netsafety.is_allowed_ip(x, nets) for x in mine):
                        out["warn"] = (f"This server's own address ({', '.join(mine)}) is not in that range. "
                                       "Screens are usually on the same network as the server.")
            elif kind == "db":
                self.answers = dict(a, create_database=a.get("create_database", "n"))
                self.db_missing = False
                problem = self.test_database(cfg)
                out.update(ok=not problem, msg=problem or "Connected.", missing=bool(getattr(self, "db_missing", False)),
                           needsAdmin=bool(problem) and any(w in problem for w in ("GRANT", "isn't allowed", "refused that user name")))
            elif kind == "dbadmin":
                from plant.dbcheck import setup_with_admin
                from plant.netsafety import NotPrivate, check_host
                try:
                    check_host(cfg["DB_HOST"], "The MariaDB server", self.plant_nets(cfg))
                except NotPrivate as e:
                    raise CommandError(str(e))
                r = setup_with_admin(cfg["DB_HOST"], cfg["DB_PORT"], a.get("admin_user", ""), a.get("admin_password", ""),
                                     cfg["DB_NAME"], cfg["DB_USER"], cfg["DB_PASSWORD"])
                out.update(ok=r["ok"], msg=r["msg"])
            elif kind in ("asset", "s2k"):
                prefix, what = ("ASSET_DB", "The asset database") if kind == "asset" else ("S2K_DB", "The S2K database")
                problem = self.test_read_only_db(cfg, prefix, what)
                out.update(ok=not problem, msg=problem or "Connected read-only; nothing was created or changed.")
            elif kind == "server":
                v = str(a.get("web_port", "8000"))
                if not v.isdigit() or not 0 < int(v) < 65536:
                    out.update(ok=False, msg="Use a port number like 8000.")
                elif not opts.get("skip_port_check") and not port_free(int(v)):
                    out.update(ok=False, msg=f"Something else on this server already uses port {v}. Pick another (for example 8080).")
                else:
                    try:
                        from zoneinfo import ZoneInfo
                        ZoneInfo(str(a.get("time_zone", "")))
                    except Exception:
                        out.update(ok=False, msg="Use a time zone name like America/New_York or America/Chicago.")
                    if out["ok"] and not re.match(r"^[A-Za-z0-9_-]{0,64}$", str(a.get("station_key", ""))):
                        out.update(ok=False, msg="The station key can only use letters, numbers, - and _.")
            elif kind == "pin":
                problem = pin_problem(str(a.get("lead_pin", "")))
                if not problem and not (0 < len(str(a.get("lead_name", "")).strip()) <= 120):
                    problem = "Type a name for the shift lead."
                out.update(ok=not problem, msg=problem or "")
        except CommandError as e:
            out.update(ok=False, msg=str(e))
        except Exception as e:  # report anything unexpected to the window instead of a traceback
            out.update(ok=False, msg=f"{type(e).__name__}: {e}")
        finally:
            self.stdout = real_stdout
        out["detail"] = buf.getvalue().strip()[-2000:]
        self.stdout.write("@@RESULT " + json.dumps(out))

    def backup(self, env_path):
        if env_path.exists():
            b = env_path.with_name(f".env.backup-{datetime.now():%Y%m%d-%H%M%S}")
            b.write_bytes(env_path.read_bytes())
            self.say(f"  Previous settings copied to {b.name}")

    @staticmethod
    def apache_settings(cfg, public_port):
        cfg.update({"WEB_HOST": "127.0.0.1", "WEB_PORT": "8008", "BEHIND_PROXY": "1", "PUBLIC_PORT": str(public_port),
                    "STATIC_ROOT": "/var/www/production-suite/static"})

    def run_finish(self, opts, apply_network=False):
        """Run stage 2 in a fresh process so it uses the settings just written."""
        cmd = [sys.executable, str(Path(settings.BASE_DIR) / "manage.py"), "install_plant", "--finish"]
        if apply_network:
            cmd.append("--apply-network")
        if getattr(self, "show_progress", False):
            cmd.append("--progress")
        if opts.get("answers"):
            cmd += ["--answers", opts["answers"]]
        env = {k: v for k, v in os.environ.items() if not k.startswith(("DB_", "ASSET_DB_", "DJANGO_", "WEB_", "PLANT_TZ", "STATION_KEY",
                                                                           "SCHEDULER_REQUIRE", "TICKER_IN_PROCESS", "SQLITE_PATH",
                                                                           "PLANT_NETWORKS", "S2K_DB_"))}
        r = subprocess.run(cmd, cwd=settings.BASE_DIR, env=env)
        if r.returncode:
            raise CommandError("Setup did not finish. The message above says what went wrong; fix it and run setup again. "
                               "Your settings file was saved, so you won't have to type everything again.")

    # ------------------------------------------------------------------ stage 2: tables, files, first login
    def finish(self, opts):
        from django.core.management import call_command
        from django.db import connection

        self.progress(28, "Building the suite's tables in its records database")
        self.say("\n6. Building the suite's own tables in its records database (existing records are kept)", self.style.MIGRATE_HEADING)
        call_command("migrate", database="default", interactive=False, verbosity=1)  # never the asset or S2K database
        self.progress(80, "Preparing the screens")
        self.say("\n7. Preparing the screens", self.style.MIGRATE_HEADING)
        call_command("collectstatic", interactive=False, verbosity=0)
        self.say("  Done.")

        from plant.demo import ensure_basics, seed_demo
        from plant.models import Machine, Person, PlantSettings, Shift
        ensure_basics()

        # The plant network typed in stage 1 becomes the live setting (Admin > Network can change it later).
        from plant import netsafety
        PlantSettings.load()
        saved = (PlantSettings.objects.filter(pk=1).values_list("network", flat=True).first() or {}).get("allowed")
        if opts.get("apply_network") or not saved:
            nets = netsafety.installer_networks()
            netsafety.save(nets, by="installer", sync=False)
            self.say("  Plant network: " + ", ".join(map(str, nets)))
        else:
            self.say("  Plant network (kept from Admin > Network): " + ", ".join(saved))

        self.progress(90, "Shift lead login")
        self.say("\n8. Shift lead login", self.style.MIGRATE_HEADING)
        fresh = not Machine.objects.exists() and not Person.objects.exists()
        demo = False
        if fresh:
            self.say("  The database is empty.")
            demo = self.yes("demo", "Load the demo plant for training (two pleaters, sample orders, PINs 1111 and 9999)", False)
        if demo:
            seed_demo(reset=True)
            self.say("  Demo plant loaded. Demo PINs: J. Ortiz 1111 · M. Lee 2222 · K. Patel 3333 · Shift Lead 9999.", self.style.WARNING)
            self.say("  Before going live, use Operator Kiosk › Admin › Backup › Clear demo data and go live.")
        else:
            if not Shift.objects.exists():
                Shift.objects.create(name="1st shift", start="06:00", end="14:30", days=[1, 2, 3, 4, 5])
            if Person.objects.filter(role="admin", active=True).exclude(pin="").exists():
                self.say("  A shift lead login already exists.")
            else:
                name = self.ask("lead_name", "Name for the first shift lead", "Shift Lead",
                                check=lambda v: None if 0 < len(v) <= 120 else "Type a name.")
                existing = {p.lower() for p in Person.objects.exclude(pin="").values_list("pin", flat=True)}

                def pin_check(v):
                    return pin_problem(v, existing)
                for _ in range(5):
                    pin = self.ask("lead_pin", "Shift lead PIN (6-12 digits, not a run like 123456 or 111111; typing is hidden)", "", secret=True, check=pin_check)
                    again = self.answers.get("lead_pin") if "lead_pin" in self.answers else self.ask("lead_pin_again", "Type the PIN again", "", secret=True)
                    if pin == again:
                        break
                    self.say(self.style.WARNING("  The two PINs didn't match. Try again."))
                else:
                    raise CommandError("The PINs didn't match.")
                p = Person.objects.create(name=name, pin=pin, role="admin")
                p.trained.set(Machine.objects.filter(stage="s1"))
                self.say(f"  {name} can now log in with that PIN.", self.style.SUCCESS)

        port = os.environ.get("PUBLIC_PORT") or os.environ.get("WEB_PORT", "8000")
        self.progress(100, "Setup complete")
        self.say("\nSetup complete.", self.style.SUCCESS)
        self.say(f"  Records: {connection.vendor} database {connection.settings_dict['NAME']}")
        self.say("  Open these from any screen on the plant network:")
        for host in local_addresses()[:4]:
            self.say(f"    http://{host}:{port}/operator/    Operator Kiosk")
        host = local_addresses()[0] if local_addresses() else "localhost"
        self.say(f"    http://{host}:{port}/scheduler/   Scheduler")
        self.say(f"    http://{host}:{port}/data/        Data Kiosk (wall screen)")
        if not Machine.objects.filter(stage="s1").exists():
            self.say("  Next: add your machines and shifts in Scheduler › Lines & Shifts, then set each pleater's machine data "
                     "in Operator Kiosk › Admin.")
