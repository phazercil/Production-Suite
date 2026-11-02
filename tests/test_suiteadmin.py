"""The Production Suite Admin desktop program's back end (manage.py suiteadmin)."""
import io
import json
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.test import override_settings

from hmi.models import AuditLog
from plant.envfile import read_env

from .test_platform import Base


class SuiteAdminTests(Base):
    def setUp(self):
        super().setUp()
        self.dir = Path(tempfile.mkdtemp())
        o = override_settings(BASE_DIR=self.dir)
        o.enable()
        self.addCleanup(o.disable)
        (self.dir / ".env").write_text('WEB_PORT="8999"\nPLANT_TZ="America/New_York"\nDB_ENGINE="mariadb"\nDB_HOST="192.168.100.20"\n'
                                       'DB_NAME="old"\nDB_USER="suite"\nDB_PASSWORD="oldpw"\n', encoding="utf-8")

    def run_cmd(self, *args, params=None):
        if params is not None:
            f = self.dir / "p.json"
            f.write_text(json.dumps(params), encoding="utf-8")
            args = args + ("--params", str(f))
        out = io.StringIO()
        call_command("suiteadmin", *args, stdout=out)
        line = [x for x in out.getvalue().splitlines() if x.startswith("@@RESULT ")][-1]
        if params is not None:
            self.assertFalse((self.dir / "p.json").exists(), "the params file (it can hold passwords) is removed")
        return json.loads(line[9:])

    def test_info_has_no_secrets(self):
        r = self.run_cmd("info")
        self.assertTrue(r["ok"])
        self.assertEqual((r["server"]["port"], r["records"]["name"], r["records"]["hasPassword"]), ("8999", "old", True))
        self.assertEqual(r["revision"], settings.SUITE["REVISION"])
        self.assertNotIn("oldpw", json.dumps(r))

    def test_connection_commands_are_checked_and_audited(self):
        r = self.run_cmd("cmd", "connSave", params={"group": "paycom", "fields": {"sid": "abc", "token": "tok"}})
        self.assertTrue(r["ok"], r)
        self.assertEqual(read_env(self.dir / ".env")["PAYCOM_SID"], "abc")
        self.assertTrue(AuditLog.objects.filter(by__startswith="Server desktop", what__icontains="Paycom").exists())
        r = self.run_cmd("cmd", "connSave", params={"group": "s2k", "fields": {"host": "8.8.8.8", "name": "a", "user": "u", "password": "p"}})
        self.assertFalse(r["ok"])
        self.assertFalse(self.run_cmd("cmd", "startShift", params={})["ok"])  # only connection / network commands
        r = self.run_cmd("cmd", "networkSet", params={"allowed": ["192.168.100.0/24", "10.1.0.0/16"]})
        self.assertTrue(r["ok"], r)

    def test_server_settings(self):
        self.assertFalse(self.run_cmd("server-save", params={"port": "80000", "tz": "America/Chicago"})["ok"])
        self.assertFalse(self.run_cmd("server-save", params={"port": "8999", "tz": "Mars/Base"})["ok"])
        r = self.run_cmd("server-save", params={"port": "8999", "tz": "America/Chicago", "stationKey": "abc_1"})
        self.assertTrue(r["ok"] and r["restart"] and not r["portChanged"], r)
        env = read_env(self.dir / ".env")
        self.assertEqual((env["PLANT_TZ"], env["STATION_KEY"], env["DB_PASSWORD"]), ("America/Chicago", "abc_1", "oldpw"))

    def test_records_database_save_and_undo(self):
        r = self.run_cmd("records-save", params={"host": "192.168.100.21", "port": "3306", "name": "new", "user": "suite2", "password": ""})
        self.assertTrue(r["ok"], r)
        env = read_env(self.dir / ".env")
        self.assertEqual((env["DB_HOST"], env["DB_NAME"], env["DB_PASSWORD"]), ("192.168.100.21", "new", "oldpw"))  # blank keeps the password
        self.assertTrue(list(self.dir.glob(".env.backup-records-*")))
        self.assertTrue(self.run_cmd("records-undo")["ok"])
        env = read_env(self.dir / ".env")
        self.assertEqual((env["DB_HOST"], env["DB_NAME"], env["DB_USER"]), ("192.168.100.20", "old", "suite"))
        self.assertFalse(self.run_cmd("records-undo")["ok"])
