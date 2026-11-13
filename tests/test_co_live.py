"""A changeover doesn't stop the machine's record: speed and parts are kept as they are, and only the time the line
is actually stopped during a changeover is Changeover downtime. Stops without a reason: the operator screens ask
about the shift running now; earlier shifts' are on the Admin page."""
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase
from django.utils import timezone

from hmi import engine
from hmi.models import Downtime, PpmReading, ShiftRun
from plant.demo import seed_demo
from plant.models import Machine, Person


class ChangeoverLiveTests(TestCase):
    def setUp(self):
        cache.clear()
        seed_demo(reset=True)
        self.m = Machine.objects.get(name="Barbara")
        self.sh = ShiftRun.objects.get(line="Barbara", active=True)
        self.t = timezone.now().replace(second=0, microsecond=0) + timedelta(minutes=2)
        engine._planned_cache.clear()
        for p in (mock.patch.object(engine, "planned_for_tick", return_value=[]),         # no break in the way at this hour
                  mock.patch.object(engine, "roll_over", side_effect=lambda sh, t: sh)):  # nor a shift change
            p.start()
            self.addCleanup(p.stop)
        self.lead = Person.objects.get(pin="9999")

    def tick(self, i, ppm=190):
        engine.tick(self.m, {"ppm": ppm, "run": ppm > 0}, self.t + timedelta(minutes=i))

    def cmd(self, c, p, i):
        return engine.execute(c, p, engine.Ctx(user=self.lead, line="Barbara", t=self.t + timedelta(minutes=i)))

    def co_downs(self):
        return list(Downtime.objects.filter(shift__line="Barbara", reason="Changeover", start_time__gte=self.t - timedelta(minutes=5))
                    .order_by("start_time").values_list("start_time", "end_time"))

    def test_rate_keeps_showing_and_only_stops_are_changeover(self):
        self.tick(0)
        self.assertTrue(self.cmd("coOpen", {}, 1)["ok"])
        self.assertTrue(self.cmd("coEnd", {"checks": [True] * 10, "qty": 10}, 1)["ok"])
        before = ShiftRun.objects.get(pk=self.sh.pk).count
        for i in range(1, 4):
            self.tick(i)                                             # still running: running out the last parts
        r = PpmReading.objects.filter(shift_id=self.sh.pk).latest("t")
        self.assertEqual((r.ppm, r.run), (190, True))                 # the real speed, not 0
        sh = ShiftRun.objects.get(pk=self.sh.pk)
        self.assertEqual(sh.state, "changeover")
        self.assertGreater(sh.count, before)                          # what it made is counted
        for i in range(4, 12):
            self.tick(i, ppm=0)                                       # stopped to change over
        self.assertEqual(ShiftRun.objects.get(pk=self.sh.pk).state, "changeover")   # (not "stopped": no stop alert)
        for i in range(12, 15):
            self.tick(i)                                              # a test run
        downs = self.co_downs()
        self.assertEqual(len(downs), 1)                               # only the stopped stretch
        s, e = downs[0]
        self.assertEqual((s, e), (self.t + timedelta(minutes=3), self.t + timedelta(minutes=11)))

    def test_a_stop_going_on_is_closed_as_changeover_when_it_finishes(self):
        self.tick(0)
        for i in range(1, 4):
            self.tick(i, ppm=0)                                       # stopped before Change MO was pressed
        self.assertTrue(self.cmd("coOpen", {}, 4)["ok"])
        self.assertTrue(self.cmd("coEnd", {"checks": [True] * 10, "qty": 10}, 4)["ok"])
        for i in range(4, 9):
            self.tick(i, ppm=0)
        sh = ShiftRun.objects.get(pk=self.sh.pk)
        n = len(sh.co.get("steps") or engine.config()["coSteps"])
        sh.co = {**sh.co, "step": 3, "newMo": sh.co.get("oldMo"), "done": list(range(n))}
        sh.save(update_fields=["co"])
        from plant.models import MO
        MO.objects.filter(pk=sh.co["newMo"]).update(status="OPEN")
        r = self.cmd("coFinish", {}, 9)
        self.assertTrue(r["ok"], r)
        downs = self.co_downs()
        self.assertEqual(downs, [(self.t, self.t + timedelta(minutes=9))])   # the whole stop, as Changeover
        self.assertFalse(Downtime.objects.filter(shift_id=self.sh.pk, reason__isnull=True, start_time__gte=self.t).exists())

    def test_no_downtime_when_the_line_never_stops(self):
        self.tick(0)
        self.cmd("coOpen", {}, 1)
        self.cmd("coEnd", {"checks": [True] * 10, "qty": 10}, 1)
        for i in range(1, 6):
            self.tick(i)
        self.assertEqual(self.co_downs(), [])


class ReasonsByShiftTests(TestCase):
    def setUp(self):
        cache.clear()
        seed_demo(reset=True)
        self.sh = ShiftRun.objects.get(line="Barbara", active=True)
        now = timezone.now()
        self.old_sh = ShiftRun.objects.create(machine=self.sh.machine, line="Barbara", shift_name="Shift 2", active=False,
                                              start=now - timedelta(hours=20), end=now - timedelta(hours=12))
        Downtime.objects.filter(reason__isnull=True).delete()
        self.mine = Downtime.objects.create(shift=self.sh, line="Barbara", start_time=now - timedelta(minutes=40),
                                            end_time=now - timedelta(minutes=20), minutes=20)
        self.old = Downtime.objects.create(shift=self.old_sh, line="Barbara", start_time=now - timedelta(hours=15),
                                           end_time=now - timedelta(hours=14), minutes=60)
        self.c = Client(HTTP_HX_REQUEST="true")
        self.c.post("/kiosk/do/station", {"line": "Barbara"})
        self.c.post("/kiosk/do/login", {"pin": "9999"})              # an admin, on the operator screens

    def test_operator_screens_ask_about_this_shift_only(self):
        r = self.c.get("/kiosk/m/downPrompt")
        self.assertEqual([x["d"].pk for x in r.context["rows"]], [self.mine.pk])
        r = self.c.get("/kiosk/v/downtime")
        self.assertNotContains(r, "Earlier shifts")
        r = self.c.get("/kiosk/v/admin?tab=reasons")
        self.assertContains(r, "Earlier shifts' stops without a reason")
        self.assertEqual([x["d"].pk for x in r.context["A"]["older"]], [self.old.pk])


class PackagingKioskTests(TestCase):
    def test_in_the_top_bar_for_later(self):
        seed_demo(reset=True)
        r = Client().get("/packaging/")
        self.assertContains(r, "Packaging Kiosk")
        self.assertContains(r, "coming in a later update")
        self.assertContains(Client().get("/supervisor/"), 'href="/packaging/"')
