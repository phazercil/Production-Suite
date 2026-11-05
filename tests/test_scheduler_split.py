"""Scheduler: splitting a stage in two (at a time of day, by quantity, or when the operator ends the MO)."""
import json

from django.core.cache import cache
from django.test import Client, TestCase

from plant.demo import seed_demo
from plant.models import MO, Machine
from plant.util import ms
from scheduler.engine import compute, load_world
from scheduler.models import Job


def post(c, path, data):
    return c.post(path, json.dumps(data), content_type="application/json").json()


class SplitTests(TestCase):
    def setUp(self):
        cache.clear()
        seed_demo(reset=True)
        self.c = Client()
        assert post(self.c, "/api/hmi/login", {"pin": "9999"})["ok"]
        self.sal = Machine.objects.get(name="Salvatore")
        # an open MO nobody is running, alone on its stage
        self.j = Job.objects.filter(op="s1", machine=self.sal).select_related("mo").order_by("req_start").first()
        self.mo = self.j.mo
        self.total = self.mo.qty - self.mo.produced

    def res(self):
        return compute(load_world())["res"]

    def test_by_quantity(self):
        k = self.j.key
        r = post(self.c, "/api/scheduler/job/split", {"key": k, "mode": "qty", "qty": self.total})
        self.assertFalse(r["ok"])                                    # part 1 can't make everything
        r = post(self.c, "/api/scheduler/job/split", {"key": k, "mode": "qty", "qty": 1000})
        self.assertTrue(r["ok"], r)
        res = self.res()
        p1, p2 = res[k], res[k + "__2"]
        self.assertEqual((p1["qty"], p2["qty"]), (1000, self.total - 1000))
        self.assertEqual((p1["part"], p1["parts"], p2["part"]), (1, 2, 2))
        self.assertGreaterEqual(p2["start"], p1["end"])               # part 2 starts after part 1
        # part 2 moves on its own, to another line
        bar = Machine.objects.get(name="Barbara")
        r = post(self.c, "/api/scheduler/job", {"moId": self.mo.id, "op": "s1", "part": 2, "machineId": bar.id, "reqStart": p1["end"] + 86_400_000})
        self.assertTrue(r["ok"], r)
        self.assertEqual(Job.objects.get(mo=self.mo, op="s1", part=2).machine, bar)
        # undo: one run again
        self.assertTrue(post(self.c, "/api/scheduler/job/merge", {"key": k})["ok"])
        res = self.res()
        self.assertNotIn(k + "__2", res)
        self.assertEqual(res[k]["qty"], self.total)

    def test_at_a_time_of_day(self):
        k = self.j.key
        r1 = self.res()[k]
        r = post(self.c, "/api/scheduler/job/split", {"key": k, "mode": "time", "at": r1["end"] + 3_600_000})
        self.assertFalse(r["ok"])                                    # after the run: nothing to split
        at = r1["runStart"] + (r1["end"] - r1["runStart"]) // 2
        r = post(self.c, "/api/scheduler/job/split", {"key": k, "mode": "time", "at": at})
        self.assertTrue(r["ok"], r)
        res = self.res()
        p1, p2 = res[k], res[k + "__2"]
        self.assertLessEqual(p1["end"], at)
        self.assertGreater(p1["qty"], 0)
        self.assertAlmostEqual(p1["qty"] + p2["qty"], self.total, delta=1)

    def test_when_the_operator_ends_it(self):
        k = self.j.key
        r = post(self.c, "/api/scheduler/job/split", {"key": k, "mode": "operator"})
        self.assertTrue(r["ok"], r)
        res = self.res()
        self.assertEqual(res[k]["qty"], self.total)                  # part 1 runs until the operator ends it
        self.assertTrue(res[k + "__2"]["rest"])                      # part 2: the rest, once that happens
        # the operator ends the MO at the kiosk with 700 made: part 2 makes what is left
        MO.objects.filter(pk=self.mo.pk).update(status="CLOSE", produced=self.mo.produced + 700)
        res = self.res()
        self.assertNotIn(k, res)
        self.assertEqual(res[k + "__2"]["qty"], self.total - 700)
        self.assertFalse(res[k + "__2"]["rest"])
        # later stages can't be ended by an operator
        eb = Job.objects.filter(op="eb").first()
        if eb:
            r = post(self.c, "/api/scheduler/job/split", {"key": eb.key, "mode": "operator"})
            self.assertIn("Only Stage 1", r["error"])

    def test_deleting_part_2_merges_back(self):
        k = self.j.key
        post(self.c, "/api/scheduler/job/split", {"key": k, "mode": "qty", "qty": 500})
        self.assertTrue(post(self.c, "/api/scheduler/job/delete", {"key": k + "__2"})["ok"])
        self.assertEqual(Job.objects.filter(mo=self.mo, op="s1").count(), 1)
        self.assertEqual(Job.objects.get(mo=self.mo, op="s1").split_mode, "")
        self.assertEqual(ms(Job.objects.get(mo=self.mo, op="s1").req_start), ms(self.j.req_start))
