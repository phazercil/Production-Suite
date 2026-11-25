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
        self.assertEqual(formula.evaluate("round([Pleat height (mm)] / 25.4, 2)", vals)[0], 1.2)
        self.assertEqual(formula.evaluate("max([Pleat count], 10) / 2", vals)[0], 20)
        self.assertEqual(formula.evaluate("2^3", vals)[0], 8)
        self.assertEqual(formula.evaluate("12.5", {})[0], 12.5)
        v, missing, err = formula.evaluate("[Side A height (mm)] + 1", vals)            # not on file: no value, never a guess
        self.assertEqual((v, missing, err), (None, ["Side A height (mm)"], ""))
        self.assertIn("divides by zero", formula.evaluate("1 / ([Pleat count] - 40)", vals)[2])
        self.assertNotIn("Length (in)", [v["name"] for v in formula.variables()])   # nothing from the drawing
        self.assertEqual(formula.values_for("NOPE1"), {})

    def test_only_arithmetic_is_allowed(self):
        for bad in ("__import__('os')", "[Nope] + 1", "open('x')", "[Pleat count].real", "lambda: 1", "1 if 1 else 2", "[Pleat count"):
            with self.assertRaises(formula.FormulaError, msg=bad):
                formula.check(bad)
        self.assertIn("needs brackets", formula.evaluate("round + 1", {})[2])
        self.assertIn("is text", formula.evaluate("'a' * 3", {})[2])

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
                            {"name": "Side band", "target": "[Side A height (mm)]", "unit": "mm"}])
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


class NoteCardsAndAlsoRead(TestCase):
    def setUp(self):
        no_planned_time(self)
        cache.clear()
        seed_demo(reset=True)
        from plant.models import part_key
        self.admin = Person.objects.get(pin="9999")
        self.lead = Person.objects.filter(role="lead").first()
        self.bar = Machine.objects.get(name="Barbara")
        self.nmo = MO.objects.get(number="306785")
        self.key = part_key(self.nmo.part)
        self.c = Client()
        self.c.post("/api/hmi/login", json.dumps({"pin": "1111"}), content_type="application/json")
        self.cmd = lambda n, p: self.c.post("/api/hmi/cmd", json.dumps({"name": n, "p": p, "line": "Barbara"}), content_type="application/json").json()   # noqa: E731
        assert self.cmd("coOpen", {})["ok"]
        assert self.cmd("coEnd", {"checks": [True] * 10, "qty": 100, "scrap": 0})["ok"]

    def wi(self, kind, title, condition="", parts="", see_also="", steps=("Read me",)):
        r = engine.execute("wiCreate", {"title": title, "kind": kind, "parts": parts, "condition": condition}, engine.Ctx(user=self.lead))
        assert r["ok"], r
        x = Instruction.objects.get(pk=r["data"]["id"])
        if see_also:
            assert engine.execute("wiMeta", {"id": x.pk, "title": title, "kind": kind, "parts": parts, "condition": condition,
                                             "see_also": see_also}, engine.Ctx(user=self.lead))["ok"]
        assert engine.execute("wiSave", {"id": x.pk, "change_note": "x", "steps": [{"text": t} for t in steps],
                                         "params": [{"name": "Band", "target": "[Side A thickness (mm)] * 2", "unit": "mm"}]},
                              engine.Ctx(user=self.lead))["ok"]
        assert engine.execute("wiApprove", {"id": x.pk}, engine.Ctx(user=self.admin))["ok"]
        return x

    def filt(self, thick):
        FilterModel3D.objects.update_or_create(pid=self.key, defaults={"part": self.key, "flat": {"side_a_thickness_mm": thick, "side_bands": True}})

    def sh(self):
        return ShiftRun.objects.get(machine=self.bar, active=True)

    def test_note_card_for_2mm_side_bands_must_be_ticked(self):
        self.filt(2.0)
        note = self.wi("note", "2 mm side bands", condition="[Side A thickness (mm)] = 2 and [Side bands] = 1",
                       steps=("Use the wide band guide: {Band}",))
        self.wi("note", "Thin bands", condition="[Side A thickness (mm)] < 1")
        co_wi = self.wi("changeover", "P7 changeover", steps=("Lock out",))
        self.assertTrue(self.cmd("coPick", {"mo": "306785"})["ok"])
        steps = self.sh().co["steps"]
        self.assertEqual([s.get("note", {}).get("title") for s in steps], ["2 mm side bands", None])
        self.assertEqual(steps[0]["note"]["lines"][0]["text"], "Use the wide band guide: 4 mm")
        r = self.c.get("/kiosk/m/changeover")
        self.assertContains(r, "Special note · 2 mm side bands")
        self.assertContains(r, "I've read this note")
        self.cmd("coStep", {"i": 1, "checked": True})
        self.assertFalse(self.cmd("coFinish", {})["ok"])                    # the note isn't ticked yet
        self.cmd("coStep", {"i": 0, "checked": True})
        self.assertTrue(self.cmd("coFinish", {})["ok"])
        self.assertTrue(Ack.objects.filter(revision=note.current()).exists())
        self.assertTrue(Ack.objects.filter(revision=co_wi.current()).exists())

    def test_not_for_other_filters_and_shown_when_it_cant_be_checked(self):
        self.wi("note", "2 mm side bands", condition="[Side A thickness (mm)] = 2")
        self.filt(1.5)
        self.assertTrue(self.cmd("coPick", {"mo": "306785"})["ok"])
        self.assertFalse(any(s.get("note") for s in self.sh().co["steps"]))
        self.cmd("coBack", {})
        FilterModel3D.objects.filter(pid=self.key).delete()                 # no data for the filter: shown, marked
        self.assertTrue(self.cmd("coPick", {"mo": "306785"})["ok"])
        n = [s["note"] for s in self.sh().co["steps"] if s.get("note")]
        self.assertEqual(n[0]["unchecked"], ["Side A thickness (mm)"])

    def test_a_conditional_changeover_only_when_its_condition_holds(self):
        self.filt(1.5)
        general = self.wi("changeover", "General", steps=("General",))
        self.wi("changeover", "Wide bands", condition="[Side A thickness (mm)] = 2", parts=self.nmo.part, steps=("Wide",))
        self.assertTrue(self.cmd("coPick", {"mo": "306785"})["ok"])
        self.assertEqual(self.sh().co["wi"]["id"], general.pk)

    def test_also_read_and_opening_one_over_the_changeover(self):
        self.filt(2.0)
        safety = self.wi("safety", "Blade guard")
        ref = self.wi("operation", "Glue pot", parts="ZZZ*")                # not for this part, but named
        self.wi("changeover", "P7 changeover", see_also=ref.doc_no, steps=("Lock out",))
        self.assertTrue(self.cmd("coPick", {"mo": "306785"})["ok"])
        self.assertEqual([a["doc"] for a in self.sh().co["alsoRead"]], [ref.doc_no, safety.doc_no])
        r = self.c.get("/kiosk/m/changeover")
        self.assertContains(r, "Also read")
        self.assertContains(r, f"/kiosk/m/wi?id={safety.pk}")
        r = self.c.get(f"/kiosk/m/wi?id={safety.pk}")
        self.assertContains(r, "Blade guard")
        self.assertContains(r, "Back to the changeover")
        r = self.c.post("/kiosk/do/wiReadCo", {"rev": safety.current().pk, "id": safety.pk})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(Ack.objects.filter(revision=safety.current()).exists())

    def test_details_are_checked(self):
        ctx = engine.Ctx(user=self.lead)
        r = engine.execute("wiCreate", {"title": "Everywhere note", "kind": "note"}, ctx)
        self.assertIn("needs a Show when condition", r["error"])
        r = engine.execute("wiCreate", {"title": "Bad", "kind": "note", "condition": "[Nope] = 2"}, ctx)
        self.assertIn("Show when: There is no variable [Nope]", r["error"])
        x = self.wi("changeover", "A")
        r = engine.execute("wiMeta", {"id": x.pk, "title": "A", "kind": "changeover", "see_also": "WI-999"}, ctx)
        self.assertIn("there's no WI-999", r["error"])

    def test_preview_says_whether_it_shows(self):
        self.filt(2.0)
        note = self.wi("note", "2 mm side bands", condition="[Side A thickness (mm)] = 2")
        cl = Client()
        cl.post("/api/hmi/login", json.dumps({"pin": "9999"}), content_type="application/json")
        r = cl.post(f"/instructions/{note.pk}/preview", {"rev": note.current().pk, "part": self.key})
        self.assertContains(r, "Shows")
        self.filt(1.0)
        r = cl.post(f"/instructions/{note.pk}/preview", {"rev": note.current().pk, "part": self.key})
        self.assertContains(r, "Doesn't show")


class Exclusions(NoteCardsAndAlsoRead):
    def test_not_for_filters_where_and_except_parts(self):
        from instructions.cmds import for_station, notes_for
        self.filt(2.0)
        general = self.wi("changeover", "General", steps=("General",))
        narrow = self.wi("changeover", "No bands version", parts=self.nmo.part[:2] + "*", steps=("Narrow",))
        engine.execute("wiMeta", {"id": narrow.pk, "title": narrow.title, "kind": "changeover", "parts": narrow.parts,
                                  "exclude": "[Side bands] = 1"}, engine.Ctx(user=self.lead))
        self.assertTrue(self.cmd("coPick", {"mo": "306785"})["ok"])
        self.assertEqual(self.sh().co["wi"]["id"], general.pk)              # excluded: this filter has side bands
        self.cmd("coBack", {})
        FilterModel3D.objects.update_or_create(pid=self.key, defaults={"flat": {"side_a_thickness_mm": 2.0, "side_bands": False}})
        self.assertTrue(self.cmd("coPick", {"mo": "306785"})["ok"])
        self.assertEqual(self.sh().co["wi"]["id"], narrow.pk)
        note = self.wi("note", "Bands", condition="[Side A thickness (mm)] = 2")
        engine.execute("wiMeta", {"id": note.pk, "title": note.title, "kind": "note", "condition": note.condition,
                                  "except_parts": self.key}, engine.Ctx(user=self.lead))
        self.assertEqual(notes_for(self.bar, self.key), [])
        self.assertNotIn(note.pk, [d["x"].pk for d in for_station(self.bar, self.key)])
        self.assertIn(note.pk, [d["x"].pk for d in for_station(self.bar, "OTHER1")])   # unchecked there, still shown

    def test_an_exclusion_that_cant_be_checked_doesnt_hide_it(self):
        from instructions.cmds import for_station
        x = self.wi("safety", "Blade guard")
        engine.execute("wiMeta", {"id": x.pk, "title": x.title, "kind": "safety", "exclude": "[Side bands] = 0"}, engine.Ctx(user=self.lead))
        d = next(d for d in for_station(self.bar, "NODATA1") if d["x"].pk == x.pk)
        self.assertEqual(d["unchecked"], ["Side bands"])
        r = engine.execute("wiMeta", {"id": x.pk, "title": x.title, "kind": "safety", "exclude": "[Nope] = 1"}, engine.Ctx(user=self.lead))
        self.assertIn("Not for filters where: There is no variable [Nope]", r["error"])


class ListPage(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.lead = Person.objects.filter(role="lead").first()
        self.admin = Person.objects.get(pin="9999")
        self.cl = Client()
        self.cl.post("/api/hmi/login", json.dumps({"pin": "9999"}), content_type="application/json")

    def test_in_effect_drafts_and_new_button(self):
        ctx = engine.Ctx(user=self.lead)
        a = Instruction.objects.get(pk=engine.execute("wiCreate", {"title": "Approved one"}, ctx)["data"]["id"])
        engine.execute("wiSave", {"id": a.pk, "steps": [{"text": "x"}], "change_note": "x"}, ctx)
        engine.execute("wiApprove", {"id": a.pk}, engine.Ctx(user=self.admin))
        engine.execute("wiCreate", {"title": "Still a draft"}, ctx)
        r = self.cl.get("/instructions/")
        self.assertContains(r, "Approved one")
        self.assertNotContains(r, "Still a draft")
        self.assertContains(r, "data-wi-new")
        self.assertContains(r, 'Drafts not approved yet <span class="num">(1)</span>')
        r = self.cl.get("/instructions/?show=drafts")
        self.assertContains(r, "Still a draft")
        self.assertNotContains(r, "Approved one</a>")

    def test_a_failed_create_comes_back_open_with_what_was_typed(self):
        r = self.cl.post("/instructions/do/wiCreate", {"title": "Bands note", "kind": "note", "parts": "", "condition": "",
                                                       "machines": [str(Machine.objects.get(name="Barbara").pk)]})
        self.assertIn("new=1", r["Location"])
        r = self.cl.get(r["Location"])
        self.assertContains(r, "data-wi-newdlg data-open")
        self.assertContains(r, 'value="Bands note"')
        self.assertContains(r, '<option value="note" selected>')
        self.assertContains(r, "needs a Show when condition")
        r = self.cl.post("/instructions/do/wiCreate", {"title": "Good one", "kind": "changeover"})
        x = Instruction.objects.get(title="Good one")
        self.assertTrue(r["Location"].startswith(f"/instructions/{x.pk}/"))   # straight into the editor


class InfoSheetVariables(TestCase):
    def setUp(self):
        from django.core.cache import cache as c
        c.clear()
        FilterModel3D.objects.create(pid="AQ1042", part="AQ1042", flat={}, data={
            "info": {"glue": "Hot melt", "screw": "8.1", "helix": "18/24", "tol_len": "+1 / -2 mm", "notes": "Fragile"},
            "extra": [{"label": "Box size", "value": "B12"}],
            "needs": ["P.mw"],
            "config": {"P": {"ph": 28, "pc": 30, "pp": 8.1, "mw": 215}, "ui": {"side_c_on": True}}})

    def test_info_sheet_fields_tolerances_and_added_rows(self):
        names = [v["name"] for v in formula.variables()]
        for n in ("Glue", "Screw #", "Helix #", "Tolerance – length plus (mm)", "Info sheet · Box size", "Notes"):
            self.assertIn(n, names)
        vals = formula.values_for("AQ1042")
        self.assertEqual(formula.condition('[Glue] = "hot melt"', vals)[0], True)
        self.assertEqual(formula.condition('[Screw #] = "8.1"', vals)[0], True)        # a number-looking field matches either way
        self.assertEqual(formula.condition("[Screw #] = 8.1", vals)[0], True)
        self.assertEqual(formula.condition('[Helix #] = "18/24"', vals)[0], True)
        self.assertEqual(formula.evaluate("[Tolerance – length minus (mm)]", vals)[0], 2)
        self.assertEqual(formula.condition('[Info sheet · Box size] = "b12"', vals)[0], True)
        self.assertEqual(formula.evaluate("[Pleat height (mm)] + 1", vals)[0], 29)    # read from the filter itself (no flat row)
        self.assertNotIn("media width (mm)", vals)                                    # still to review: not used


class OneSaveAndCatalog(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.lead = Person.objects.filter(role="lead").first()
        self.cl = Client()
        self.cl.post("/api/hmi/login", json.dumps({"pin": "9999"}), content_type="application/json")

    def test_details_and_draft_saved_together_or_not_at_all(self):
        x = Instruction.objects.get(pk=engine.execute("wiCreate", {"title": "Old title"}, engine.Ctx(user=self.lead))["data"]["id"])
        r = self.cl.get(f"/instructions/{x.pk}/")
        html = r.content.decode()
        self.assertIn('action="/instructions/do/wiSaveAll"', html)
        self.assertLess(html.index('name="title"'), html.index('name="s_text"'))     # details at the top
        self.assertNotIn("Save details", html)
        r = self.cl.post("/instructions/do/wiSaveAll", {"id": x.pk, "title": "New title", "kind": "changeover", "parts": "MG*",
                                                         "condition": "", "exclude": "", "except_parts": "", "see_also": "", "active": "1",
                                                         "s_text": ["Lock out"], "s_caution": [""], "p_name": [""], "change_note": "x"})
        self.assertIn("msg=", r["Location"])
        x.refresh_from_db()
        self.assertEqual((x.title, x.parts, x.draft().steps[0]["text"]), ("New title", "MG*", "Lock out"))
        r = self.cl.post("/instructions/do/wiSaveAll", {"id": x.pk, "title": "Changed again", "kind": "changeover", "parts": "MG*",
                                                         "s_text": ["Set {Nope}"], "s_caution": [""], "p_name": [""]})
        self.assertIn("err=", r["Location"])
        x.refresh_from_db()
        self.assertEqual((x.title, x.draft().steps[0]["text"]), ("New title", "Lock out"))   # neither saved

    def test_filter_catalog_and_add_new_model(self):
        FilterModel3D.objects.create(pid="AQ1042", part="AQ1042", data={"info": {"glue": "Hot melt", "tol_len": "+1 / -2 mm"},
                                                                        "config": {"P": {"ph": 28, "pc": 30, "pp": 8.1, "mw": 215}}})
        r = self.cl.get("/quality/?tab=parts")
        self.assertContains(r, "Filter catalog")
        self.assertContains(r, "AQ1042")
        self.assertContains(r, "Hot melt")
        self.assertContains(r, 'href="/quality/viewer/?new=1"')
        self.assertContains(r, 'href="/quality/viewer/?part=AQ1042"')
        self.assertContains(r, "What QC checks use")
        r = self.cl.get("/quality/?tab=viewer&new=1")
        self.assertContains(r, "/quality/viewer/app/?&amp;new=1")
        self.assertContains(self.cl.get("/quality/viewer/app/?new=1"), '"newModel": true')
