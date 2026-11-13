"""S2K orders with one MO per stage (1MG… pleating, 2MG… end bands, the finished part packaging), merged into one
order on the Scheduler, and S2K's revisions (306556 → 306556-001) updating the MO on file."""
import json
from datetime import timedelta

from django.core.cache import cache
from django.test import Client, TestCase
from django.utils import timezone

from hmi import engine
from plant import mofamily
from plant.demo import seed_demo
from plant.models import MO, Machine, Person
from plant.util import Catalog, ms
from scheduler.engine import compute, load_world
from scheduler.models import Job


def rows(n1, n2, nf, part="MG4107HP", rev="030", q1=5000, q2=5000, qf=5000, due="2026-12-01"):
    return [{"mo": f"{n1}-{rev}", "item": f"1{part}", "qty": q1, "due": due},
            {"mo": f"{n2}-{rev}", "item": f"2{part}", "qty": q2, "due": due},
            {"mo": f"{nf}-{rev}", "item": part, "qty": qf, "due": due}]


class FamilyTests(TestCase):
    def setUp(self):
        cache.clear()
        seed_demo(reset=True)
        self.ctx = engine.Ctx(user=Person.objects.get(pin="9999"))

    def test_numbers_and_stages(self):
        self.assertEqual(mofamily.split_number("306556-005"), ("306556", 5))
        self.assertEqual(mofamily.split_number("306556"), ("306556", 0))
        self.assertEqual(mofamily.split_number("NOMO-BARB-1"), ("NOMO-BARB-1", 0))
        self.assertEqual([mofamily.stage_of(p) for p in ("1MG4062", "2MG4062", "MG4062", "1AQ1278C", "AQ1097KIT")], ["s1", "eb", "pk", "s1", "pk"])

    def test_three_stage_mos_are_one_order(self):
        added, updated, skipped = mofamily.upsert(rows(312151, 312152, 312153))
        self.assertEqual((added, updated, skipped), (3, 0, 0))
        order = MO.objects.get(number="312153-030")
        self.assertIsNone(order.parent)
        self.assertEqual(sorted(MO.objects.filter(parent=order).values_list("number", flat=True)), ["312151-030", "312152-030"])
        self.assertEqual({k for k, v in order.ops.items() if v} & {"s1", "eb", "pk"}, {"s1", "eb", "pk"})
        self.assertEqual(order.stage_qty, {"s1": 5000, "eb": 5000, "pk": 5000})
        self.assertEqual(order.floor_number, "312151-030")             # what the pleater posts against
        self.assertEqual(order.stage_number("eb"), "312152-030")
        w = load_world()
        self.assertIn(order.id, w["mos"])
        self.assertFalse({m.id for m in MO.objects.filter(parent=order)} & set(w["mos"]))   # stage MOs aren't orders of their own
        self.assertEqual(w["mos"][order.id]["stageMos"]["s1"]["mo"], "312151-030")

    def test_finished_part_first_and_two_per_product(self):
        r = rows(400101, 400102, 400103, part="MG3012", q1=400, q2=400, qf=200)
        mofamily.upsert([r[2]])                                            # S2K may list the finished part first
        mofamily.upsert(r[:2])
        order = MO.objects.get(number="400103-030")
        self.assertEqual(order.stage_qty, {"s1": 400, "eb": 400, "pk": 200})
        self.assertEqual(order.qty, 400)                                   # the pleater makes 2 per finished filter
        self.assertEqual(MO.objects.filter(parent=order).count(), 2)

    def test_stage_one_first_hands_its_schedule_to_the_order(self):
        r = rows(500001, 500002, 500003)
        mofamily.upsert(r[:1])
        s1 = MO.objects.get(number="500001-030")
        bar = Machine.objects.get(name="Barbara")
        Job.objects.create(mo=s1, op="s1", machine=bar, req_start=timezone.now() + timedelta(days=3))
        mofamily.upsert(r[1:])
        order = MO.objects.get(number="500003-030")
        s1.refresh_from_db()
        self.assertEqual(s1.parent, order)
        self.assertTrue(Job.objects.filter(mo=order, op="s1", machine=bar).exists())   # the board kept its slot

    def test_revisions_update_the_mo_on_file(self):
        mofamily.upsert(rows(312151, 312152, 312153))
        order = MO.objects.get(number="312153-030")
        added, updated, _ = mofamily.upsert(rows(312151, 312152, 312153, rev="031", q1=4200, q2=5000, qf=5000))
        self.assertEqual((added, updated), (0, 3))                         # each stage MO moved to revision 031
        s1 = MO.objects.get(base="312151")
        self.assertEqual((s1.number, s1.rev, s1.remaining, s1.qty), ("312151-030", 31, 4200, 5000))   # records still match its number
        self.assertEqual(s1.s2k_number, "312151-031")
        order.refresh_from_db()
        self.assertEqual(order.floor_number, "312151-031")
        self.assertEqual(mofamily.upsert(rows(312151, 312152, 312153, rev="029", q1=1))[:2], (0, 0))   # an older revision changes nothing
        self.assertEqual(MO.objects.filter(base__in=["312151", "312152", "312153"]).count(), 3)

    def test_a_revision_of_a_plain_mo(self):
        mofamily.upsert([{"mo": "306556", "item": "MG4091HP", "qty": 1000}])
        mofamily.upsert([{"mo": "306556-001", "item": "MG4091HP", "qty": 800}])
        mo = MO.objects.get(base="306556")
        self.assertEqual((mo.number, mo.rev, mo.remaining, mo.qty, mo.s2k_number), ("306556", 1, 800, 1000, "306556-001"))

    def test_separate_orders_of_the_same_part(self):
        mofamily.upsert(rows(312151, 312152, 312153))
        mofamily.upsert(rows(318001, 318002, 318003))
        self.assertEqual(MO.objects.filter(parent__isnull=True, base__in=["312153", "318003"]).count(), 2)
        self.assertEqual(MO.objects.get(base="318001").parent.base, "318003")

    def test_scanning_the_stage_mo_starts_the_order(self):
        mofamily.upsert(rows(312151, 312152, 312153))
        order = MO.objects.get(number="312153-030")
        self.assertEqual(engine.open_mo("312151-030"), order)
        self.assertEqual(engine.open_mo("312151-031"), order)             # a later revision's label still works
        self.assertEqual(engine.open_mo("312153-030"), order)

    def test_s2k_pull_uses_it(self):
        engine.import_mos(self.ctx, rows(312151, 312152, 312153))
        self.assertEqual(MO.objects.filter(base__in=["312151", "312152", "312153"], parent__isnull=True).count(), 1)

    def test_cost_sheet_matches_stage_parts(self):
        cat = Catalog()
        it = cat.items[0]
        key = (it["cust"] or it["aq"]).upper()
        self.assertEqual(cat.matches("1" + key)[0]["uid"], it["uid"])


class PackagingScheduleTests(TestCase):
    def setUp(self):
        cache.clear()
        seed_demo(reset=True)
        self.c = Client()
        self.c.post("/api/hmi/login", json.dumps({"pin": "9999"}), content_type="application/json")
        self.pk = Machine.objects.create(name="Pack 1", stage="pk", rate=300, speed_mode="fixed")
        self.pk.shifts.set(Machine.objects.get(name="William").shifts.all())

    def test_packaging_follows_end_bands_with_its_own_quantity(self):
        mofamily.upsert(rows(600001, 600002, 600003, part="MG3012", q1=400, q2=400, qf=200))
        order = MO.objects.get(number="600003-030")
        t0 = timezone.now() + timedelta(days=1)
        Job.objects.create(mo=order, op="s1", machine=Machine.objects.get(name="Barbara"), req_start=t0)
        Job.objects.create(mo=order, op="eb", machine=Machine.objects.get(name="William"), req_start=t0)
        Job.objects.create(mo=order, op="pk", machine=self.pk, req_start=t0)
        res = {r["op"]: r for r in compute(load_world())["res"].values() if r["moId"] == order.id}
        self.assertEqual((res["s1"]["qty"], res["eb"]["qty"], res["pk"]["qty"]), (400, 400, 200))
        self.assertGreaterEqual(res["pk"]["end"], res["eb"]["end"])          # packed after the end bands
        self.assertFalse(res["pk"]["waiting"])
        st = self.c.get("/api/scheduler/state").json()
        self.assertIn("pk", st["modes"])

    def test_packaging_only_order_needs_no_stage_one(self):
        mofamily.upsert([{"mo": "700003", "item": "MG3012", "qty": 50, "due": "2026-12-01"}])
        order = MO.objects.get(number="700003")
        self.assertEqual({k for k, v in order.ops.items() if v}, {"pk"})
        Job.objects.create(mo=order, op="pk", machine=self.pk, req_start=timezone.now() + timedelta(hours=2))
        r = next(r for r in compute(load_world())["res"].values() if r["moId"] == order.id)
        self.assertFalse(r["waiting"])
        self.assertEqual(r["qty"], 50)


class PullIntervalTests(TestCase):
    """Admin › Connections › Pulls every: an interval for each connection the server reads on a timer."""

    def setUp(self):
        cache.clear()
        seed_demo(reset=True)
        self.c = Client(HTTP_HX_REQUEST="true")
        self.c.post("/kiosk/do/login", {"pin": "9999"})

    def test_each_timed_connection_has_its_interval(self):
        r = self.c.get("/kiosk/v/admin?tab=connections")
        self.assertContains(r, "Pulls every")
        for k in ("s2kEvery", "peopleEvery", "paycom.every", "assetsEvery"):
            self.assertContains(r, f'"k": "{k}"')
        self.assertContains(r, "every minute (each minute is counted)")   # machine data: the counting minute
        lead = engine.Ctx(user=Person.objects.get(pin="9999"))
        self.assertTrue(engine.execute("settingSet", {"k": "s2kEvery", "v": 15}, lead)["ok"])
        self.assertIn("0 to 1440", engine.execute("settingSet", {"k": "s2kEvery", "v": 5000}, lead)["error"])
        self.assertTrue(engine.execute("settingSet", {"k": "influxEvery", "v": 0}, lead)["ok"])
        from plant.models import PlantSettings
        s = PlantSettings.load().settings
        self.assertEqual((s["s2kEvery"], s["influxEvery"]), (15, 1))       # InfluxDB copies at least once a minute

    def test_drawing_reread_interval(self):
        from unittest import mock
        lead = engine.Ctx(user=Person.objects.get(pin="9999"))
        with mock.patch.object(engine, "pull_drawing") as pull:
            engine.refresh_part("MG4062")
            engine.refresh_part("MG4062")
            self.assertEqual(pull.call_count, 1)                           # once a minute by default
            engine.execute("settingSet", {"k": "assetsEvery", "v": 0}, lead)
            cache.clear()
            engine.refresh_part("MG4062")
            engine.refresh_part("MG4062")
            self.assertEqual(pull.call_count, 3)                           # 0: every time it's needed
