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
