"""The control service behind Production Suite Admin and the tray icon (plant/control.py), against a real
throwaway copy of the suite (SQLite, demo data). The Windows task parts are replaced by a fake."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from django.test import SimpleTestCase

from plant import control

from .test_updates import copy_app


class FakeSuite(control.Suite):
    def __init__(self, folder):
        super().__init__(folder, python=sys.executable)
        self.calls = []
        self.up = True

    def task_state(self, name):
        return "Running" if self.up else "Ready"

    def answering(self, wait=3):
        return {"version": "9.9.9", "revision": 9} if self.up else None

    def stop(self):
        self.calls.append("stop")
        self.up = False

    def start(self):
        self.calls.append("start")
        self.up = True
        return {"ok": True, "msg": "Running"}


class ControlTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = Path(tempfile.mkdtemp())
        cls.install = copy_app(cls.tmp / "install")
        (cls.install / ".env").write_text('DJANGO_DEBUG="1"\nWEB_PORT="8999"\n', encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("DB_", "DJANGO_"))}
        for args in (["migrate", "--noinput"], ["seed_demo", "--yes"]):
            r = subprocess.run([sys.executable, "manage.py", *args], cwd=cls.install, env=env, capture_output=True, text=True, timeout=600)
            assert r.returncode == 0, r.stdout + r.stderr
        cls.suite = FakeSuite(cls.install)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), control.make_handler(cls.suite, control.Sessions()))
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, True)
        super().tearDownClass()

    def post(self, path, body=None, header=True):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body or {}).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **({"X-Suite-Control": "1"} if header else {})})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=120) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def login(self):
        code, r = self.post("/login", {"pin": "9999", "who": "operator1"})
        self.assertTrue(r["ok"], r)
        return r["token"]

    def test_only_programs_on_this_server(self):
        self.assertEqual(self.post("/status", header=False)[0], 403)  # a web page can't send the header
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(f"http://127.0.0.1:{self.port}/status", timeout=10)

    def test_status_start_restart_and_logs_need_no_pin(self):
        code, r = self.post("/status")
        self.assertEqual((code, r["ok"], r["answering"]["revision"], r["port"]), (200, True, 9, 8999))
        self.suite.calls.clear()
        self.assertTrue(self.post("/restart")[1]["ok"])
        self.assertEqual(self.suite.calls, ["stop", "start"])
        self.assertTrue(self.post("/log", {"name": "web.log"})[1]["ok"])
        self.assertFalse(self.post("/log", {"name": "../.env"})[1]["ok"])
        r = self.post("/run", {"args": ["suiteadmin", "info"]})[1]
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["server"]["port"], "8999")

    def test_stop_and_changes_need_a_shift_lead_pin(self):
        r = self.post("/stop")[1]
        self.assertTrue(r["pin"] and not r["ok"])
        r = self.post("/run", {"args": ["suiteadmin", "cmd", "connSave"], "params": {"group": "paycom", "fields": {"sid": "x", "token": "y"}}})[1]
        self.assertTrue(r["pin"])
        self.assertFalse(self.post("/login", {"pin": "1111"})[1]["ok"])  # an operator's PIN
        tok = self.login()
        r = self.post("/run", {"token": tok, "who": "operator1", "args": ["suiteadmin", "cmd", "connSave"],
                               "params": {"group": "paycom", "fields": {"sid": "abc", "token": "tok"}}})[1]
        self.assertTrue(r["ok"], r)
        self.assertIn('PAYCOM_SID="abc"', (self.install / ".env").read_text())
        with sqlite3.connect(self.install / "suite.sqlite3") as db:
            by = db.execute("SELECT by FROM hmi_audit ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.assertIn("(server desktop)", by)
        self.assertIn("operator1", by)
        self.assertFalse(list((self.install / "logs").glob("admin-*.json")), "the params file (it can hold passwords) is removed")
        # only the suite's own admin commands
        self.assertFalse(self.post("/run", {"token": tok, "args": ["flush", "--noinput"]})[1]["ok"])
        self.assertFalse(self.post("/run", {"token": tok, "args": ["suiteadmin", "cmd", "startShift"], "params": {}})[1]["ok"])
        self.suite.calls.clear()
        self.assertTrue(self.post("/stop", {"token": tok})[1]["ok"])
        self.assertEqual(self.suite.calls, ["stop"])
        self.post("/start")
        self.assertTrue(self.post("/logout", {"token": tok})[1]["ok"])
        self.assertTrue(self.post("/stop", {"token": tok})[1]["pin"])
        self.assertIn("logged in to Production Suite Admin", (self.install / "logs" / "admin.log").read_text())

    def test_wrong_pins_are_slowed_down(self):
        sessions = control.Sessions()
        sessions.fails = [__import__("time").time()] * 5
        self.assertTrue(sessions.limited())
