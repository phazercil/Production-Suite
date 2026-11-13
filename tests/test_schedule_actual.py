"""The Scheduler against what really happens on the floor: the MO running now is re-planned at the speed it is
really running, with the time saved or lost; what already ran shows on each line; MOs carry gross, net and scrap."""
import json
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase

from hmi.models import MoRun, ShiftRun
from plant.demo import seed_demo
from plant.models import MO, Machine
from plant.util import MIN, ms, now
from scheduler import engine
from scheduler.engine import actual_pace, compute, load_world

from .test_suite import no_planned_time


class _AllWork:
    """A calendar where every minute is shift time (so the test doesn't depend on the time of day)."""
    def between(self, m, a, b):
        return [(a, b)]


def all_work():
    return mock.patch.object(engine.Calendar, "between", lambda self, m, a, b: [(a, b)])


class PaceTests(TestCase):
    def setUp(self):
        no_planned_time(self)
        cache.clear()
        seed_demo(reset=True)
        self.run = MO.objects.get(number="309215")
        self.sh = ShiftRun.objects.get(machine__name="Barbara", active=True)

    def test_pace_from_what_was_made(self):
        act = {"made": 600, "spans": [(0, 60 * MIN)]}
        p = actual_pace(_AllWork(), {}, act, eff=300)
        self.assertEqual((round(p["eff"]), p["workMin"], p["savedMin"]), (600, 60, 60))      # twice as fast: an hour saved
        p = actual_pace(_AllWork(), {}, {"made": 150, "spans": [(0, 60 * MIN)]}, eff=300)
        self.assertEqual(p["savedMin"], -30)                                                  # half as fast: 30 min lost
        self.assertIsNone(actual_pace(_AllWork(), {}, {"made": 100, "spans": [(0, 10 * MIN)]}, eff=300))   # too soon to say

    def key(self):
        return f"{self.run.id}__s1"

    def test_running_mo_is_replanned_at_its_real_speed(self):
        with all_work():
            base = compute(load_world())["res"][self.key()]
            eff = base["eff"]
            # it has run 2 hours making twice what the plan says
            ShiftRun.objects.filter(pk=self.sh.pk).update(mo_start=now() - timedelta(hours=2), mo_count=int(eff * 4))
            MoRun.objects.filter(mo=self.run.number).delete()
            r = compute(load_world())["res"][self.key()]
        self.assertIsNotNone(r["pace"])
        self.assertAlmostEqual(r["pace"]["eff"] / eff, 2, delta=0.05)
        self.assertAlmostEqual(r["runMin"], r["gross"] / (eff * 2) * 60, delta=1)
        self.assertGreater(r["pace"]["savedMin"], 110)                                        # ~2 hours saved
        self.assertGreater(r["pace"]["finishMin"], 0)                                         # finishes sooner
        with all_work():
            ShiftRun.objects.filter(pk=self.sh.pk).update(mo_count=int(eff))                  # half the planned speed
            r = compute(load_world())["res"][self.key()]
        self.assertLess(r["pace"]["savedMin"], 0)
        self.assertLess(r["pace"]["finishMin"], 0)                                            # finishes later

    def test_earlier_shifts_count_too(self):
        with all_work():
            eff = compute(load_world())["res"][self.key()]["eff"]
            MoRun.objects.filter(mo=self.run.number).delete()
            ShiftRun.objects.filter(pk=self.sh.pk).update(mo_start=now() - timedelta(minutes=5), mo_count=0)
            t = now() - timedelta(hours=10)
            MoRun.objects.create(line="Barbara", mo=self.run.number, item=self.run.part, qty=int(eff * 2), start_time=t,
                                 end_time=t + timedelta(hours=2))
            r = compute(load_world())["res"][self.key()]
        self.assertAlmostEqual(r["pace"]["workMin"], 125, delta=2)
        self.assertAlmostEqual(r["pace"]["savedMin"], -5, delta=3)                            # on plan, less the 5 min now

    def test_remaining_is_good_filters(self):
        MO.objects.filter(pk=self.run.pk).update(produced=1000, scrap=100)
        r = compute(load_world())["res"][self.key()]
        self.assertEqual(r["qty"], self.run.qty - 900)


class HistoryTests(TestCase):
    def setUp(self):
        no_planned_time(self)
        cache.clear()
        seed_demo(reset=True)
        self.c = Client()
        self.c.post("/api/hmi/login", json.dumps({"pin": "9999"}), content_type="application/json")
        self.run = MO.objects.get(number="309215")

    def test_board_shows_what_ran_with_gross_net_scrap_and_time(self):
        t = now() - timedelta(hours=20)
        MoRun.objects.create(line="Barbara", mo=self.run.number, item=self.run.part, qty=1200, scrap=30, start_time=t,
                             end_time=t + timedelta(hours=3))
        a, b = ms(now() - timedelta(days=2)), ms(now() + timedelta(days=2))
        with all_work():
            j = self.c.get(f"/api/scheduler/state?from={a}&to={b}").json()
        bar = Machine.objects.get(name="Barbara")
        hist = [h for h in j["hist"] if h["machineId"] == bar.id]
        done = next(h for h in hist if not h["now"] and h["gross"] == 1200)
        self.assertEqual((done["net"], done["scrap"], done["workMin"]), (1170, 30, 180))
        self.assertIsNotNone(done["savedMin"])
        self.assertTrue(any(h["now"] for h in hist))                                            # and the run going on now
        mo = next(m for m in j["mos"] if m["mo"] == self.run.s2k_number)
        self.assertEqual((mo["produced"], mo["scrapped"], mo["net"]), (self.run.produced, self.run.scrap, self.run.produced - self.run.scrap))
        self.assertIn("pace", next(iter(j["res"].values())))
