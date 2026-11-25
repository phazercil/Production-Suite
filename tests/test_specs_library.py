"""Filter dimensions in Parts & specs come from the 3D filter library (in the suite), worked out as the viewer does."""
import json

from django.test import Client, TestCase

from hmi import engine
from instructions import formula
from plant.demo import seed_demo
from plant.models import FilterModel3D, PartSpec, Person
from quality import viewer


def filt(part, needs=(), side=True, end=False, foams=(), **P):
    P = {"pc": 40, "pp": 6.0, "mw": 200.0, "ph": 30.0, **P}
    band = {"sb": 25.4, "bt": 1.5, "ob": 1.0, "ot": 2.0}
    return FilterModel3D.objects.create(pid=part, part=part, data={
        "info": {"mtype": "Synthetic"}, "needs": list(needs),
        "config": {"P": P, "ui": {"side_c_on": side, "end_c_on": end}, "foams": list(foams),
                   "groups": {"side": {"bands": [band, band]}, "end": {"bands": [band, band]}}}})


class Dims(TestCase):
    def test_as_the_viewer_works_it_out(self):
        d = viewer.dims(filt("MG1"))
        self.assertEqual((d["pleats"], d["len"], d["olen"], d["wid"], d["ph"], d["sb"]), (40, 240, 240, 203, 30, 25.4))
        self.assertEqual(d["hgt"], 33)                                   # 30 + 2 above, 1 below
        d = viewer.dims(filt("MG2", end=True, foams=[{"where": "end0", "t": 5}]))
        self.assertEqual(d["olen"], 240 + 3 + 5)
        d = viewer.dims(filt("MG3", side=False))
        self.assertEqual((d["wid"], d["sb"]), (200, None))

    def test_values_still_to_review_are_not_used(self):
        d = viewer.dims(filt("MG4", needs=["P.pp", "band.side.0.sb"]))
        self.assertEqual((d["len"], d["olen"], d["sb"], d["pleats"], d["wid"]), (None, None, None, 40, 203))


class PartsFromLibrary(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.admin = Person.objects.get(pin="9999")

    def test_pull_fills_parts_and_specs_in_inches(self):
        filt("MG9100", needs=["P.mw"])
        PartSpec.objects.create(key="MG9100", wid=8.5, tol=0.06, hand=["wid"], rev="C")
        msg = engine.pull_drawing("MG9100")
        self.assertIn("loaded from the 3D filter library", msg)
        ps = PartSpec.objects.get(key="MG9100")
        self.assertEqual((ps.pleats, round(ps.len, 4), ps.ph, ps.sb), (40, round(240 / 25.4, 4), round(30 / 25.4, 4), 1.0))
        self.assertEqual((ps.wid, ps.tol, ps.rev, ps.media), (8.5, 0.06, "C", "Synthetic"))   # typed / not in the library: kept
        self.assertIn("isn't in the 3D filter library", engine.pull_drawing("NOPE77"))
        self.assertFalse(PartSpec.objects.filter(key="NOPE77").exists())

    def test_import_and_the_switch(self):
        filt("MG9200")
        r = engine.execute("partsImport", {"parts": "MG9200 NOPE88"}, engine.Ctx(user=self.admin))
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["data"]["done"], r["data"]["missing"]), (["MG9200"], ["NOPE88"]))
        self.assertTrue(engine.execute("settingSet", {"k": "specsFrom", "v": "db"}, engine.Ctx(user=self.admin))["ok"])
        self.assertFalse(engine.specs_from_library())
        self.assertFalse(engine.execute("settingSet", {"k": "specsFrom", "v": "x"}, engine.Ctx(user=self.admin))["ok"])
        cl = Client()
        cl.post("/api/hmi/login", json.dumps({"pin": "9999"}), content_type="application/json")
        r = cl.get("/kiosk/v/admin?tab=codata")
        self.assertContains(r, "3D filter library (in the suite)")

    def test_size_is_a_formula_variable(self):
        filt("MG9300")
        vals = formula.values_for("MG9300")
        self.assertEqual(formula.evaluate("[Overall length (mm)] / 2", vals)[0], 120)
        self.assertEqual(formula.evaluate("[Filter width (mm)]", vals)[0], 203)


class SheetTolerances(TestCase):
    def setUp(self):
        seed_demo(reset=True)

    def test_parse(self):
        self.assertEqual(viewer.parse_tol("+1 / -2 mm"), (2, 1))
        self.assertEqual(viewer.parse_tol("+1 mm / -2 mm"), (2, 1))
        self.assertEqual(viewer.parse_tol("±0.5 mm"), (0.5, 0.5))
        self.assertEqual(viewer.parse_tol("+/-0.5"), (0.5, 0.5))
        self.assertEqual(viewer.parse_tol("0"), (0, 0))
        self.assertEqual(viewer.parse_tol('±0.04"'), (0.04 * 25.4, 0.04 * 25.4))
        for bad in ("", "abc", "1 2", "+1 mm / -0.04 in"):
            self.assertIsNone(viewer.parse_tol(bad), bad)

    def test_sheet_first_then_every_filter_then_tol(self):
        from hmi import qc
        x = filt("MG9400")
        x.data["info"].update({"tol_len": "+1 / -2 mm", "tol_wid": "", "tol_ph": "nonsense"})
        x.save()
        engine.pull_drawing("MG9400")
        ps = PartSpec.objects.get(key="MG9400")
        self.assertEqual(ps.sheet_tol, {"len": {"minus": round(2 / 25.4, 5), "plus": round(1 / 25.4, 5)}})
        PartSpec.objects.filter(pk=ps.pk).update(tol=0.03)
        part = PartSpec.objects.get(pk=ps.pk).as_dict()
        every = {"len": {"minus": 0.5, "plus": 0.5}, "wid": {"minus": 0.25, "plus": 0.25}}
        self.assertEqual(qc.tol_for("len", part, every), (round(2 / 25.4, 5), round(1 / 25.4, 5)))   # its sheet
        self.assertEqual(qc.tol_for("wid", part, every), (0.25, 0.25))                              # Tolerances tab
        self.assertEqual(qc.tol_for("ph", part, every), (0.03, 0.03))                               # its own Tol ±
        self.assertEqual(qc.fmt_tol("len", part, every), "+0.039 / −0.079")
