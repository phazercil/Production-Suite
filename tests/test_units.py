"""Machine speed units: pleats per minute, linear mm per second, parts per minute."""
from django.test import Client, TestCase

from plant import units
from plant.demo import seed_demo
from plant.models import Machine
from scheduler.engine import rate_for


class UnitTests(TestCase):
    def test_per_part(self):
        spec = {"pleats": 30, "ph": 1.0}
        self.assertEqual(units.per_part({"unit": "ppm"}, spec), 30)
        self.assertEqual(units.per_part({"unit": "ppm"}, spec, pleats=20), 20)          # a run's own pleat count
        self.assertEqual(units.per_part({"unit": "pcs"}, spec), 1)
        self.assertAlmostEqual(units.per_part({"unit": "mms"}, spec), 30 * 2 * 25.4 / 60)  # media of one pleat pack
        self.assertAlmostEqual(units.per_part({"unit": "mms", "mmPerPart": 300}, spec), 5)
        self.assertEqual(units.per_part({"unit": "mms"}, {"pleats": 30}), 0)             # no pleat height: unknown
        self.assertEqual(units.short({"unit": "nope"}), "P/m")

    def test_scheduler_rate(self):
        mo = {"part": "MG3045", "pleats": 30}
        item = {"pleats": 30}
        self.assertEqual(rate_for({"stage": "s1", "mode": "fixed", "ppm": 180, "unit": "ppm"}, item, mo)[0], 180 * 60 / 30)
        self.assertEqual(rate_for({"stage": "s1", "mode": "fixed", "ppm": 12, "unit": "pcs"}, item, mo)[0], 720)
        r, basis = rate_for({"stage": "s1", "mode": "fixed", "ppm": 50, "unit": "mms", "mmPerPart": 250}, item, mo)
        self.assertAlmostEqual(r, 50 * 3600 / 250)
        self.assertIn("mm/s", basis)


class AdminUnitTests(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.c = Client(HTTP_HX_REQUEST="true")
        self.c.post("/kiosk/do/login", {"pin": "9999"})

    def set(self, f, v):
        return self.c.post("/kiosk/do/run", {"c": "lineSet", "id": "Barbara", "f": f, "v": v})

    def test_set_in_admin_machines(self):
        r = self.c.get("/kiosk/v/admin?tab=machines")
        self.assertContains(r, "Linear mm per second")
        self.set("unit", "mms")
        self.set("mmpp", "420")
        m = Machine.objects.get(name="Barbara")
        self.assertEqual((m.speed_unit, m.mm_per_part, m.speed_short), ("mms", 420, "mm/s"))
        self.set("unit", "furlongs")
        self.assertEqual(Machine.objects.get(name="Barbara").speed_unit, "mms")
        self.set("mmpp", "")
        self.assertIsNone(Machine.objects.get(name="Barbara").mm_per_part)
        r = self.c.get("/kiosk/v/status")
        self.assertContains(r, "mm/s")


class CountingTests(TestCase):
    def test_speed_turns_into_parts_in_the_machines_unit(self):
        from hmi import engine
        from hmi.models import ShiftRun
        seed_demo(reset=True)
        sh = ShiftRun.objects.select_related("machine", "mo").get(line="Barbara", active=True)
        pleats = engine.spec_for(sh.mo.part)["pleats"]
        self.assertEqual(engine.pleats_for(sh), pleats)
        Machine.objects.filter(pk=sh.machine_id).update(speed_unit="pcs")
        sh = ShiftRun.objects.select_related("machine", "mo").get(pk=sh.pk)
        self.assertEqual(engine.pleats_for(sh), 1)
        Machine.objects.filter(pk=sh.machine_id).update(speed_unit="mms", mm_per_part=600)
        sh = ShiftRun.objects.select_related("machine", "mo").get(pk=sh.pk)
        self.assertEqual(engine.pleats_for(sh), 10)                 # 600 mm per part at mm/s: 10 speed-minutes
