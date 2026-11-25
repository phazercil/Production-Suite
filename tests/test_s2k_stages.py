"""S2K posting: each entry goes to the S2K MO of its own stage (S2K keeps one MO per stage of an order)."""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from hmi.models import MoRun
from packkiosk.models import PalletTag
from plant.demo import seed_demo
from plant.models import MO
from plant.mofamily import group, split_number
from supervisor import s2k


def s2k_mo(number, part, qty, remaining):
    base, rev = split_number(number)
    m = MO.objects.create(number=number, base=base, rev=rev, part=part, qty=qty, remaining=remaining, source="s2k",
                          stage=__import__("plant.mofamily", fromlist=["stage_of"]).stage_of(part))
    return group(m)


class StageMOs(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        MoRun.objects.all().delete()
        PalletTag.objects.all().delete()
        s2k_mo("312151-030", "1MG4107HP", 5000, 5000)
        s2k_mo("312152-030", "2MG4107HP", 5000, 5000)
        self.order = s2k_mo("312153-030", "MG4107HP", 5000, 5000)
        self.order.refresh_from_db()

    def test_runs_go_to_stage_1_and_pallets_to_packaging(self):
        self.assertEqual(self.order.stage, "pk")                       # the finished part's MO is the order
        now = timezone.now()
        MoRun.objects.create(line="Barbara", mo=self.order.number, item=self.order.part, qty=3000, scrap=60,
                             start_time=now - timedelta(hours=8), end_time=now - timedelta(hours=1))
        for i in (1, 2):
            PalletTag.objects.create(part="MG4107HP", mo="312153-030", line="Barbara", pallet_no=i, qty=1500, t=now - timedelta(minutes=30), packed_by="x")
        PalletTag.objects.create(part="MG4107HP", mo="312151-030", line="Barbara", pallet_no=3, qty=1000, t=now - timedelta(minutes=20), packed_by="x")
        R = s2k.rows(now - timedelta(days=1), now + timedelta(minutes=1))
        by = {x["mo"]: x for x in R}
        self.assertEqual(set(by), {"312151-030", "312153-030"}, [(x["mo"], x["stage"], x["flags"]) for x in R])
        s1, pk = by["312151-030"], by["312153-030"]
        self.assertEqual((s1["stage"], s1["inDates"], s1["floor"], s1["own"]), ("s1", 2940, 2940, 5000))
        self.assertIn("S2K shows 0 done, the floor 2,940 (+2,940 to post)", s1["flags"])
        self.assertEqual((pk["stage"], pk["inDates"], pk["floor"], pk["own"]), ("pk", 4000, 4000, 5000))
        self.assertTrue(any("1,060 more packed than Stage 1 made good" in f for f in pk["flags"]), pk["flags"])
        self.assertTrue(any("pallet tags say MO 312151-030" in f for f in pk["flags"]), pk["flags"])
        rows = list(s2k.csv_rows(R))
        self.assertEqual(rows[0][:3], ["Type", "Stage", "S2K MO"])
        self.assertIn(["MO run", "Stage 1", "312151-030", "1MG4107HP"], [r[:4] for r in rows])
        self.assertEqual(sum(1 for r in rows if r[0] == "Pallet" and r[2] == "312153-030"), 3)
        self.assertEqual(s2k.mark(R, "me", now), 4)
        self.assertEqual(s2k.rows(now - timedelta(days=1), now + timedelta(minutes=1), only_open=True), [])

    def test_stage_2_lines_go_to_the_2mg_mo(self):
        from plant.models import Machine
        Machine.objects.create(name="Foam GB", stage="gb")
        Machine.objects.create(name="Crafts", stage="ac")
        now = timezone.now()
        for line in ("Foam GB", "Crafts"):
            MoRun.objects.create(line=line, mo=self.order.number, item=self.order.part, qty=800, scrap=0,
                                 start_time=now - timedelta(hours=3), end_time=now - timedelta(hours=1))
        R = s2k.rows(now - timedelta(days=1), now + timedelta(minutes=1))
        self.assertEqual([(x["mo"], x["stage"], x["inDates"]) for x in R], [("312152-030", "eb", 800)])   # Arts & Crafts: no S2K MO

    def test_changes_after_posting_are_corrections(self):
        from scheduler.models import Job
        from plant.models import Machine
        now = timezone.now()
        jsr = Machine.objects.create(name="JSR2", stage="eb")
        j = Job.objects.create(mo=self.order, op="eb", machine=jsr, req_start=now - timedelta(hours=5),
                               wip_started=now - timedelta(hours=4), wip_qty=900, wip_done=now - timedelta(hours=2))
        p = PalletTag.objects.create(part="MG4107HP", mo="312153-030", line="Barbara", pallet_no=9, qty=1500, t=now - timedelta(minutes=30), packed_by="x")
        win = (now - timedelta(days=1), now + timedelta(minutes=1))
        self.assertEqual(s2k.mark(s2k.rows(*win), "me", now), 2)
        self.assertEqual(s2k.rows(*win, only_open=True), [])
        # undo the station job, fix its quantity, complete it again; void the pallet
        Job.objects.filter(pk=j.pk).update(wip_qty=950)
        PalletTag.objects.filter(pk=p.pk).update(voided=True)
        R = s2k.rows(*win, only_open=True)
        es = {e["kind"]: e for x in R for e in x["entries"] if not e["posted"]}
        self.assertEqual((es["eb"]["qty"], es["eb"]["label"]), (50, "End bands · correction"))
        self.assertEqual((es["pallet"]["qty"], es["pallet"]["label"]), (-1500, "Pallet · reversal"))
        self.assertTrue(any("Pallet · reversal" in r[0] for r in s2k.csv_rows(R)))
        self.assertEqual(s2k.mark(R, "me", now), 2)
        self.assertEqual(s2k.rows(*win, only_open=True), [])
        # an undone completion that's never redone: reversed in full, even outside the dates shown
        Job.objects.filter(pk=j.pk).update(wip_done=None)
        R = s2k.rows(now - timedelta(days=30), now - timedelta(days=29), only_open=True)
        self.assertEqual([e["qty"] for x in R for e in x["entries"]], [-950])

    def test_missing_stage_mo_is_blocked_not_posted_elsewhere(self):
        from plant.models import Machine
        Machine.objects.create(name="Foam GB2", stage="gb")
        MO.objects.filter(stage="eb").delete()       # the order has no Stage 2 MO from S2K
        now = timezone.now()
        MoRun.objects.create(line="Foam GB2", mo=self.order.number, item=self.order.part, qty=500, scrap=0,
                             start_time=now - timedelta(hours=3), end_time=now - timedelta(hours=1))
        R = s2k.rows(now - timedelta(days=1), now + timedelta(minutes=1))
        self.assertEqual(len(R), 1)
        self.assertIsNone(R[0]["m"])
        self.assertIn("has no Stage 2 MO", R[0]["blocked"])
        self.assertEqual(list(s2k.csv_rows(R))[1:], [])
        self.assertEqual(s2k.mark(R, "me", now), 0)
