"""Admin › Machines: a tag per other system (PIM, …) for each machine, used in saved queries as :tag_<system>."""
from django.test import TestCase

from hmi import engine
from plant.demo import seed_demo
from plant.models import Machine, Person


class MachineTagTests(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.ctx = engine.Ctx(user=Person.objects.get(name="Plant Admin"))

    def run_cmd(self, name, **p):
        return engine.execute(name, p, self.ctx)

    def test_systems_tags_and_query_params(self):
        self.assertTrue(self.run_cmd("tagSysAdd", v="PIM")["ok"])
        self.assertFalse(self.run_cmd("tagSysAdd", v="pim")["ok"])          # already there
        self.assertFalse(self.run_cmd("tagSysAdd", v="S2K")["ok"])          # that's the process code
        self.assertTrue(self.run_cmd("lineSet", id="Barbara", f="tag", sys="PIM", v=" A-1023 ")["ok"])
        self.assertFalse(self.run_cmd("lineSet", id="Barbara", f="tag", sys="Nope", v="x")["ok"])
        bar = Machine.objects.get(name="Barbara")
        self.assertEqual(bar.tags, {"PIM": "A-1023"})
        self.assertEqual(engine.machine_params(bar), {"line_code": bar.code, "tag_pim": "A-1023"})
        self.assertEqual(engine.machine_params(Machine.objects.get(name="Salvatore"))["tag_pim"], "")
        self.assertTrue(self.run_cmd("lineSet", id="Barbara", f="tag", sys="PIM", v="")["ok"])   # blank clears it
        self.assertEqual(Machine.objects.get(name="Barbara").tags, {})
        self.run_cmd("lineSet", id="Barbara", f="tag", sys="PIM", v="A-1023")
        self.assertTrue(self.run_cmd("tagSysDel", sys="PIM")["ok"])
        self.assertEqual(Machine.objects.get(name="Barbara").tags, {})
        self.assertEqual(engine.machine_params(Machine.objects.get(name="Barbara")), {"line_code": bar.code})
