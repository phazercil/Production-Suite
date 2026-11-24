"""A changeover ends its time when the machine runs (Admin › Rules › "A changeover is done once the machine runs for"),
and Supervisor › Downtime can give many stops one reason at once."""
import json
from datetime import timedelta

from django.core.cache import cache
from django.test import Client, TestCase
from django.utils import timezone

from hmi import engine
from hmi.models import ChangeoverPiece, Downtime, ShiftRun
from plant.demo import seed_demo
from plant.models import MO, Machine
from supervisor.models import HistoryEdit

from .test_suite import no_planned_time


class ChangeoverEndsWhenItRuns(TestCase):
    def setUp(self):
        no_planned_time(self)
        cache.clear()
        seed_demo(reset=True)
        self.c = Client()
        self.bar = Machine.objects.get(name="Barbara")
        self.c.post("/api/hmi/login", json.dumps({"pin": "1111"}), content_type="application/json")
        cmd = lambda n, p: self.c.post("/api/hmi/cmd", json.dumps({"name": n, "p": p, "line": "Barbara"}), content_type="application/json").json()   # noqa: E731
        self.cmd = cmd
        assert cmd("coOpen", {})["ok"]
        assert cmd("coEnd", {"checks": [True] * 10, "qty": 100, "scrap": 0})["ok"]
        self.t = timezone.now().replace(second=0, microsecond=0)
        sh = self.sh()
        sh.co = {**sh.co, "start": engine.ms(self.t)}
        sh.last_count = 1000
        sh.save()

    def sh(self):
        return ShiftRun.objects.get(machine=self.bar, active=True)

    def minute(self, i, run, count):
        engine.tick(self.bar, {"ppm": 200 if run else 0, "run": run, "count": count}, self.t + timedelta(minutes=i))

    def test_running_ends_the_changeover_time_and_later_stops_are_ordinary(self):
        for i in range(1, 6):
            self.minute(i, False, 1000)          # stopped: the changeover itself
        self.minute(6, True, 1010)                # one minute of running: a test run, still in changeover
        self.minute(7, False, 1010)
        self.assertEqual(self.sh().state, "changeover")
        for i, c in zip(range(8, 12), (1020, 1040, 1060, 1080)):
            self.minute(i, True, c)              # running 3+ minutes: done on the machine
        sh = self.sh()
        self.assertEqual(sh.state, "running")
        self.assertEqual(sh.co["ranAt"], engine.ms(self.t + timedelta(minutes=7)))
        piece = ChangeoverPiece.objects.get(co_id=sh.co["id"])
        self.assertEqual((piece.ended, piece.end), ("machine ran", self.t + timedelta(minutes=7)))
        for i in range(12, 16):
            self.minute(i, False, 1080)          # a stop after it ran
        self.minute(16, True, 1100)
        later = Downtime.objects.filter(shift=sh, start_time__gte=self.t + timedelta(minutes=11))
        self.assertEqual([d.reason for d in later], [None])          # not tagged Changeover
        # finishing it later keeps the time as it was, and gives the parts made meanwhile to the new MO
        r = self.cmd("coPick", {"mo": "306785"})
        self.assertTrue(r["ok"], r)
        for i in range(8):
            self.cmd("coStep", {"i": i, "checked": True})
        before = MO.objects.get(number="306785").produced
        r = self.cmd("coFinish", {})
        self.assertTrue(r["ok"], r)
        piece.refresh_from_db()
        self.assertEqual((piece.ended, piece.end), ("finished", self.t + timedelta(minutes=7)))
        self.assertEqual(ChangeoverPiece.objects.filter(co_id=piece.co_id).count(), 1)
        self.assertEqual(MO.objects.get(number="306785").produced - before, 90)   # 1100 − 1010: made since it started running

    def test_it_carries_into_the_next_log_as_running(self):
        self.minute(1, False, 1000)
        for i, c in zip(range(2, 6), (1010, 1020, 1030, 1040)):
            self.minute(i, True, c)
        sh = self.sh()
        engine.close_log(sh, self.t + timedelta(minutes=6), why="window")
        nxt = engine.open_log(self.bar, self.t + timedelta(minutes=7), co=dict(sh.co))
        self.assertEqual(nxt.state, "running")


class BatchReasons(TestCase):
    def setUp(self):
        seed_demo(reset=True)

    def test_batch_reason_change_keeps_history(self):
        sh = ShiftRun.objects.filter(line="Barbara").first()
        t = timezone.now() - timedelta(hours=3)
        ds = [Downtime.objects.create(shift=sh, line="Barbara", start_time=t + timedelta(minutes=10 * i), end_time=t + timedelta(minutes=10 * i + 4),
                                      minutes=4, reason="Changeover") for i in range(3)]
        c = Client()
        from .test_quality import sup_login
        self.assertTrue(sup_login(c, "4444")["ok"])
        page = c.get("/supervisor/?tab=down").content.decode()
        self.assertIn('id="dbatch"', page)
        r = c.post("/supervisor/down-batch", {"ids": [ds[0].pk, ds[1].pk], "reason": "Jam or misfeed", "why": "was running", "back": "tab=down"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("2+stops+set", r["Location"])
        self.assertEqual(sorted(Downtime.objects.filter(pk__in=[d.pk for d in ds]).values_list("reason", flat=True)),
                         ["Changeover", "Jam or misfeed", "Jam or misfeed"])
        self.assertEqual(HistoryEdit.objects.filter(kind="downtime", field="Reason", new="Jam or misfeed", why="was running").count(), 2)
        r = c.post("/supervisor/down-batch", {"ids": [ds[0].pk], "reason": "", "back": "tab=down"})
        self.assertIn("Pick+the+reason", r["Location"])


class NightsAndCalendar(TestCase):
    def setUp(self):
        no_planned_time(self)
        seed_demo(reset=True)
        self.bar = Machine.objects.get(name="Barbara")

    def test_changeover_left_open_overnight_is_not_downtime(self):
        t = timezone.now().replace(second=0, microsecond=0) - timedelta(hours=10)
        ShiftRun.objects.filter(machine=self.bar).update(active=False)
        log = ShiftRun.objects.create(machine=self.bar, line="Barbara", shift_name=engine.NO_SHIFT, start=t, state="changeover",
                                      co={"step": 2, "start": engine.ms(t), "done": [], "id": "x1"}, active=True)
        engine.close_log(log, t + timedelta(hours=7), why="window")
        self.assertFalse(Downtime.objects.filter(shift=log).exists())
        self.assertFalse(ChangeoverPiece.objects.filter(co_id="x1").exists())

    def test_deleting_a_changeover_clears_its_reason(self):
        import supervisor.edit  # noqa: F401 (registers the history commands)
        from plant.models import Person
        t = timezone.now() - timedelta(hours=5)
        sh = ShiftRun.objects.filter(machine=self.bar).first()
        co = ChangeoverPiece.objects.create(machine=self.bar, line="Barbara", co_id="c9", start=t, end=t + timedelta(minutes=40), minutes=40)
        d = Downtime.objects.create(shift=sh, line="Barbara", start_time=t + timedelta(minutes=5), end_time=t + timedelta(minutes=15), minutes=10, reason="Changeover")
        r = engine.execute("histDelete", {"kind": "co", "id": co.pk, "why": "wasn't one"}, engine.Ctx(user=Person.objects.get(pin="9999")))
        self.assertTrue(r["ok"], r)
        d.refresh_from_db()
        self.assertIsNone(d.reason)
        self.assertIn("cleared", r["msg"])

    def test_plant_calendar_drives_shift_windows(self):
        from plant.models import PlantDay, Shift
        from plant.schedule import book, forget
        s = Shift.objects.order_by("sort", "id").first()
        d = timezone.localdate() + timedelta(days=3)
        dow = (d.weekday() + 1) % 7
        PlantDay.objects.create(day=d, kind="closed", note="Holiday")
        forget()
        self.assertIsNone(book().on(s.id, d))
        PlantDay.objects.filter(day=d).update(kind="", shifts={str(s.id): {"on": True, "start": "09:00", "end": "13:00"}})
        forget()
        x = book().on(s.id, d)
        self.assertEqual((x["start"], x["end"]), ("09:00", "13:00"))
        self.assertIn(dow, x["days"])
        c = Client()
        c.post("/api/hmi/login", json.dumps({"pin": "9999"}), content_type="application/json")
        r = c.get(f"/schedule/?month={d:%Y-%m}&day={d.isoformat()}")
        self.assertContains(r, "09:00")
        r = c.post("/schedule/do/day", {"day": d.isoformat(), "kind": "off", "note": "weather"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(PlantDay.objects.get(day=d).kind, "off")
        r = c.post("/schedule/do/shift", {"id": s.id, "name": s.name, "start": "06:30", "end": "15:00", "days": ["1", "2"],
                                           "bLabel": ["Lunch"], "bStart": ["11:00"], "bEnd": ["11:30"]})
        s.refresh_from_db()
        self.assertEqual((s.start, s.days, s.breaks.count()), ("06:30", [1, 2], 1))
