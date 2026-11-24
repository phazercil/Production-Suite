"""Each section's tab has its own address; the Scheduler's pages too; switching person needs the name and the PIN."""
import json

from django.test import Client, TestCase

from plant.demo import seed_demo
from plant.models import Person


class Addresses(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.c = Client()
        self.c.get("/supervisor/")
        self.c.post("/api/hmi/login", json.dumps({"pin": "9999"}), content_type="application/json")

    def test_tabs_have_their_own_address(self):
        r = self.c.get("/supervisor/down/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["f"]["tab"], "down")
        self.assertEqual(self.c.get("/supervisor/down/?tab=logs").context["f"]["tab"], "logs")   # a page's own ?tab= link wins
        self.assertEqual(self.c.get("/supervisor/nonsense/")["Location"], "/supervisor/")
        self.assertEqual(self.c.get("/quality/trace/").context["f"]["tab"], "trace")
        self.assertEqual(self.c.get("/maintenance/inventory/").context["tab"], "inventory")
        self.assertEqual(self.c.get("/sensors/log/").status_code, 200)
        self.assertEqual(self.c.get("/admin/backup/").status_code, 200)
        self.assertContains(self.c.get("/kiosk/v/admin"), 'hx-push-url="/admin/backup/"')
        self.assertContains(self.c.get("/kiosk/v/admin"), '"/admin/backup/"')   # the page the address shows is the tab
        self.assertEqual(self.c.get("/supervisor/log/1/").status_code in (200, 404), True)   # its own pages still resolve first

    def test_scheduler_pages(self):
        for p in ("/scheduler/", "/scheduler/lines/", "/scheduler/orders/", "/scheduler/parts/"):
            self.assertContains(self.c.get(p), 'href="/scheduler/s2k/"')
        self.assertEqual(self.c.get("/scheduler/nope/")["Location"], "/scheduler/")

    def test_switching_person_needs_the_name(self):
        def login(**kw):
            return self.c.post("/api/hmi/login", json.dumps(kw), content_type="application/json").json()
        lead = Person.objects.get(pin="4444")
        r = login(pin="4444", switch=True)
        self.assertFalse(r["ok"])
        self.assertIn("Pick your name", r["error"])
        self.assertFalse(login(pin="4444", userId=Person.objects.get(pin="9999").pk, switch=True)["ok"])   # someone else's name
        self.assertTrue(login(pin="4444", userId=lead.pk, switch=True)["ok"])
        names = self.c.get("/api/hmi/people").json()["people"]
        self.assertIn(lead.name, [x["name"] for x in names])
        self.assertNotIn("pin", names[0])

    def test_kiosk_switch_needs_the_name(self):
        k = Client(HTTP_HX_REQUEST="true")
        k.post("/kiosk/do/station", {"line": "Barbara"})
        k.post("/kiosk/do/login", {"pin": "1111"})                   # nobody on this screen yet: the PIN alone is fine
        self.assertEqual(k.session.get("pid"), Person.objects.get(pin="1111").pk)
        r = k.post("/kiosk/do/login", {"pin": "2222"})               # switching: name and PIN
        self.assertContains(r, "Pick your name", status_code=r.status_code)
        k.post("/kiosk/do/login", {"pin": "2222", "user": Person.objects.get(pin="2222").pk})
        self.assertEqual(k.session.get("pid"), Person.objects.get(pin="2222").pk)
        r = k.post("/kiosk/do/changeOp", {"pin": "1111"})
        self.assertContains(r, "Pick your name", status_code=r.status_code)


class LargePlanAndIcon(TestCase):
    def setUp(self):
        seed_demo(reset=True)

    def test_a_big_labor_plan_saves(self):
        c = Client()
        c.get("/supervisor/")
        c.post("/api/hmi/login", json.dumps({"pin": "9999"}), content_type="application/json")
        plan = {"lines": [], "schedules": [], "calendar": {"year": 2027}, "projectName": "Big", "notes": "x" * (3 * 1024 * 1024)}
        r = c.post("/api/labor/save", json.dumps({"budget": "B", "version": 0, "data": plan}), content_type="application/json").json()
        self.assertTrue(r["ok"], r)
        r = c.post("/api/labor/save", json.dumps({"budget": "B", "version": r["version"], "data": {**plan, "notes": "x" * (17 * 1024 * 1024)}}),
                   content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("too large", r.json()["error"])

    def test_tab_icon(self):
        c = Client()
        self.assertContains(c.get("/supervisor/"), "suite/icon.svg")
        self.assertEqual(c.get("/favicon.ico")["Location"], "/static/suite/icon.svg")
