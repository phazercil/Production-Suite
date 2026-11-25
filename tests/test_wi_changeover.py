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
        r = cl.post(f"/instructions/{x.pk}/preview", {"rev": x.draft().pk, "part": "NOPE1"})
        self.assertContains(r, "Not on file")
        self.assertContains(r, "NOPE1 isn't in the 3D filter library")


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


class PhotosOnSteps(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.lead = Person.objects.filter(role="lead").first()
        r = engine.execute("wiCreate", {"title": "P7 changeover", "kind": "changeover"}, engine.Ctx(user=self.lead))
        self.x = Instruction.objects.get(pk=r["data"]["id"])
        self.cl = Client()
        self.cl.post("/api/hmi/login", json.dumps({"pin": "9999"}), content_type="application/json")

    def test_add_photo_on_a_step_when_saving(self):
        import io

        from django.core.files.uploadedfile import SimpleUploadedFile
        from PIL import Image
        b = io.BytesIO()
        Image.new("RGB", (40, 30), "blue").save(b, "PNG")
        pic = SimpleUploadedFile("screws.png", b.getvalue(), content_type="image/png")
        junk = SimpleUploadedFile("notes.txt", b"hello", content_type="text/plain")
        r = self.cl.post("/instructions/do/wiSave", {"id": self.x.pk, "s_text": ["Lock out", "Set the screws"], "s_caution": ["", ""],
                                                     "s_files_1": [pic, junk], "p_name": [""], "change_note": "First"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("1+file+not+added", r["Location"])
        steps = self.x.draft().steps
        self.assertEqual(steps[0]["media"], [])
        self.assertEqual(len(steps[1]["media"]), 1)
        m = self.x.media.get()
        self.assertEqual((m.kind, m.name, steps[1]["media"][0]), ("image", "screws.png", m.pk))
        r = self.cl.get(f"/instructions/{self.x.pk}/")
        self.assertContains(r, 'name="s_files_0"')
        self.assertContains(r, 'enctype="multipart/form-data" data-wi-form')
        self.assertContains(r, f'name="s_media_1" data-wi-media value="{m.pk}" checked')


class MostSpecificWins(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.lead = Person.objects.filter(role="lead").first()
        self.admin = Person.objects.get(pin="9999")
        self.bar = Machine.objects.get(name="Barbara")

    def wi(self, parts, machines=()):
        r = engine.execute("wiCreate", {"title": parts or "all", "kind": "changeover", "parts": parts, "machines": [str(m.pk) for m in machines]},
                           engine.Ctx(user=self.lead))
        x = Instruction.objects.get(pk=r["data"]["id"])
        engine.execute("wiSave", {"id": x.pk, "change_note": "x", "steps": [{"text": parts or "all"}]}, engine.Ctx(user=self.lead))
        assert engine.execute("wiApprove", {"id": x.pk}, engine.Ctx(user=self.admin))["ok"]
        return x

    def test_exact_then_longest_pattern_then_machine(self):
        from instructions.cmds import changeover_for
        every = self.wi("")
        mg = self.wi("MG*")                         # made before the narrower ones: lower WI number
        mg_bar = self.wi("MG*", machines=[self.bar])
        mg4062 = self.wi("MG4062*")
        exact = self.wi("MG4062HP")
        pick = lambda part, m=self.bar: changeover_for(m, part)[0]   # noqa: E731
        self.assertEqual(pick("MG4062HP"), exact)
        self.assertEqual(pick("MG4062C"), mg4062)
        self.assertEqual(pick("MG5000"), mg_bar)
        self.assertEqual(pick("MG5000", Machine.objects.get(name="Salvatore")), mg)
        self.assertEqual(pick("AQ1278"), every)


class ParametersInSteps(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.lead = Person.objects.filter(role="lead").first()
        self.admin = Person.objects.get(pin="9999")
        FilterModel3D.objects.create(pid="MG7001", part="MG7001", flat={"pleat_height_mm": 30.5})
        r = engine.execute("wiCreate", {"title": "P7 changeover", "kind": "changeover"}, engine.Ctx(user=self.lead))
        self.x = Instruction.objects.get(pk=r["data"]["id"])
        self.params = [{"name": "Screw height", "desc": "1mm above pleat height", "target": "[Pleat height (mm)] + 1",
                        "min": "[Pleat height (mm)] + 0.5", "max": "[Pleat height (mm)] + 1.5", "unit": "mm"},
                       {"name": "Glue temp", "target": "180", "unit": "°C"}]

    def test_fill_text(self):
        resolved = formula.resolve_params(self.params, "MG7001")
        vals = formula.values_for("MG7001")
        out = formula.fill_text("Set to {Screw height}, between {screw height:range}; glue {Glue temp:max}; "
                                "half pleat {[Pleat height (mm)] / 2}; {Unknown}", resolved, vals)
        self.assertEqual(out, "Set to 31.5 mm, between 31–32 mm; glue (Glue temp: no max set); half pleat 15.25; {Unknown}")
        missing = formula.fill_text("{Screw height}", formula.resolve_params(self.params, "NOPE9"), {})
        self.assertEqual(missing, "(not on file: Pleat height (mm))")

    def test_saving_checks_the_names(self):
        r = engine.execute("wiSave", {"id": self.x.pk, "steps": [{"text": "Set {Screw hieght}"}], "params": self.params},
                           engine.Ctx(user=self.lead))
        self.assertFalse(r["ok"])
        self.assertIn("Step 1: {Screw hieght}: there's no parameter called Screw hieght", r["error"])
        self.assertFalse(engine.execute("wiSave", {"id": self.x.pk, "steps": [{"text": "x {[Nope] + 1}"}], "params": self.params},
                                        engine.Ctx(user=self.lead))["ok"])
        self.assertTrue(engine.execute("wiSave", {"id": self.x.pk, "steps": [{"text": "Set the screws to {Screw height}"}],
                                                  "params": self.params, "change_note": "x"}, engine.Ctx(user=self.lead))["ok"])

    def test_changeover_checklist_and_kiosk_show_the_values(self):
        from instructions.cmds import changeover_checklist
        engine.execute("wiSave", {"id": self.x.pk, "steps": [{"text": "Set the screws to {Screw height}", "caution": "Under {Glue temp}"}],
                                  "params": self.params, "change_note": "x"}, engine.Ctx(user=self.lead))
        engine.execute("wiApprove", {"id": self.x.pk}, engine.Ctx(user=self.admin))
        steps, sets, _ = changeover_checklist(self.x, self.x.current(), "MG7001")
        self.assertEqual((steps[0]["text"], steps[0]["caution"]), ("Set the screws to 31.5 mm", "Under 180 °C"))
        self.assertEqual(self.x.current().steps[0]["text"], "Set the screws to {Screw height}")   # the document keeps the placeholder

    def test_preview_uses_unsaved_editor_content_and_saves_nothing(self):
        cl = Client()
        cl.post("/api/hmi/login", json.dumps({"pin": "9999"}), content_type="application/json")
        before = list(self.x.draft().steps)
        r = cl.post(f"/instructions/{self.x.pk}/preview", {
            "part": "mg7001", "s_text": ["Lock out", "Set the screws to {Screw height}"], "s_caution": ["", ""],
            "p_name": ["Screw height"], "p_desc": ["1mm above"], "p_target": ["[Pleat height (mm)] + 1"], "p_min": [""], "p_max": [""],
            "p_unit": ["mm"], "new_files": "2"})
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("Set the screws to 31.5 mm", html)
        self.assertIn("Step 2 of 2", html)
        self.assertIn("for <b>MG7001</b>", html)
        self.assertIn("2 photos or videos picked but not saved yet", html)
        self.assertEqual(self.x.draft().steps, before)                         # nothing saved
        r = cl.post(f"/instructions/{self.x.pk}/preview", {"part": "MG7001", "s_text": ["Set {Nope}"], "s_caution": [""], "p_name": [""]})
        self.assertContains(r, "This wouldn't save yet")                 # a typo is shown, the rest still previews
        self.assertContains(r, "Set {Nope}")
        self.assertEqual(Client().post(f"/instructions/{self.x.pk}/preview", {"part": "x"}).status_code, 403)

    def test_editor_page_has_preview_and_unsaved_guard(self):
        cl = Client()
        cl.post("/api/hmi/login", json.dumps({"pin": "9999"}), content_type="application/json")
        r = cl.get(f"/instructions/{self.x.pk}/")
        for bit in ("data-wi-preview", "data-wi-savebar", "data-wi-dirty", "data-wi-param", "data-wi-modal"):
            self.assertContains(r, bit)
        self.assertNotContains(r, 'method="get" class="wi-try"')              # the old Work out form (it left the page)
