"""Supervisor › History builder: operators and parts filled in from the paper log for time recorded with nobody logged
in (split at hand-overs), and logs added by hand for time with no record."""
import json
from datetime import datetime, timedelta

from django.test import Client, TestCase
from django.utils import timezone

from hmi import engine
from hmi.models import Downtime, MoRun, PpmReading, QCCheck, ShiftRun
from plant.demo import seed_demo
from plant.models import MO, Machine, Person
from supervisor.build import to_fill
from supervisor.models import HistoryEdit


class HistoryBuilderTests(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.lead = engine.Ctx(user=Person.objects.get(name="S. Lead"))
        self.op = engine.Ctx(user=Person.objects.get(name="J. Ortiz"))
        self.m = Machine.objects.get(name="Salvatore")
        d = timezone.localdate() - timedelta(days=40)
        self.day = d
        self.t0 = timezone.make_aware(datetime(d.year, d.month, d.day, 6, 0))
        self.t1 = self.t0 + timedelta(hours=8)
        self.sh = ShiftRun.objects.create(machine=self.m, line=self.m.name, shift_name="1st shift", start=self.t0, end=self.t1, active=False,
                                          state="idle", count=480 * 3)
        PpmReading.objects.bulk_create([PpmReading(machine=self.m, line=self.m.name, shift=self.sh, t=self.t0 + timedelta(minutes=i + 1), ppm=180,
                                                   units=3, run=True) for i in range(480)])
        self.down = Downtime.objects.create(shift=self.sh, line=self.m.name, start_time=self.t0 + timedelta(hours=3, minutes=50),
                                            end_time=self.t0 + timedelta(hours=4, minutes=20), minutes=30, reason="Jam")
        self.qc = QCCheck.objects.create(shift=self.sh, t=self.t0 + timedelta(hours=6), line=self.m.name, item="MG4062HP", by="?", passed=True)
        self.mo = MO.objects.create(number="999001", part="MG4062HP", qty=5000)

    def fill(self, rows, ctx=None, **kw):
        return engine.execute("histFill", {"id": self.sh.pk, "rows": rows, **kw}, ctx or self.lead)

    def test_operators_cannot_fill(self):
        self.assertFalse(self.fill([{"who": "J. Ortiz", "from": "06:00", "to": "14:00"}], self.op)["ok"])

    def test_listed_until_filled(self):
        self.assertIn(self.sh.pk, [x.pk for x in to_fill(self.m.name)])
        r = self.fill([{"who": "J. Ortiz", "from": "06:00", "to": "14:00", "mo": "999001"}], why="paper log")
        self.assertTrue(r["ok"], r)
        self.sh.refresh_from_db()
        self.assertEqual((self.sh.user_name, self.sh.person.name, self.sh.count), ("J. Ortiz", "J. Ortiz", 1440))
        run = MoRun.objects.get(shift=self.sh)
        self.assertEqual((run.mo, run.item, run.qty), ("999001", "MG4062HP", 1440))   # part from the MO, qty from the machine
        self.assertTrue(HistoryEdit.objects.filter(kind="log", obj_id=self.sh.pk, field="Operator", new="J. Ortiz", why="paper log").exists())
        self.assertTrue(HistoryEdit.objects.filter(kind="morun", obj_id=run.pk).exists())
        self.assertNotIn(self.sh.pk, [x.pk for x in to_fill(self.m.name)])
        self.assertEqual(MO.objects.get(number="999001").produced, 0)   # order progress isn't touched

    def test_split_at_hand_over(self):
        r = self.fill([{"who": "J. Ortiz", "from": "06:00", "to": "10:00", "mo": "999001"},
                       {"who": "M. Lee", "from": "10:00", "to": "14:00", "item": "cf1234", "qty": "500", "scrap": "4"}])
        self.assertTrue(r["ok"], r)
        a, b = (ShiftRun.objects.get(pk=i) for i in r["data"]["ids"])
        self.assertEqual((a.user_name, a.start, a.end, a.count), ("J. Ortiz", self.t0, self.t0 + timedelta(hours=4), 720))
        self.assertEqual((b.user_name, b.start, b.end, b.count, b.shift_name), ("M. Lee", self.t0 + timedelta(hours=4), self.t1, 720, "1st shift"))
        self.assertEqual((PpmReading.objects.filter(shift=a).count(), PpmReading.objects.filter(shift=b).count()), (240, 240))
        self.down.refresh_from_db()   # the stop over the hand-over is split between them
        self.assertEqual((self.down.shift_id, self.down.minutes), (a.pk, 10))
        self.assertEqual(Downtime.objects.get(shift=b).minutes, 20)
        self.qc.refresh_from_db()
        self.assertEqual(self.qc.shift_id, b.pk)
        self.assertEqual(a.down_mins, 10)
        lee = MoRun.objects.get(shift=b)
        self.assertEqual((lee.item, lee.qty, lee.scrap), ("CF1234", 500, 4))
        self.assertEqual(MoRun.objects.get(shift=a).qty, 720)

    def test_refusals(self):
        self.assertFalse(self.fill([])["ok"])
        r = self.fill([{"who": "J. Ortiz", "from": "06:00", "to": "11:00"}, {"who": "M. Lee", "from": "10:00", "to": "14:00"}])
        self.assertIn("overlap", r["error"])
        self.assertIn("outside", self.fill([{"who": "J. Ortiz", "from": "05:00", "to": "14:00"}])["error"])
        self.assertTrue(self.fill([{"who": "J. Ortiz", "from": "05:50", "to": "14:10"}])["ok"])   # rounded paper times
        self.assertIn("MO or the part", engine.execute("histFill", {"id": self.sh.pk, "rows": [{"who": "x", "from": "06:00", "to": "07:00",
                                                                                                "qty": "5"}]}, self.lead)["error"])
        live = ShiftRun.objects.filter(active=True).first()
        self.assertFalse(engine.execute("histFill", {"id": live.pk, "rows": [{"who": "x", "from": "06:00", "to": "07:00"}]}, self.lead)["ok"])

    def test_add_by_hand(self):
        day = (self.day + timedelta(days=1)).isoformat()
        rows = [{"who": "J. Ortiz", "from": "22:00", "to": "02:00", "mo": "999001", "qty": "400"},
                {"who": "M. Lee", "from": "02:00", "to": "06:00", "mo": "999001", "qty": "350"}]
        r = engine.execute("histAdd", {"machine": "Salvatore", "day": day, "start": "22:00", "end": "06:00", "shift_name": "3rd shift",
                                       "rows": rows, "why": "feed down"}, self.lead)
        self.assertTrue(r["ok"], r)
        a, b = (ShiftRun.objects.get(pk=i) for i in r["data"]["ids"])
        self.assertEqual((a.user_name, a.count, b.user_name, b.count), ("J. Ortiz", 400, "M. Lee", 350))
        self.assertEqual((b.end - a.start), timedelta(hours=8))   # over midnight
        self.assertFalse(a.active or b.active)
        self.assertEqual(MoRun.objects.filter(shift__in=[a, b]).count(), 2)
        again = engine.execute("histAdd", {"machine": "Salvatore", "day": day, "start": "23:00", "end": "01:00", "rows": rows[:1]}, self.lead)
        self.assertIn("already has a log", again["error"])
        clash = engine.execute("histAdd", {"machine": "Salvatore", "day": self.day.isoformat(), "start": "07:00", "end": "08:00",
                                           "rows": [{"who": "x", "from": "07:00", "to": "08:00"}]}, self.lead)
        self.assertIn("already has a log", clash["error"])

    def test_pages(self):
        c = Client(enforce_csrf_checks=True)
        c.get("/supervisor/")
        c.post("/api/hmi/login", json.dumps({"pin": "4444", "perm": "section.supervisor"}), content_type="application/json",
               HTTP_X_CSRFTOKEN=c.cookies["csrftoken"].value)
        r = c.get(f"/supervisor/build/?from={self.day.isoformat()}")
        self.assertContains(r, f"/supervisor/build/{self.sh.pk}/")
        page = c.get(f"/supervisor/build/{self.sh.pk}/")
        self.assertContains(page, "What the machine recorded")
        self.assertContains(page, 'name="who_0"')
        tok = c.cookies["csrftoken"].value
        r = c.post(f"/supervisor/build/{self.sh.pk}/", {"csrfmiddlewaretoken": tok, "who_0": "K. Patel", "from_0": "06:00", "to_0": "14:00",
                                                        "mo_0": "999001", "why": "paper"})
        self.assertEqual(r.status_code, 302)
        self.sh.refresh_from_db()
        self.assertEqual(self.sh.user_name, "K. Patel")
        r = c.post(f"/supervisor/build/{self.sh.pk}/", {"csrfmiddlewaretoken": tok, "who_0": "x", "from_0": "06:00", "to_0": "14:00", "mo_0": "999001"})
        self.assertContains(r, "already has MO run")   # shown on the page, the rows kept
        self.assertContains(r, 'value="x"')
        self.assertContains(c.get("/supervisor/build/new/"), "Add a log by hand")
        self.assertContains(c.get(f"/supervisor/log/{self.sh.pk}/"), "Fill in from paper log")
        self.assertContains(c.get("/supervisor/?tab=logs"), "History builder")
