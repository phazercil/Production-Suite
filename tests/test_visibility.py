"""Visibility: OEE and speed loss, downtime Pareto / repeats / cost, Admin › Health, Supervisor › Timeline, the daily
digest, scheduled and off-server backups, must-read handoff notes, and live updates."""
import gzip
import sqlite3
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.test import Client, TestCase
from django.utils import timezone

from hmi import engine
from hmi.models import AlertContact, Downtime, MoRun, PpmReading, ShiftNote, ShiftRun
from plant.demo import seed_demo
from plant.models import Machine, Person, PlantSettings
from reports import engine as rep

from .test_quality import sup_login


class OeeAndDowntime(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.bar = Machine.objects.get(name="Barbara")
        self.bar.ppm = 200
        self.bar.save()
        self.t0 = timezone.now().replace(second=0, microsecond=0) - timedelta(days=2)
        PpmReading.objects.filter(line="Barbara", t__gte=self.t0 - timedelta(hours=1), t__lt=self.t0 + timedelta(hours=3)).delete()
        Downtime.objects.filter(line="Barbara").delete()
        MoRun.objects.filter(line="Barbara").delete()
        sh = ShiftRun.objects.create(machine=self.bar, line="Barbara", start=self.t0, end=self.t0 + timedelta(hours=2), active=False)
        for i in range(100):    # 80 minutes running at 150 of 200 P/m, 20 stopped
            run = i < 80
            PpmReading.objects.create(machine=self.bar, shift=sh, line="Barbara", t=self.t0 + timedelta(minutes=i), ppm=150 if run else 0,
                                      units=10 if run else 0, run=run)
        MoRun.objects.create(shift=sh, line="Barbara", mo="A1", item="P", qty=800, scrap=40, start_time=self.t0, end_time=self.t0 + timedelta(minutes=99))
        for k in range(4):
            Downtime.objects.create(shift=sh, line="Barbara", start_time=self.t0 + timedelta(minutes=80 + 5 * k), end_time=self.t0 + timedelta(minutes=85 + 5 * k),
                                    minutes=5, reason="Pleater jam")
        self.a, self.b = self.t0, self.t0 + timedelta(minutes=100)

    def test_oee_is_availability_times_performance_times_quality(self):
        t, rows = rep.summarize(rep.parse_scope("line:Barbara"), self.a, self.b, 5)
        self.assertEqual(t["uptime"], 80.0)
        self.assertEqual(t["perf"], 75.0)
        self.assertEqual(t["quality"], 95.0)
        self.assertEqual(t["oee"], 57.0)
        self.assertEqual(t["speedLossMin"], 20.0)

    def test_pareto_repeats_and_cost(self):
        d = rep.downtime_intel(rep.parse_scope("line:Barbara"), self.a, self.b)
        self.assertEqual(d["pareto"][0]["reason"], "Pleater jam")
        self.assertEqual(d["pareto"][0]["cum"], 100.0)
        self.assertEqual(d["repeats"][0]["n"], 4)
        self.assertEqual(d["lostFilters"], 200)       # 20 min down x 10 filters per running minute

    def test_report_api_has_oee_and_downtime(self):
        c = Client()
        day = timezone.localtime(self.t0).date().isoformat()
        r = c.get(f"/api/report?scope=line:Barbara&period=day:{day}").json()
        self.assertIn("oee", r["total"])
        self.assertIn("pareto", r["downtime"])
        self.assertNotIn("wages", r["downtime"])     # not a shift lead


class HealthTimelineDigest(TestCase):
    def setUp(self):
        seed_demo(reset=True)

    def test_health_tab(self):
        c = Client(HTTP_HX_REQUEST="true")
        c.post("/kiosk/do/login", {"pin": "9999"})
        import logging
        logging.getLogger("plant").warning("S2K pull failed: test")
        r = c.get("/kiosk/v/admin?tab=health")
        self.assertContains(r, "Minute ticker")
        self.assertContains(r, "S2K pull failed: test")
        self.assertContains(r, "Barbara")

    def test_not_reporting_is_not_stopped(self):
        from hmi.models import LineState
        from plant import health
        m = Machine.objects.get(name="Barbara")
        m.feed_type = "db"
        ls = LineState.objects.get(machine=m)
        ls.feed = {"ok": False, "err": "timeout", "t": int(timezone.now().timestamp() * 1000)}
        sh = ShiftRun.objects.filter(machine=m, active=True).first()
        self.assertEqual(health.machine_state(m, ls, sh, timezone.now())[0], "not reporting")
        ls.feed = {"ok": True, "t": int(timezone.now().timestamp() * 1000)}
        sh.state = "stopped"
        self.assertEqual(health.machine_state(m, ls, sh, timezone.now())[0], "stopped")

    def test_timeline_merges_sources_with_before_and_after(self):
        from supervisor.models import HistoryEdit
        now = timezone.now()
        HistoryEdit.objects.create(t=now, by="K. Patel", kind="log", obj_id=1, line="Barbara", field="Units", old="90", new="100", why="paper log")
        c = Client()
        self.assertTrue(sup_login(c, "4444")["ok"])
        r = c.get("/supervisor/?tab=events").content.decode()
        self.assertIn("Corrected log #1: Units", r)
        self.assertIn("paper log", r)
        r = c.get("/supervisor/?tab=events&kind=edit&csv=1")
        self.assertIn("Corrected log #1", r.content.decode())

    def test_digest(self):
        subject, body = __import__("hmi.alerts", fromlist=["digest"]).digest(timezone.now())
        self.assertIn("OEE", subject)
        self.assertIn("Needs attention", body)
        AlertContact.objects.create(name="Lead", email="lead@plant.local", digest=True, down_after=0)
        st = PlantSettings.load()
        st.settings = {**st.settings, "digest": {"at": "00:00"}}
        st.save()
        from hmi import alerts
        jobs = alerts.digest_jobs(timezone.now(), list(AlertContact.objects.all()))
        self.assertEqual(len(jobs), 1)


class BackupsHandoffsLive(TestCase):
    def setUp(self):
        seed_demo(reset=True)

    def test_offsite_copy_is_checked(self):
        from plant import dbbackup, offsite
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            def fake_make():    # (the real one waits for the test's own open transaction on SQLite)
                plain = Path(src) / "x.sqlite3"
                with sqlite3.connect(plain) as c:
                    c.execute("CREATE TABLE t (a int)")
                name = "production_suite-20260102-030405.sqlite3.gz"
                with open(plain, "rb") as i, gzip.open(Path(src) / name, "wb") as o:
                    o.write(i.read())
                plain.unlink()
                return next(x for x in dbbackup.listing() if x["name"] == name)
            with mock.patch.object(dbbackup, "folder", return_value=Path(src)), mock.patch.object(dbbackup, "make", side_effect=fake_make):
                st = PlantSettings.load()
                st.settings = {**st.settings, "dbBackup": {"every": 24, "copyTo": dst, "keep": 2}}
                st.save()
                msg = offsite.tick({}, timezone.now())
                self.assertIn("copied", msg)
                self.assertIn("Test restore passed", msg)
                self.assertTrue(offsite.status()["ok"])
                bad = Path(dst) / "production_suite-20260101-000000.sql.gz"
                with gzip.open(bad, "wt") as f:
                    f.write("CREATE TABLE x (a int);\n")      # cut short
                with self.assertRaises(offsite.OffsiteError):
                    offsite.verify(bad)
        with self.assertRaises(offsite.OffsiteError):
            offsite.check_dest("\\\\8.8.8.8\\share")               # not on the plant network

    def test_must_read_handoff(self):
        bar = Machine.objects.get(name="Barbara")
        ShiftNote.objects.create(t=timezone.now(), line="Barbara", by="J. Ortiz", text="Watch side-band tension", pinned=True)
        n = engine.handoffs("Barbara", timezone.now())
        self.assertEqual(len(n), 1)
        ShiftRun.objects.filter(machine=bar).update(active=False)
        op = Person.objects.get(pin="1111")
        op.trained.add(bar)
        ctx = engine.Ctx(user=op, line="Barbara")
        safety = [True] * len(engine.config()["safety"])
        r = engine.execute("startShift", {"line": "Barbara", "safety": safety}, ctx)
        self.assertFalse(r["ok"])
        r = engine.execute("startShift", {"line": "Barbara", "safety": safety, "acks": [str(n[0].pk)]}, ctx)
        self.assertTrue(r["ok"], r)
        self.assertEqual(ShiftNote.objects.get(pk=n[0].pk).acked_by, op.name)
        self.assertEqual(engine.handoffs("Barbara", timezone.now()), [])

    def test_live_updates_are_opt_in(self):
        c = Client()
        self.assertEqual(c.get("/thin/events?scope=data").status_code, 404)
        st = PlantSettings.load()
        st.settings = {**st.settings, "liveUpdates": True}
        st.save()
        from suite import thin
        with mock.patch.object(thin, "SSE_SECONDS", 0.5):
            r = c.get("/thin/events?scope=data")
            first = next(iter(r.streaming_content)).decode()
        self.assertTrue(first.startswith("data: "))
        self.assertIn('"data"', first)
        self.assertIn('data-sse="1"', c.get("/data/").content.decode())
