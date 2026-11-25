"""Changeover steps and settings come from approved work instructions kept in the suite (no outside database), with
process parameters worked out from the filter's own data: [Pleat height (mm)] + 1."""
import json

from django.core.cache import cache
from django.test import Client, TestCase, TransactionTestCase

from hmi import engine
from hmi.models import ChangeoverCache, ShiftRun
from instructions import formula
from instructions.models import Ack, Instruction
from plant.demo import seed_demo
from plant.models import MO, FilterModel3D, Machine, PartSpec, Person

from .test_suite import no_planned_time


class Formulas(TestCase):
    def setUp(self):
        PartSpec.objects.create(key="MG9001", ph=1.25, len=9.5, pleats=40)
        FilterModel3D.objects.create(pid="MG9001", part="MG9001", flat={"pleat_height_mm": 30.5, "pleat_count": 40, "media_type": "synthetic"})

    def test_values_and_math(self):
        vals = formula.values_for("MG9001")
        self.assertEqual(formula.evaluate("[Pleat height (mm)] + 1", vals)[0], 31.5)
        self.assertEqual(formula.evaluate("[pleat height (MM)]+1", vals)[0], 31.5)      # case and spacing don't matter
        self.assertEqual(formula.evaluate("round([Length (in)] * 25.4, 1)", vals)[0], 241.3)
        self.assertEqual(formula.evaluate("max([Pleats], 10) / 2", vals)[0], 20)
        self.assertEqual(formula.evaluate("2^3", vals)[0], 8)
        self.assertEqual(formula.evaluate("12.5", {})[0], 12.5)
        v, missing, err = formula.evaluate("[Side band height (in)] + 1", vals)          # not on file: no value, never a guess
        self.assertEqual((v, missing, err), (None, ["Side band height (in)"], ""))
        self.assertIn("divides by zero", formula.evaluate("1 / ([Pleats] - 40)", vals)[2])
        self.assertEqual(formula.values_for("NOPE1"), {})

    def test_only_arithmetic_is_allowed(self):
        for bad in ("__import__('os')", "[Nope] + 1", "open('x')", "[Pleats].real", "lambda: 1", "1 if 1 else 2", "[Pleats", "'a' * 3"):
            with self.assertRaises(formula.FormulaError, msg=bad):
                formula.check(bad)
        self.assertIn("needs brackets", formula.evaluate("round + 1", {})[2])

    def test_resolved_for_a_part(self):
        rows = formula.resolve_params([{"name": "Screw height", "desc": "1mm above pleat height", "target": "[Pleat height (mm)] + 1",
                                        "min": "[Pleat height (mm)] + 0.5", "max": "", "unit": "mm"}], "MG9001")
        self.assertEqual((rows[0]["target"], rows[0]["min"], rows[0]["max"], rows[0]["f_target"]), ("31.5", "31", "", "[Pleat height (mm)] + 1"))


class ChangeoverFromInstructions(TestCase):
    def setUp(self):
        no_planned_time(self)
        cache.clear()
        seed_demo(reset=True)
        self.admin = Person.objects.get(pin="9999")
        self.lead = Person.objects.filter(role="lead").first()
        self.bar = Machine.objects.get(name="Barbara")
        self.nmo = MO.objects.get(number="306785")
        self.c = Client()
        self.c.post("/api/hmi/login", json.dumps({"pin": "1111"}), content_type="application/json")
        self.cmd = lambda n, p: self.c.post("/api/hmi/cmd", json.dumps({"name": n, "p": p, "line": "Barbara"}), content_type="application/json").json()   # noqa: E731
        assert self.cmd("coOpen", {})["ok"]
        assert self.cmd("coEnd", {"checks": [True] * 10, "qty": 100, "scrap": 0})["ok"]

    def ex(self, name, user, **p):
        return engine.execute(name, p, engine.Ctx(user=user))

    def wi(self, kind="changeover", parts="", machines=(), steps=("Lock out", "Change the knife"), params=()):
        r = self.ex("wiCreate", self.lead, title=f"{kind} {parts}", kind=kind, parts=parts, machines=[str(m.pk) for m in machines])
        x = Instruction.objects.get(pk=r["data"]["id"])
        assert self.ex("wiSave", self.lead, id=x.pk, change_note="First", steps=[{"text": t} for t in steps], params=list(params))["ok"]
        assert self.ex("wiApprove", self.admin, id=x.pk)["ok"]
        return x

    def sh(self):
        return ShiftRun.objects.get(machine=self.bar, active=True)

    def test_standard_checklist_without_an_instruction(self):
        self.assertTrue(self.cmd("coPick", {"mo": "306785"})["ok"])
        co = self.sh().co
        self.assertEqual(co["src"], "admin")
        self.assertEqual([s["text"] for s in co["steps"]], engine.machine_list("coSteps", "Barbara"))

    def test_the_closest_instruction_gives_steps_and_worked_out_settings(self):
        from plant.models import part_key
        key = part_key(self.nmo.part)
        PartSpec.objects.update_or_create(key=key, defaults={"ph": 1.2, "sb": None})
        FilterModel3D.objects.update_or_create(pid=key, defaults={"part": key, "flat": {"pleat_height_mm": 30.5}})
        self.wi(parts="", steps=("Generic step",))                                       # every machine, every part
        x = self.wi(parts=self.nmo.part[:3] + "*", machines=[self.bar], steps=("Lock out", "Set the screws"),
                    params=[{"name": "Screw height", "desc": "1mm above pleat height", "target": "[Pleat height (mm)] + 1", "unit": "mm"},
                            {"name": "Glue temp", "target": "180", "min": "175", "max": "185", "unit": "°C"},
                            {"name": "Side band", "target": "[Side band height (in)]", "unit": "in"}])
        self.wi(kind="inkjet", machines=[self.bar], steps=("Inkjet only",))           # only for inkjet-only changes
        self.assertTrue(self.cmd("coPick", {"mo": "306785"})["ok"])
        co = self.sh().co
        self.assertEqual((co["src"], co["wi"]["id"]), ("wi", x.pk))
        self.assertEqual([s["text"] for s in co["steps"]], ["Lock out", "Set the screws"])
        sets = {s["name"]: s for s in co["settings"]}
        self.assertEqual((sets["Screw height"]["value"], sets["Screw height"]["desc"]), ("31.5", "1mm above pleat height"))
        self.assertEqual((sets["Glue temp"]["value"], sets["Glue temp"]["range"]), ("180", "175–185"))
        self.assertEqual(sets["Side band"]["value"], "")
        self.assertIn("Not on file for this part", sets["Side band"]["desc"])
        r = self.c.get("/kiosk/m/changeover")
        self.assertContains(r, x.doc_no)
        self.assertContains(r, "1mm above pleat height")
        self.assertContains(r, "31.5")
        # finishing it counts as reading that revision
        for i in range(2):
            self.cmd("coStep", {"i": i, "checked": True})
        self.assertTrue(self.cmd("coFinish", {})["ok"])
        self.assertTrue(Ack.objects.filter(revision=x.current()).exists())

    def test_import_saved_asset_database_steps_as_drafts(self):
        ChangeoverCache.objects.create(part="MG4062", machine="Barbara", t=engine.now(),
                                       steps=[{"text": "Set pleat height", "setting": "PH", "value": "25", "unit": "mm"}],
                                       settings=[{"name": "Blade", "value": "B-12"}, {"name": "Glue", "value": "180", "unit": "°C"}])
        r = self.ex("wiImportSaved", self.lead)
        self.assertTrue(r["ok"], r)
        x = Instruction.objects.get(title="Changeover: MG4062 on Barbara")
        d = x.draft()
        self.assertEqual((x.kind, x.parts, list(x.machines.all()), x.current()), ("changeover", "MG4062", [self.bar], None))
        self.assertEqual(d.steps[0]["text"], "Set pleat height (PH 25mm)")
        self.assertEqual([(p["name"], p["target"], p["desc"]) for p in d.params], [("Blade", "", "B-12"), ("Glue", "180", "")])
        self.assertIn("0 draft", self.ex("wiImportSaved", self.lead)["msg"])                # not twice

    def test_editor_saves_formulas_and_rejects_typos(self):
        x = self.wi(parts="", steps=("a",))
        self.assertTrue(self.ex("wiDraft", self.lead, id=x.pk)["ok"])
        r = self.ex("wiSave", self.lead, id=x.pk, steps=[{"text": "a"}], params=[{"name": "Screw", "target": "[Pleat hieght (mm)] + 1"}])
        self.assertIn("no variable [Pleat hieght (mm)]", r["error"])
        self.assertTrue(self.ex("wiSave", self.lead, id=x.pk, steps=[{"text": "a"}],
                                params=[{"name": "Screw", "desc": "1mm above", "target": "[Pleat height (mm)] + 1", "min": "5"}])["ok"])
        self.assertEqual(x.draft().params[0]["desc"], "1mm above")
        cl = Client()
        cl.post("/api/hmi/login", json.dumps({"pin": "9999"}), content_type="application/json")
        r = cl.get(f"/instructions/{x.pk}/")
        self.assertContains(r, "data-wi-add=\"step\"")
        self.assertContains(r, "data-wi-var")
        self.assertContains(r, "Pleat height (mm)")
        self.assertContains(r, "instructions/wi.js")
        r = cl.get(f"/instructions/{x.pk}/?part=NOPE1")
        self.assertContains(r, "Not on file for NOPE1")


class SetupFoldedIntoChangeover(TransactionTestCase):
    def test_migration_turns_setup_into_changeover(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor
        ex = MigrationExecutor(connection)
        ex.migrate([("instructions", "0001_work_instructions")])
        old = ex.loader.project_state([("instructions", "0001_work_instructions")]).apps
        old.get_model("instructions", "Instruction").objects.create(doc_no="WI-900", title="Old setup", kind="setup", created=engine.now(), created_by="x")
        ex = MigrationExecutor(connection)
        ex.loader.build_graph()
        ex.migrate(ex.loader.graph.leaf_nodes())
        self.assertEqual(Instruction.objects.get(doc_no="WI-900").kind, "changeover")
        self.assertNotIn("setup", dict(Instruction.KINDS))
