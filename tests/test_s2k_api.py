"""S2K open MOs from the HTTPS API: company and location filters, the token header from .env, and the pull."""
from unittest import mock

from django.conf import settings
from django.test import Client, TestCase

from hmi import engine, integrations
from plant import liveconfig
from plant.demo import seed_demo
from plant.models import MO, Person, PlantSettings


class S2kApiTests(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.lead = engine.Ctx(user=Person.objects.get(pin="9999"))
        saved = {}
        p1 = mock.patch.object(liveconfig, "save", side_effect=lambda v: (saved.update(v), settings.SUITE.update({k: v or "" for k, v in v.items()})))
        p2 = mock.patch.object(liveconfig, "refresh", return_value=None)
        p1.start(), p2.start()
        self.addCleanup(p1.stop), self.addCleanup(p2.stop)
        self.addCleanup(lambda: [settings.SUITE.pop(k, None) for k in ("S2K_API_URL", "S2K_API_COMPANY", "S2K_API_LOCATION", "S2K_API_TOKEN")])
        self.saved = saved

    def info(self):
        s = settings.SUITE
        return {"s2kapi": {"on": bool(s.get("S2K_API_URL") and s.get("S2K_API_TOKEN")), "url": s.get("S2K_API_URL", ""),
                           "company": s.get("S2K_API_COMPANY", ""), "location": s.get("S2K_API_LOCATION", ""), "hasToken": bool(s.get("S2K_API_TOKEN"))}}

    def save(self, **f):
        with mock.patch.object(liveconfig, "info", side_effect=self.info):
            return engine.execute("connSave", {"group": "s2kapi", "fields": f}, self.lead)

    def test_saved_in_env_with_its_filters(self):
        self.assertIn("https://", self.save(url="http://api.aps.dev/x", company="1", location="07", token="t")["error"])
        self.assertIn("token", self.save(url="https://api.aps.dev/datalake/api/open-mos/", company="1", location="07")["error"])
        self.assertIn("location", self.save(url="https://api.aps.dev/datalake/api/open-mos/", company="1", location="", token="t")["error"])
        r = self.save(url="https://api.aps.dev/datalake/api/open-mos/?company=9&location=99", company="1", location="07", token="secret-123")
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.saved, {"S2K_API_URL": "https://api.aps.dev/datalake/api/open-mos/", "S2K_API_COMPANY": "1",
                                      "S2K_API_LOCATION": "07", "S2K_API_TOKEN": "secret-123"})
        self.assertEqual(integrations.s2k_api_url(), "https://api.aps.dev/datalake/api/open-mos/?company=1&location=07")
        r = self.save(url="https://api.aps.dev/datalake/api/open-mos/", company="1", location="08")   # blank token: the saved one stays
        self.assertTrue(r["ok"], r)
        self.assertEqual(settings.SUITE["S2K_API_TOKEN"], "secret-123")

    def test_pull_sends_the_token_and_adds_new_mos(self):
        settings.SUITE.update(S2K_API_URL="https://api.aps.dev/datalake/api/open-mos/", S2K_API_COMPANY="1", S2K_API_LOCATION="07",
                              S2K_API_TOKEN="secret-123")
        st = PlantSettings.load()
        st.settings = {**st.settings, "s2kSource": "api"}
        st.save()
        rows = [{"mo": "318555-002", "item": "MG4062HP", "qty": 1200, "due": "2026-11-20"}, {"mo": "318556", "item": "MG3045", "qty": 50, "due": "2026-11-21"}]
        with mock.patch.object(integrations, "_request", return_value=rows) as req:
            r = engine.execute("s2kPull", {}, self.lead)
        self.assertTrue(r["ok"], r)
        url = req.call_args[0][0]
        self.assertEqual(url, "https://api.aps.dev/datalake/api/open-mos/?company=1&location=07")
        self.assertEqual(req.call_args[1]["headers"], {"X-Api-Token": "secret-123"})
        self.assertTrue(req.call_args[1]["public_ok"])                     # the one opted-in internet address
        m = MO.objects.get(number="318555-002")
        self.assertEqual((m.part, m.qty, str(m.due)), ("MG4062HP", 1200, "2026-11-20"))
        self.assertTrue(MO.objects.filter(number="318556").exists())
        self.assertTrue(engine.s2k_on(PlantSettings.load().settings))

    def test_admin_panel(self):
        c = Client(HTTP_HX_REQUEST="true")
        c.post("/kiosk/do/login", {"pin": "9999"})
        r = c.post("/kiosk/do/s2k", {"op": "source", "v": "api"})
        self.assertContains(r, "The S2K API (JSON over HTTPS, with a token)")
        self.assertContains(r, 'name="company"')
        self.assertContains(r, "internet connection, made only to this address")
        with mock.patch.object(liveconfig, "info", side_effect=lambda: {**self.info(), **{k: {} for k in ("assets",)}}):
            r = c.post("/kiosk/do/s2k", {"op": "apiSave", "url": "https://api.aps.dev/datalake/api/open-mos/", "company": "1", "location": "07", "token": "tok"})
        self.assertContains(r, "S2K API saved")
        with mock.patch.object(integrations, "_request", return_value=[{"mo": "1", "item": "A", "qty": 2, "due": "2026-11-20"}]):
            r = c.post("/kiosk/do/s2k", {"op": "apiTest"})
        self.assertContains(r, "1 open MO from the S2K API")
