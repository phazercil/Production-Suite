"""Admin › Backup: backing up the suite's own database."""
import gzip
import shutil
import sqlite3
import tempfile
from pathlib import Path
from unittest import mock

from django.db import connection
from django.test import Client, TransactionTestCase

from plant import dbbackup
from plant.demo import seed_demo
from plant.models import Machine


class DbBackupTests(TransactionTestCase):
    def setUp(self):
        seed_demo(reset=True)
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        p = mock.patch.object(dbbackup, "folder", return_value=d / "database")
        p.start()
        self.addCleanup(p.stop)
        self.c = Client(HTTP_HX_REQUEST="true")

    def test_button_list_download_and_permissions(self):
        self.c.post("/kiosk/do/login", {"pin": "1111"})              # an operator can't
        self.c.post("/kiosk/do/dbBackup")
        self.assertEqual(dbbackup.listing(), [])
        self.c.post("/kiosk/do/logout")
        self.c.post("/kiosk/do/login", {"pin": "9999"})
        r = self.c.get("/kiosk/v/admin?tab=backup")
        self.assertContains(r, "Back up the database now")
        r = self.c.post("/kiosk/do/dbBackup")
        self.assertContains(r, "Database backed up")
        [b] = dbbackup.listing()
        self.assertRegex(b["name"], dbbackup.NAME_RE)
        r = self.c.get(f"/kiosk/backup/{b['name']}")
        self.assertEqual(r.status_code, 200)
        body = b"".join(r.streaming_content)
        if connection.vendor == "mysql":
            sql = gzip.decompress(body).decode()
            self.assertIn("CREATE TABLE `plant_machine`", sql)
            self.assertIn("'Barbara'", sql)
        else:   # SQLite: a copy of the database file
            p = dbbackup.folder() / "check.sqlite3"
            p.write_bytes(gzip.decompress(body))
            with sqlite3.connect(p) as c:
                self.assertEqual(c.execute("SELECT count(*) FROM plant_machine").fetchone()[0], Machine.objects.count())
        self.assertEqual(self.c.get("/kiosk/backup/../../.env").status_code, 404)
        self.assertEqual(self.c.get("/kiosk/backup/x.sql.gz").status_code, 404)
        self.c.post("/kiosk/do/logout")
        self.assertEqual(self.c.get(f"/kiosk/backup/{b['name']}").status_code, 404)

    def test_keeps_the_newest(self):
        f = dbbackup.folder()
        f.mkdir(parents=True)
        for i in range(dbbackup.KEEP + 3):
            (f / f"production_suite-20260101-0000{i:02d}.sql.gz").write_bytes(b"x")
        dbbackup._prune()
        names = [x["name"] for x in dbbackup.listing()]
        self.assertEqual(len(names), dbbackup.KEEP)
        self.assertNotIn("production_suite-20260101-000000.sql.gz", names)
