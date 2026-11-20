"""Quality › 3D viewer: the Pleated Filter 3D Viewer served by the suite (no internet), its part library in the suite's
database (saved only with Quality settings), updates from the asset database's "3D filter data", and the Operator
Kiosk's view-only 3D Model following the filter its line runs."""
import json
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase

from hmi import engine
from hmi.models import SavedQuery, ShiftRun
from plant.demo import seed_demo
from plant.models import FilterModel3D
from quality import viewer

from .test_quality import sup_login


def doc(part, **P):
    return {"part": part, "info": {"cust": "MG4062HP"}, "config": {"P": {"ph": 28, "pc": 28, "pp": 9.8, "fl": 274.4, **P}, "groups": {}, "foams": [], "ui": {}},
            "needs": [], "inkjets": {}, "extra": []}


class FilterViewerTests(TestCase):
    def setUp(self):
        cache.clear()
        seed_demo(reset=True)

    def post(self, c, path, body):
        return c.post(path, json.dumps(body), content_type="application/json", HTTP_X_CSRFTOKEN=c.cookies["csrftoken"].value)

    def test_the_viewer_runs_without_the_internet(self):
        r = Client().get("/quality/viewer/app/?view=1&line=Barbara")
        body = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("cdn.jsdelivr.net", body)
        self.assertNotIn("cdnjs.cloudflare.com", body)
        self.assertIn("/static/vendor/three/three.module.min.js", body)
        self.assertIn("/static/vendor/three/addons/", body)
        self.assertIn("vendor/xlsx/xlsx.full.min.js", body)
        self.assertIn('"view": true', body)
        self.assertIn('"canEdit": false', body)
        self.assertIn('"line": "Barbara"', body)
        from django.contrib.staticfiles import finders
        for f in ("vendor/three/three.module.min.js", "vendor/three/addons/controls/OrbitControls.js", "vendor/three/addons/exporters/STLExporter.js",
                  "vendor/three/addons/renderers/CSS2DRenderer.js", "vendor/xlsx/xlsx.full.min.js", "quality/fv-host.js"):
            self.assertTrue(finders.find(f), f)

    def test_library_reads_for_anyone_and_saves_with_quality_settings(self):
        anon = Client(enforce_csrf_checks=True)
        anon.get("/quality/viewer/app/")
        r = self.post(anon, "/api/quality/fv/parts/set", {"id": "AQ1136C", "data": doc("AQ1136C")})
        self.assertEqual(r.status_code, 403)
        c = Client(enforce_csrf_checks=True)
        self.assertTrue(sup_login(c, "4444", "section.quality", "/quality/")["ok"])
        c.get("/quality/viewer/app/")
        r = self.post(c, "/api/quality/fv/parts/set", {"id": "AQ1136C", "data": doc("AQ1136C")})
        self.assertTrue(r.json()["ok"], r.content)
        j = anon.get("/api/quality/fv/parts").json()
        self.assertEqual([d["id"] for d in j["docs"]], ["AQ1136C"])
        self.assertTrue(anon.get(f"/api/quality/fv/parts?v={j['v']}").json()["same"])
        self.post(c, "/api/quality/fv/parts/delete", {"id": "AQ1136C"})
        self.assertFalse(FilterModel3D.objects.exists())
        page = c.get("/quality/?tab=viewer").content.decode()
        self.assertIn('src="/quality/viewer/app/"', page)
        self.assertIn("3D filter data", page)                                   # how to connect a database later

    def test_kiosk_3d_model_follows_the_line(self):
        k = Client(HTTP_HX_REQUEST="true")
        k.post("/kiosk/do/station", {"line": "Barbara"})
        r = k.get("/kiosk/v/model")
        self.assertContains(r, "/quality/viewer/app/?view=1&amp;line=Barbara")
        self.assertContains(r, 'hx-preserve="true"')
        sh = ShiftRun.objects.get(line="Barbara", active=True)
        j = Client().get("/api/quality/fv/running?line=Barbara").json()
        self.assertEqual(j["part"], sh.mo.part)
        self.assertFalse(j["settingUp"])

    def test_found_by_customer_number(self):
        FilterModel3D.objects.create(pid="AQ1136C", part="AQ1136C", data=doc("AQ1136C"))
        self.assertEqual(viewer.find("MG4062HP").pid, "AQ1136C")
        self.assertEqual(viewer.find("aq1136c").pid, "AQ1136C")
        self.assertIsNone(viewer.find("ZZ1"))

    def test_updates_from_the_asset_database_keep_what_it_does_not_have(self):
        x = FilterModel3D.objects.create(pid="AQ1136C", part="AQ1136C", data=doc("AQ1136C", mw=215, mt=0.8))
        self.assertIsNone(viewer.refresh_from_db(x))                             # not set up: kept as stored
        SavedQuery.objects.create(name="filter_3d", sql="SELECT 1", saved_by="x")
        row = {"columns": [], "rows": [{"PLEAT_HEIGHT_MM": "1.25", "side_a_offset_below_pleat_mm": 2, "inkjet_extra_word": "HEPA",
                                        "uom": "in", "not_a_setting": "x", "pleat_count": None}], "ms": 1, "truncated": False}
        with mock.patch.object(engine.integrations, "assets_ready", return_value=True), \
                mock.patch.object(engine.integrations, "run_saved", return_value=row):
            msg = viewer.refresh_from_db(x, force=True)
        self.assertIn("3 settings from the asset database", msg)
        d = FilterModel3D.objects.get(pid="AQ1136C").data
        self.assertEqual(d["db"]["row"], {"pleat_height_mm": "1.25", "side_a_offset_below_pleat_mm": 2, "inkjet_extra_word": "HEPA", "uom": "in"})
        self.assertEqual(d["config"]["P"]["mw"], 215)                            # the saved settings stay; the viewer lays the row over them
        c = Client(enforce_csrf_checks=True)
        self.assertTrue(sup_login(c, "4444", "section.quality", "/quality/")["ok"])
        with mock.patch.object(engine.integrations, "assets_ready", return_value=True), \
                mock.patch.object(engine.integrations, "run_saved", return_value=row):
            r = c.post("/quality/viewer/refresh-all/", {"csrfmiddlewaretoken": c.cookies["csrftoken"].value})
        self.assertIn("Read+1+filter", r["Location"])

    def test_every_viewer_setting_is_a_connector_role(self):
        from hmi.snapshot import qdef
        roles = [r[0] for r in qdef()["filter_3d"]["roles"]]
        for k in ("pleat_height_mm", "pleat_count", "side_a_offset_below_pleat_mm", "side_a_offset_above_pleat_mm", "end_a_fold_over",
                  "pull_tab_position_from_band_end_mm", "glue_bead_count", "inkjet_epa_registration", "foam_1_placed_on", "customer_oe_no", "uom"):
            self.assertIn(k, roles)
        self.assertGreater(len(roles), 120)
        self.assertNotIn("needs_review", roles)


class ViewerLibrarySourceTests(TestCase):
    """The 3D viewer's part library as a Connector builder source (no outside database needed)."""
    def setUp(self):
        cache.clear()
        seed_demo(reset=True)
        from hmi.models import Person
        self.ctx = engine.Ctx(user=Person.objects.get(role="admin"))
        FilterModel3D.objects.create(pid="AQ1234", part="AQ1234", data={"part": "AQ1234"},
                                     flat={"screw_no": "SC-12", "helix_no": "HX-3", "media_material_no": "M-778", "pleat_height_mm": 25})
        FilterModel3D.objects.create(pid="AQ9999", part="AQ9999", data={"part": "AQ9999"}, flat={"screw_no": "SC-40"})

    def test_library_is_a_table_and_tooling_reads_it(self):
        from hmi import connector, integrations, runinfo
        self.assertIn("viewer", connector.ready_dbs())
        cols = {c["name"] for c in integrations.schema("viewer")["tables"][0]["columns"]}
        self.assertTrue({"part_number", "screw_no", "side_a_offset_below_pleat_mm"} <= cols)
        r = engine.execute("sourceCardSave", {"db": "viewer", "table": "filter_parts", "title": "3D library", "filters": [], "columns": []}, self.ctx)
        self.assertTrue(r["ok"], r)
        cid = (r.get("data") or r)["id"]
        r = engine.execute("connectorSave", {"group": "q:part_tooling", "src": cid,
                                             "map": {"f_part": "part_number", "screws": "screw_no", "helix": "helix_no", "media": "media_material_no"}}, self.ctx)
        self.assertTrue(r["ok"], r)
        q = SavedQuery.objects.get(name="part_tooling")
        self.assertTrue(integrations.is_viewer_sql(q.sql))
        self.assertTrue(integrations.query_ready("part_tooling"))
        self.assertFalse(integrations.assets_ready())
        self.assertEqual(integrations.run_saved(q.sql, {"part": "AQ1234"})["rows"],
                         [{"screws": "SC-12", "helix": "HX-3", "media": "M-778"}])
        t = runinfo.tooling("AQ1234", "Barbara")
        self.assertEqual(t["src"], "db")
        self.assertIn("SC-12", json.dumps(t["groups"]))
        g = next(g for g in connector.groups() if g["key"] == "q:part_tooling")
        self.assertEqual((g["db"], g["status"]), ("viewer", "done"))
        f3 = next(g for g in connector.groups() if g["key"] == "q:filter_3d")
        self.assertNotIn("viewer", f3["dbs"])                    # not from itself
        r = engine.execute("connectorSave", {"group": "q:filter_3d", "src": cid, "map": {"f_part": "part_number"}}, self.ctx)
        self.assertFalse(r["ok"])
        # an edit in the viewer shows up straight away
        x = FilterModel3D.objects.get(pid="AQ1234")
        x.flat = {"screw_no": "SC-99"}
        x.save()
        self.assertEqual(integrations.run_saved(q.sql, {"part": "AQ1234"})["rows"][0]["screws"], "SC-99")

    def test_library_is_read_only(self):
        from hmi import integrations
        for bad in ("DELETE FROM filter_parts", "SELECT 1; DROP TABLE filter_parts"):
            with self.assertRaises(integrations.ConnError):
                integrations.run_saved(bad, {}, db="viewer")
        self.assertEqual(FilterModel3D.objects.count(), 2)

    def test_saving_keeps_the_flat_row(self):
        c = Client()
        self.assertTrue(sup_login(c, "4444", "section.quality", "/quality/")["ok"])
        r = c.post("/api/quality/fv/parts/set", json.dumps({"id": "AQ5", "data": {"part": "AQ5"}, "flat": {"screw_no": "S1", "bogus": "x", "notes": ""}}),
                   content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(FilterModel3D.objects.get(pid="AQ5").flat, {"screw_no": "S1"})
        r = c.post("/api/quality/fv/parts/flat", json.dumps({"rows": {"AQ9999": {"helix_no": "H2"}}}), content_type="application/json")
        self.assertEqual(r.json()["n"], 1)
        self.assertEqual(FilterModel3D.objects.get(pid="AQ9999").flat, {"helix_no": "H2"})
        self.assertFalse(next(d for d in c.get("/api/quality/fv/parts").json()["docs"] if d["id"] == "AQ5")["flat"] is False)
        self.assertEqual(Client().post("/api/quality/fv/parts/flat", json.dumps({"rows": {}}), content_type="application/json").status_code, 403)
