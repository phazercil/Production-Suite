"""The setup command behind the Windows installer. MariaDB is replaced by a stand-in driver that can
answer the way a real server does when something is wrong."""
import io
import json
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from plant.management.commands import install_plant as ip
from plant.models import Person


class FakeError(Exception):
    pass


class FakeOperationalError(FakeError):
    pass


class FakeServer:
    """Behaves like MariaDB: scenario keys switch on failures."""

    def __init__(self, **scenario):
        self.s = scenario
        self.created = []
        self.dbs = set(scenario.get("dbs", ["production_suite"]))
        self.log = []   # (database, init_command, sql) for every statement sent

    def connect(self, host, port, user, password, database=None, **kw):
        passwd, db = password, database
        self.log.append((db, kw.get("init_command"), "<connect>"))
        if self.s.get("unreachable"):
            raise FakeOperationalError(2003, f"Can't connect to MySQL server on '{host}:{port}' (10061)")
        if passwd != self.s.get("password", "secret"):
            raise FakeOperationalError(1045, f"Access denied for user '{user}'@'10.0.0.5' (using password: YES)")
        if db and db not in self.dbs:
            raise FakeOperationalError(1049, f"Unknown database '{db}'")
        return FakeConn(self, db, kw.get("init_command"))

    def module(self):
        m = types.ModuleType("MySQLdb")
        m.connect = self.connect
        m.Error = FakeError
        m.OperationalError = FakeOperationalError
        return m


class FakeConn:
    def __init__(self, server, db=None, init=None):
        self.server, self.db, self.init = server, db, init

    def cursor(self):
        return FakeCursor(self.server, self)

    def close(self):
        pass


class FakeCursor:
    def __init__(self, server, conn=None):
        self.server, self.last, self.conn = server, None, conn

    def execute(self, sql, params=None):
        self.server.log.append((self.conn.db if self.conn else None, self.conn.init if self.conn else None, sql))
        if sql.startswith("CREATE DATABASE"):
            if self.server.s.get("no_create"):
                raise FakeOperationalError(1044, "Access denied for user to database")
            name = sql.split("`")[1]
            self.server.dbs.add(name)
            self.server.created.append(name)
        if sql.startswith("CREATE TABLE") and self.server.s.get("no_ddl"):
            raise FakeOperationalError(1142, "CREATE command denied to user")
        self.last = sql

    def fetchone(self):
        if "VERSION" in self.last:
            return (self.server.s.get("version", "10.11.6-MariaDB-0+deb12u1"),)
        return (12,)

    def fetchall(self):
        return [(g,) for g in self.server.s.get("grants", ["GRANT SELECT ON `s2k`.* TO `reader`@`%`"])]


class InstallerTests(TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.env = self.dir / ".env"

    def run_setup(self, server, answers, expect_ok=True, out=None):
        f = self.dir / "answers.json"
        base = {"db_host": "192.168.100.20", "db_port": "3306", "db_name": "production_suite", "db_user": "suite", "db_password": "secret",
                "use_asset_db": "n", "web_port": "8799", "time_zone": "America/New_York", "scheduler_lead": "y", "station_key": "",
                "create_database": "y"}
        base.update(answers)
        f.write_text(json.dumps(base))
        with mock.patch.dict(sys.modules, {"MySQLdb": server.module()}), \
                override_settings(BASE_DIR=self.dir), \
                mock.patch.object(ip.Command, "run_finish", lambda self, opts, apply_network=False: None):
            if expect_ok:
                call_command("install_plant", "--answers", str(f), "--skip-port-check", stdout=out or io.StringIO())
            else:
                with self.assertRaises(CommandError) as cm:
                    call_command("install_plant", "--answers", str(f), "--skip-port-check", stdout=out or io.StringIO())
                return str(cm.exception)

    def test_good_server_writes_settings(self):
        self.run_setup(FakeServer(), {})
        env = ip.read_env(self.env)
        self.assertEqual(env["DB_ENGINE"], "mariadb")
        self.assertEqual((env["DB_HOST"], env["DB_USER"], env["DB_PASSWORD"]), ("192.168.100.20", "suite", "secret"))
        self.assertEqual(env["DJANGO_DEBUG"], "0")
        self.assertEqual(env["TICKER_IN_PROCESS"], "0")
        self.assertGreater(len(env["DJANGO_SECRET_KEY"]), 40)
        self.assertEqual(env["PLANT_NETWORKS"], "192.168.100.0/24")

    def test_plant_network_question(self):
        self.run_setup(FakeServer(), {"plant_network": "10.20.0.0/16, 192.168.100.0/24", "db_host": "10.20.1.5"})
        self.assertEqual(ip.read_env(self.env)["PLANT_NETWORKS"], "10.20.0.0/16,192.168.100.0/24")

    def test_database_outside_the_plant_network_is_refused(self):
        buf = io.StringIO()
        self.run_setup(FakeServer(), {"db_host": "10.0.0.20"}, expect_ok=False, out=buf)
        self.assertIn("not on the plant network", buf.getvalue())
        self.assertFalse(self.env.exists())

    def test_public_plant_network_is_refused(self):
        for bad in ("8.8.8.0/24", "0.0.0.0/0", "192.168.100.0/24, 203.0.113.0/24", "hello"):
            msg = self.run_setup(FakeServer(), {"plant_network": bad}, expect_ok=False)
            self.assertIn("plant_network", msg)
            self.assertFalse(self.env.exists())

    def test_missing_database_is_created(self):
        srv = FakeServer(dbs=[])
        self.run_setup(srv, {})
        self.assertEqual(srv.created, ["production_suite"])

    def test_problems_stop_setup_and_write_nothing(self):
        cases = [(FakeServer(unreachable=True), {}, "reach"),
                 (FakeServer(password="other"), {}, "user name or password"),
                 (FakeServer(dbs=[], no_create=True), {}, "GRANT ALL"),
                 (FakeServer(version="10.3.39-MariaDB"), {}, "10.5"),
                 (FakeServer(version="5.7.44"), {}, "8.0.11"),
                 (FakeServer(no_ddl=True), {}, "can't create tables")]
        for srv, ans, words in cases:
            buf = io.StringIO()
            msg = self.run_setup(srv, ans, expect_ok=False, out=buf)
            self.assertIn("Nothing was changed", msg)
            self.assertIn(words, buf.getvalue(), (words, buf.getvalue()[-400:]))
            self.assertFalse(self.env.exists())

    def test_existing_settings_are_kept_on_upgrade(self):
        self.run_setup(FakeServer(), {})
        before = ip.read_env(self.env)
        self.run_setup(FakeServer(), {"keep_settings": "y", "db_password": "changed-but-ignored"})
        self.assertEqual(ip.read_env(self.env), before)
        self.assertTrue(list(self.dir.glob(".env.backup-*")))

    def test_awkward_passwords_survive_the_settings_file(self):
        import suite.settings as st
        for pw in ['a"b', '"quoted"', " spaced ", "p#ss=word", "'single'", 'tr@iling"']:
            ip.write_env(self.env, {"DB_PASSWORD": pw})
            self.assertEqual(ip.read_env(self.env)["DB_PASSWORD"], pw)
            with mock.patch.dict("os.environ", {}, clear=False):
                import os
                os.environ.pop("DB_PASSWORD", None)
                st._load_env(self.env)  # the reader Django itself uses
                self.assertEqual(os.environ.pop("DB_PASSWORD"), pw)

    S2K = {"use_s2k_db": "y", "s2k_host": "192.168.100.30", "s2k_port": "3306", "s2k_name": "s2k", "s2k_user": "reader",
           "s2k_password": "secret"}

    def test_s2k_database_is_only_read(self):
        srv = FakeServer(dbs=["production_suite", "s2k"])
        buf = io.StringIO()
        self.run_setup(srv, self.S2K, out=buf)
        env = ip.read_env(self.env)
        self.assertEqual((env["S2K_DB_HOST"], env["S2K_DB_NAME"], env["S2K_DB_USER"]), ("192.168.100.30", "s2k", "reader"))
        s2k = [(init, sql) for db, init, sql in srv.log if db == "s2k"]
        self.assertTrue(s2k)
        for init, sql in s2k:
            self.assertEqual(init, "SET SESSION TRANSACTION READ ONLY")
            self.assertRegex(sql, r"^(<connect>|SELECT |SHOW GRANTS$)", sql)
        self.assertIn("nothing was created or changed", buf.getvalue())

    def test_missing_s2k_database_is_never_created(self):
        srv = FakeServer(dbs=["production_suite"])
        buf = io.StringIO()
        self.run_setup(srv, {**self.S2K, "create_database": "y"}, out=buf)
        self.assertEqual(srv.created, [])
        self.assertIn("nothing is ever created there", buf.getvalue())
        self.assertNotIn("S2K_DB_HOST", ip.read_env(self.env))   # skipped, the rest of setup carries on

    def test_s2k_cannot_be_the_suites_own_database(self):
        srv = FakeServer(dbs=["production_suite"])
        buf = io.StringIO()
        self.run_setup(srv, {**self.S2K, "s2k_host": "192.168.100.20", "s2k_name": "production_suite"}, out=buf)
        self.assertIn("suite's own records database", buf.getvalue())
        self.assertNotIn("S2K_DB_HOST", ip.read_env(self.env))

    def test_writable_s2k_account_gets_a_warning(self):
        srv = FakeServer(dbs=["production_suite", "s2k"], grants=["GRANT ALL PRIVILEGES ON `s2k`.* TO `reader`@`%`"])
        buf = io.StringIO()
        self.run_setup(srv, self.S2K, out=buf)
        self.assertIn("SELECT-only account is safer", buf.getvalue())

    def test_version_rules(self):
        self.assertTrue(ip.mariadb_version_ok("10.5.0-MariaDB")[0])
        self.assertTrue(ip.mariadb_version_ok("11.4.2-MariaDB-ubu2404")[0])
        self.assertFalse(ip.mariadb_version_ok("10.4.34-MariaDB")[0])
        self.assertTrue(ip.mariadb_version_ok("8.0.36")[0])
        self.assertFalse(ip.mariadb_version_ok("8.0.10")[0])


class FinishStageTests(TestCase):
    def finish(self, answers):
        f = Path(tempfile.mkdtemp()) / "a.json"
        f.write_text(json.dumps(answers))
        call_command("install_plant", "--finish", "--answers", str(f), verbosity=0)

    def test_first_shift_lead_is_created_with_a_strong_pin(self):
        self.finish({"demo": "n", "lead_name": "Dana Ruiz", "lead_pin": "482913"})
        p = Person.objects.get(role="admin")
        self.assertEqual((p.name, p.pin), ("Dana Ruiz", "482913"))

    def test_weak_pins_are_refused(self):
        for pin in ("1234", "111111", "123456", "987654", "12ab56"):
            with self.assertRaises(CommandError):
                self.finish({"demo": "n", "lead_name": "X", "lead_pin": pin})
        self.assertFalse(Person.objects.exists())

    def test_plant_network_is_stored_and_kept(self):
        from plant import netsafety
        from plant.models import PlantSettings
        with mock.patch.dict("os.environ", {"PLANT_NETWORKS": "10.20.0.0/16"}):
            call_command("install_plant", "--finish", "--apply-network", "--answers", self._answers({"demo": "n", "lead_name": "D", "lead_pin": "482913"}), verbosity=0)
        self.assertEqual(PlantSettings.objects.get(pk=1).network["allowed"], ["10.20.0.0/16"])
        netsafety.save([__import__("ipaddress").ip_network("192.168.100.0/24")], by="lead", sync=False)  # changed in Admin > Network
        with mock.patch.dict("os.environ", {"PLANT_NETWORKS": "10.20.0.0/16"}):
            self.finish({})   # a plain upgrade keeps the Admin > Network setting
        self.assertEqual(PlantSettings.objects.get(pk=1).network["allowed"], ["192.168.100.0/24"])

    def _answers(self, answers):
        f = Path(tempfile.mkdtemp()) / "a.json"
        f.write_text(json.dumps(answers))
        return str(f)

    def test_rerun_keeps_existing_lead(self):
        self.finish({"demo": "n", "lead_name": "Dana Ruiz", "lead_pin": "482913"})
        self.finish({})
        self.assertEqual(Person.objects.count(), 1)


class SetupWindowTests(TestCase):
    """The page checks and progress lines the Windows setup window relies on."""

    def check(self, kind, answers, server=None):
        f = Path(tempfile.mkdtemp()) / "a.json"
        f.write_text(json.dumps(answers))
        out = io.StringIO()
        mods = {"MySQLdb": (server or FakeServer()).module()}
        with mock.patch.dict(sys.modules, mods), override_settings(BASE_DIR=f.parent):
            call_command("install_plant", "--check", kind, "--answers", str(f), stdout=out)
        line = [x for x in out.getvalue().splitlines() if x.startswith("@@RESULT ")]
        self.assertEqual(len(line), 1, out.getvalue())
        return json.loads(line[0][9:])

    DB = {"plant_network": "192.168.100.0/24", "db_host": "192.168.100.20", "db_port": "3306", "db_name": "production_suite",
          "db_user": "suite", "db_password": "secret"}

    def test_network_page(self):
        r = self.check("network", {"plant_network": "192.168.100.0/24, 10.20.0.0/16"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["networks"], "192.168.100.0/24,10.20.0.0/16")
        self.assertFalse(self.check("network", {"plant_network": "8.8.8.0/24"})["ok"])

    def test_database_page(self):
        self.assertTrue(self.check("db", self.DB)["ok"])
        r = self.check("db", dict(self.DB, db_password="wrong"))
        self.assertFalse(r["ok"])
        self.assertIn("user name or password", r["msg"])
        self.assertFalse(self.check("db", dict(self.DB, db_host="8.8.8.8"))["ok"])

    def test_missing_database_is_only_created_when_asked(self):
        srv = FakeServer(dbs=[])
        r = self.check("db", self.DB, srv)
        self.assertFalse(r["ok"])
        self.assertTrue(r["missing"])
        self.assertEqual(srv.created, [])
        r = self.check("db", dict(self.DB, create_database="y"), srv)
        self.assertTrue(r["ok"], r)
        self.assertEqual(srv.created, ["production_suite"])

    def test_s2k_page_is_read_only(self):
        srv = FakeServer(dbs=["production_suite", "s2k"])
        r = self.check("s2k", dict(self.DB, s2k_host="192.168.100.30", s2k_port="3306", s2k_name="s2k", s2k_user="r", s2k_password="secret"), srv)
        self.assertTrue(r["ok"], r)
        for db, init, sql in srv.log:
            if db == "s2k":
                self.assertRegex(sql, r"^(<connect>|SELECT |SHOW GRANTS$)")

    def test_server_and_pin_pages(self):
        self.assertTrue(self.check("server", {"web_port": "8799", "time_zone": "America/Chicago"})["ok"])
        self.assertFalse(self.check("server", {"web_port": "99999", "time_zone": "America/Chicago"})["ok"])
        self.assertFalse(self.check("server", {"web_port": "8799", "time_zone": "Mars/Base"})["ok"])
        self.assertFalse(self.check("pin", {"lead_pin": "111111", "lead_name": "Dana"})["ok"])
        self.assertTrue(self.check("pin", {"lead_pin": "482913", "lead_name": "Dana"})["ok"])

    def test_progress_and_error_lines(self):
        d = Path(tempfile.mkdtemp())
        f = d / "a.json"
        f.write_text(json.dumps(dict(self.DB, db_password="wrong", use_asset_db="n", web_port="8799", time_zone="America/New_York")))
        out = io.StringIO()
        with mock.patch.dict(sys.modules, {"MySQLdb": FakeServer().module()}), override_settings(BASE_DIR=d):
            with self.assertRaises(CommandError):
                call_command("install_plant", "--answers", str(f), "--progress", "--skip-port-check", stdout=out)
        text = out.getvalue()
        self.assertIn("@@PROGRESS 3 Checking the plant network", text)
        self.assertIn("@@PROGRESS 6 Connecting to MariaDB", text)
        self.assertIn("@@ERROR MariaDB refused that user name or password.", text)
        self.assertFalse((d / ".env").exists())
