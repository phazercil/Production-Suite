"""Updates without the installer: update files, Admin > Updates, the update folder, and the updater itself
(install, go back, and going back by itself when the new version doesn't start) on a throwaway copy."""
import base64
import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import Client, SimpleTestCase, override_settings
from django.utils import timezone

from plant import ed25519, updatefile, updates

from .test_platform import Base, cmd, login

SRC = Path(settings.BASE_DIR)
SECRET = bytes(range(32))
PUB = base64.b64encode(ed25519.public_key(SECRET)).decode()
OTHER = bytes(range(1, 33))


def copy_app(dest):
    dest.mkdir(parents=True, exist_ok=True)
    for rel in updatefile.app_files(SRC):
        if rel.startswith(("tests/", "labor/source/")):
            continue
        t = dest / rel
        t.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SRC / rel, t)
    return dest


def set_revision(app, rev, version="9.9.9"):
    (app / "suite" / "version.py").write_text(f'VERSION = "{version}"\nREVISION = {rev}\n', encoding="utf-8")


class FileFormatTests(SimpleTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.app = self.tmp / "app"
        self.app.mkdir()
        (self.app / "suite").mkdir()
        set_revision(self.app, 5)
        (self.app / "manage.py").write_text("print('hi')\n")
        (self.app / ".env").write_text("SECRET=never shipped\n")
        (self.app / "logs").mkdir()
        (self.app / "logs" / "web.log").write_text("x")
        (self.app / "requirements-windows.txt").write_text("Django\n")

    def build(self, secret=SECRET, wheels=None):
        out = self.tmp / "u.zip"
        updatefile.build(self.app, out, secret, wheels, "notes here")
        return out

    def test_round_trip_and_what_is_left_out(self):
        m = updatefile.verify(self.build(), [PUB])
        self.assertEqual((m["version"], m["revision"], m["hasPackages"], m["notes"]), ("9.9.9", 5, False, "notes here"))
        self.assertEqual(sorted(m["files"]), ["app/manage.py", "app/requirements-windows.txt", "app/suite/version.py"])

    def test_wrong_key_or_changed_files_are_refused(self):
        with self.assertRaisesRegex(updatefile.UpdateError, "signature"):
            updatefile.verify(self.build(OTHER), [PUB])
        good = self.build()
        # a file changed after signing
        bad = self.tmp / "bad.zip"
        with zipfile.ZipFile(good) as zin, zipfile.ZipFile(bad, "w") as zout:
            for i in zin.infolist():
                data = zin.read(i)
                zout.writestr(i, b"import os; os.system('x')\n" if i.filename == "app/manage.py" else data)
        with self.assertRaisesRegex(updatefile.UpdateError, "changed after it was built"):
            updatefile.verify(bad, [PUB])
        # an extra file slipped in, or a path that climbs out of the folder
        for name in ("app/extra.py", "app/../../evil.py"):
            shutil.copy(good, bad)
            with zipfile.ZipFile(bad, "a") as z:
                z.writestr(name, "x")
            with self.assertRaises(updatefile.UpdateError):
                updatefile.verify(bad, [PUB])
        (self.tmp / "nz.zip").write_text("not a zip")
        with self.assertRaisesRegex(updatefile.UpdateError, "not a zip"):
            updatefile.verify(self.tmp / "nz.zip", [PUB])

    def test_packages_hash_matches_installer(self):
        # PowerShell: SHA-256 of (requirements text + "|" + wheel names sorted, joined with ","), upper-case hex
        h = updatefile.packages_hash("Django\r\n", ["waitress-3.whl", "Django-5.whl", "asgiref-3.whl"])
        import hashlib
        self.assertEqual(h, hashlib.sha256("Django\r\n|asgiref-3.whl,Django-5.whl,waitress-3.whl".encode()).hexdigest().upper())


class AdminUpdateTests(Base):
    def setUp(self):
        super().setUp()
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        o = override_settings(BASE_DIR=self.dir)
        o.enable()
        self.addCleanup(o.disable)
        k = mock.patch.object(updates, "PUBLIC_KEYS", [PUB])
        k.start()
        self.addCleanup(k.stop)
        self.c = Client()
        login(self.c, "9999")
        self.src = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.src, True)
        (self.src / "suite").mkdir()
        (self.src / "manage.py").write_text("")

    def make(self, rev, secret=SECRET):
        set_revision(self.src, rev)
        out = self.src.parent / f"upd-{rev}-{id(self)}.zip"
        updatefile.build(self.src, out, secret)
        self.addCleanup(lambda: out.unlink(missing_ok=True))
        return out

    def upload(self, path, when="now", c=None):
        with open(path, "rb") as f:
            return (c or self.c).post("/api/updates/upload", {"file": f, "when": when})

    def test_upload_checks_and_waits(self):
        r = self.upload(self.make(settings.SUITE["REVISION"]))
        self.assertEqual(r.status_code, 400)
        self.assertIn("already installed", r.json()["error"])
        r = self.upload(self.make(999, OTHER))
        self.assertIn("signature", r.json()["error"])
        op = Client()
        login(op, "1111")
        self.assertEqual(self.upload(self.make(999), c=op).status_code, 403)
        r = self.upload(self.make(999), when="idle")
        self.assertTrue(r.json()["ok"], r.json())
        st = updates.state()
        self.assertEqual((st["status"], st["revision"], st["when"]), ("ready", 999, "idle"))
        self.assertEqual(len(list((self.dir / "updates" / "ready").glob("*.zip"))), 1)
        # a line is running (demo shift): an "idle" update waits
        with mock.patch.object(updates, "launch") as launch:
            updates.tick()
            launch.assert_not_called()
            r = cmd(self.c, "updWhen", {"when": "now"})
            self.assertTrue(r["ok"], r)
            updates.tick()
            launch.assert_called_once()
        self.assertTrue(cmd(self.c, "updCancel", {})["ok"])
        self.assertEqual(updates.state()["status"], "idle")
        self.assertFalse(list((self.dir / "updates" / "ready").glob("*.zip")))

    def test_update_folder(self):
        inbox = updates.root() / "inbox"
        good = inbox / "good.zip"
        shutil.copy(self.make(1000), good)
        (inbox / "bad.zip").write_text("nope")
        later = timezone.now() + timedelta(minutes=1)
        self.assertTrue(cmd(self.c, "updFolder", {"when": "hold"})["ok"])
        with mock.patch.object(updates, "launch") as launch:
            updates.tick(later)
            launch.assert_not_called()  # held for a shift lead
        self.assertFalse(good.exists())
        self.assertEqual(len(list((updates.root() / "rejected").iterdir())), 1)
        st = updates.state()
        self.assertEqual((st["status"], st["revision"], st["when"]), ("ready", 1000, "hold"))
        info = cmd(self.c, "updInfo", {})["data"]
        self.assertEqual(info["revision"], settings.SUITE["REVISION"])
        self.assertTrue(info["folder"].endswith("inbox"))

    def test_launch_prepares_the_updater(self):
        self.upload(self.make(1001))
        (self.dir / "tools").mkdir()
        shutil.copy(SRC / "tools" / "updater.py", self.dir / "tools" / "updater.py")
        (self.dir / "plant").mkdir()
        for f in ("updatefile.py", "ed25519.py"):
            shutil.copy(SRC / "plant" / f, self.dir / "plant" / f)
        with mock.patch("plant.updates.subprocess.Popen") as popen, mock.patch("plant.updates.os.name", "posix"):
            updates.tick()
        args = popen.call_args[0][0]
        self.assertEqual(args[-3:], ["--dir", str(self.dir), "request"])
        run = self.dir / "updates" / "run"
        self.assertEqual(json.loads((run / "keys.json").read_text()), [PUB])
        self.assertEqual(json.loads((run / "request.json").read_text())["args"][0], "apply")
        self.assertEqual(updates.state()["status"], "starting")

    def test_version_endpoint_and_label(self):
        r = Client().get("/api/version").json()
        self.assertEqual((r["version"], r["revision"]), (settings.SUITE["VERSION"], settings.SUITE["REVISION"]))
        page = Client().get("/operator/").content.decode()
        self.assertIn(f"v{settings.SUITE['VERSION']} · rev {settings.SUITE['REVISION']}", page)
        self.assertIn("suite/rev.js", page)


class Services:
    def __init__(self):
        self.calls = []

    def stop(self):
        self.calls.append("stop")

    def start(self):
        self.calls.append("start")


class UpdaterEndToEndTests(SimpleTestCase):
    """A real install folder (SQLite), a real update with a new migration, real migrate / collectstatic."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = Path(tempfile.mkdtemp())
        cls.install = copy_app(cls.tmp / "install")
        set_revision(cls.install, 1, "1.0.0")
        (cls.install / ".env").write_text('DJANGO_DEBUG="1"\nWEB_PORT="8999"\n', encoding="utf-8")
        (cls.install / "logs").mkdir()
        (cls.install / "install-info.json").write_text('{"version": "1.0.0", "packagesHash": "ABC"}')
        cls.manage("migrate", "--noinput")
        # the new version: rev 2, a new table, a new file, one file gone
        cls.new = copy_app(cls.tmp / "new")
        set_revision(cls.new, 2, "1.1.0")
        (cls.new / "WHATS-NEW.txt").write_text("new")
        (cls.new / "README.md").unlink()
        (cls.new / "plant" / "migrations" / "9001_update_probe.py").write_text(
            "from django.db import migrations\n\n\nclass Migration(migrations.Migration):\n"
            f"    dependencies = [('plant', '{cls.leaf('plant')}')]\n"
            "    operations = [migrations.RunSQL('CREATE TABLE upd_probe (id integer)', 'DROP TABLE upd_probe')]\n")
        cls.zip = cls.tmp / "update-r2.zip"
        updatefile.build(cls.new, cls.zip, SECRET, notes="test")
        run = cls.install / "updates" / "run"  # where the suite puts the updater before starting it
        run.mkdir(parents=True)
        shutil.copy(SRC / "tools" / "updater.py", run / "updater.py")
        for f in ("updatefile.py", "ed25519.py"):
            shutil.copy(SRC / "plant" / f, run / f)
        spec = importlib.util.spec_from_file_location("suite_updater", run / "updater.py")
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, True)
        super().tearDownClass()

    @classmethod
    def manage(cls, *args):
        env = {k: v for k, v in __import__("os").environ.items() if not k.startswith(("DB_", "DJANGO_"))}
        r = subprocess.run([sys.executable, "manage.py", *args], cwd=cls.install, env=env, capture_output=True, text=True, timeout=600)
        assert r.returncode == 0, r.stdout + r.stderr
        return r.stdout

    @classmethod
    def leaf(cls, app):
        names = sorted(p.stem for p in (cls.install / app / "migrations").glob("0*.py"))
        return names[-1]

    def tables(self):
        with sqlite3.connect(self.install / "suite.sqlite3") as c:
            return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def run_updater(self, healthy=True):
        run_dir = self.install / "updates" / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "keys.json").write_text(json.dumps([PUB]))
        svc = Services()
        with mock.patch.object(self.mod, "HERE", run_dir):
            r = self.mod.Run(self.install, services=svc, health=lambda rev: (healthy, f"rev {rev}" if healthy else "didn't answer"),
                             python=sys.executable)
        return r, svc

    def state(self):
        return json.loads((self.install / "updates" / "state.json").read_text())

    def test_1_install_then_go_back(self):
        self.assertTrue((self.install / "suite.sqlite3").exists(), list(self.install.iterdir()))
        r, svc = self.run_updater()
        zip_copy = self.tmp / "u1.zip"
        shutil.copy(self.zip, zip_copy)
        with mock.patch.object(self.mod, "HERE", self.install / "updates" / "run"):
            self.assertTrue(r.apply(zip_copy))
        self.assertEqual(updatefile.read_version(self.install), ("1.1.0", 2))
        self.assertTrue((self.install / "WHATS-NEW.txt").exists())
        self.assertFalse((self.install / "README.md").exists())
        self.assertIn('WEB_PORT="8999"', (self.install / ".env").read_text())  # settings kept
        self.assertIn("upd_probe", self.tables())
        self.assertEqual(svc.calls, ["stop", "start"])
        self.assertEqual(self.state()["status"], "done")
        self.assertEqual(json.loads((self.install / "install-info.json").read_text())["revision"], 2)
        self.assertFalse(zip_copy.exists())
        # an older or the same revision is refused before anything stops
        r, svc = self.run_updater()
        shutil.copy(self.zip, zip_copy)
        with mock.patch.object(self.mod, "HERE", self.install / "updates" / "run"), self.assertRaises(self.mod.Failed):
            r.apply(zip_copy)
        self.assertEqual(svc.calls, [])
        # go back
        r, svc = self.run_updater()
        with mock.patch.object(self.mod, "HERE", self.install / "updates" / "run"):
            self.assertTrue(r.goback())
        self.assertEqual(updatefile.read_version(self.install), ("1.0.0", 1))
        self.assertTrue((self.install / "README.md").exists())
        self.assertFalse((self.install / "WHATS-NEW.txt").exists())
        self.assertNotIn("upd_probe", self.tables())
        self.assertTrue((self.install / "suite.sqlite3").exists())
        self.assertEqual(self.state()["status"], "done")

    def test_2_goes_back_by_itself_when_the_new_version_does_not_start(self):
        r, svc = self.run_updater(healthy=False)
        zip_copy = self.tmp / "u2.zip"
        shutil.copy(self.zip, zip_copy)
        with mock.patch.object(self.mod, "HERE", self.install / "updates" / "run"), self.assertRaises(self.mod.Failed):
            r.apply(zip_copy)
        self.assertEqual(updatefile.read_version(self.install), ("1.0.0", 1))
        self.assertTrue((self.install / "README.md").exists())
        self.assertNotIn("upd_probe", self.tables())
        st = self.state()
        self.assertEqual(st["status"], "failed")
        self.assertIn("previous version (rev 1) was put back", st["message"])
        self.assertEqual(svc.calls, ["stop", "start", "stop", "start"])
        self.assertFalse([p for p in (self.install / "backups").iterdir() if p.name.startswith("1.0.0-r1-")][1:], "the used backup is removed")


class UpdaterRequestTests(SimpleTestCase):
    def test_a_refused_request_says_why(self):
        """"Go back" with no earlier version fails before anything is installed; Admin > Updates still hears why."""
        spec = importlib.util.spec_from_file_location("suite_updater_req", SRC / "tools" / "updater.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "updates" / "run").mkdir(parents=True)
        (d / "updates" / "run" / "request.json").write_text('{"args": ["goback"]}')
        with mock.patch.object(mod, "Services", lambda folder: Services()):
            self.assertEqual(mod.main(["--dir", str(d), "request"]), 1)
        st = json.loads((d / "updates" / "state.json").read_text())
        self.assertEqual(st["status"], "failed")
        self.assertIn("no earlier version", st["message"])
        self.assertFalse((d / "updates" / "run" / "request.json").exists())
        self.assertFalse((d / "updates" / "run" / "lock").exists())
