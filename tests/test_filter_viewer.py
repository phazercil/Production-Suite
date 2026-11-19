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
        row = {"columns": [], "rows": [{"ph": "1.25", "pc": "30", "pp": "0.4", "uom": "in"}], "ms": 1, "truncated": False}
        with mock.patch.object(engine.integrations, "assets_ready", return_value=True), \
                mock.patch.object(engine.integrations, "run_saved", return_value=row):
            msg = viewer.refresh_from_db(x, force=True)
        self.assertIn("from the asset database", msg)
        P = FilterModel3D.objects.get(pid="AQ1136C").data["config"]["P"]
        self.assertEqual((P["ph"], P["pc"], P["pp"], P["fl"]), (31.75, 30, 10.16, 304.8))     # inches → mm; length = count × pitch
        self.assertEqual((P["mw"], P["mt"]), (215, 0.8))                                      # not in the database: as stored
        c = Client(enforce_csrf_checks=True)
        self.assertTrue(sup_login(c, "4444", "section.quality", "/quality/")["ok"])
        with mock.patch.object(engine.integrations, "assets_ready", return_value=True), \
                mock.patch.object(engine.integrations, "run_saved", return_value=row):
            r = c.post("/quality/viewer/refresh-all/", {"csrfmiddlewaretoken": c.cookies["csrftoken"].value})
        self.assertIn("Read+1+filter", r["Location"])
        from hmi.snapshot import qdef
        self.assertIn("filter_3d", qdef())                                       # its own entry in the Connector builder
