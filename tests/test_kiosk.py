"""Operator Kiosk built on the server: every page and Admin tab renders, and the actions behind its forms do their work."""
import json
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase

from hmi import kadmin, kscreen
from plant.demo import seed_demo
from plant.models import Machine, PlantSettings

from .test_suite import no_planned_time

SCHEMA = {"err": "", "ready": True, "tables": [
    {"name": "open_orders", "columns": [{"name": n} for n in ("mo_number", "item_number", "qty_open", "due_date", "status")]},
    {"name": "co_steps", "columns": [{"name": n} for n in ("part_no", "machine", "step_no", "instruction", "note")]}]}


class KioskTests(TestCase):
    def setUp(self):
        no_planned_time(self)
        cache.clear()
        kadmin._tables_cache.clear()
        seed_demo(reset=True)
        self.c = Client(HTTP_HX_REQUEST="true")

    def do(self, action, **data):
        return self.c.post(f"/kiosk/do/{action}", data)

    def login_lead(self):
        r = self.do("login", pin="9999")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Logged in", r.headers.get("HX-Trigger", ""))

    def toast(self, r):
        return json.loads(r.headers.get("HX-Trigger") or "{}").get("suite:toast", {})

    def test_every_page_renders_for_anyone_and_a_shift_lead(self):
        for lead in (False, True):
            if lead:
                self.login_lead()
            r = self.c.get("/operator/")
            self.assertContains(r, "htmx.min.js")
            self.assertNotContains(r, "kiosk.js\"")
            for page, _ in filter(None, kscreen.PAGES):
                r = self.c.get(f"/kiosk/v/{page}")
                self.assertEqual(r.status_code, 200, page)

    def test_every_admin_tab_renders(self):
        self.login_lead()
        for tab, _ in kadmin.TABS:
            r = self.c.get(f"/kiosk/v/admin?tab={tab}")
            self.assertEqual(r.status_code, 200, tab)
            self.assertContains(r, 'aria-selected="true"')
        r = self.c.get("/kiosk/v/admin?tab=connections")
        self.assertContains(r, 'id="accounts"')
        self.assertContains(r, 'id="mc-')
        self.assertNotContains(r, "password\" value=")     # passwords never go back to the screen
        r = self.c.get("/kiosk/v/admin?tab=shifts")
        self.assertContains(r, "1st shift")
        r = self.c.get("/kiosk/v/admin?tab=parts")
        self.assertContains(r, "MG4062HP")

    def test_admin_actions_need_a_shift_lead(self):
        r = self.do("mdSet", id="Barbara", f="feed.type", v="none")
        self.assertNotEqual(Machine.objects.get(name="Barbara").feed_type, "none")
        self.assertIn(b"PIN", r.content)         # the login dialog instead
        r = self.do("s2k", op="build")
        self.assertIn(b"PIN", r.content)

    def test_machine_card_follows_its_source(self):
        self.login_lead()
        r = self.do("mdSet", id="Barbara", f="feed.type", v="db")
        self.assertEqual(Machine.objects.get(name="Barbara").feed_type, "db")
        self.assertContains(r, "Newest row by")
        self.assertContains(r, "Set up the machine data database")
        r = self.do("mdSet", id="Barbara", f="uptime.type", v="feed")
        self.assertContains(r, "Running when the signal is")
        r = self.do("mdSet", id="Barbara", f="feed.type", v="http")
        r = self.do("mdSet", id="Barbara", f="feed.url", v="http://8.8.8.8/x")
        self.assertIn("public internet", self.toast(r).get("text", ""))
        self.assertEqual(Machine.objects.get(name="Barbara").feed_url, "")

    def test_kiosk_panels_are_read_on_the_server(self):
        self.login_lead()
        r = self.do("kioskPanels", text="Speed = 2\nnot a panel")
        self.assertIn("Line 2", self.toast(r)["text"])
        self.do("kioskPanels", text="Speed = 2\n\nUptime = 4")
        self.assertEqual(PlantSettings.load().settings["kiosk"]["panels"], [{"title": "Speed", "panel": 2}, {"title": "Uptime", "panel": 4}])

    def test_s2k_query_is_written_on_the_server(self):
        self.login_lead()
        with mock.patch.object(kadmin, "tables", return_value=SCHEMA), mock.patch.object(kadmin.integrations, "s2k_db_ready", return_value=True):
            r = self.do("s2k", op="source", v="db")
            self.assertContains(r, "Show tables")
            r = self.do("s2k", op="pick", changed="b_table", b_table="open_orders", shown="1")
            self.assertContains(r, "<option selected>open_orders</option>", html=False)
            self.assertContains(r, "mo_number")
            r = self.do("s2k", op="build", b_table="open_orders", b_mo="mo_number", b_item="item_number", b_qty="qty_open",
                        b_fcol="status", b_fval="OPEN", shown="1")
            self.assertContains(r, "SELECT `mo_number` AS mo")
            self.assertContains(r, "WHERE `status` = &#x27;OPEN&#x27;")
            r = self.do("s2k", op="build", b_table="open_orders", shown="1")
            self.assertContains(r, "Pick a column for: MO number")

    def test_changeover_query_builder(self):
        self.login_lead()
        with mock.patch.object(kadmin, "tables", return_value=SCHEMA), mock.patch.object(kadmin.integrations, "assets_ready", return_value=True):
            r = self.c.get("/kiosk/v/admin?tab=codata")
            self.assertContains(r, "Changeover steps")
            r = self.do("codata", op="pick", q="changeover_steps", changed="b_table", b_table="co_steps")
            body = r.content.decode()
            self.assertIn("data-hold", body)
            self.assertIn("<option selected>instruction</option>", body)   # columns guessed from their names
            r = self.do("codata", op="build", q="changeover_steps", b_table="co_steps", b_step="instruction", b_seq="step_no", b_f_part="part_no")
            self.assertContains(r, "WHERE `part_no` = :part")
            self.assertContains(r, "ORDER BY `step_no`")

    def test_data_source_editor(self):
        self.login_lead()
        r = self.do("srcEdit")
        self.assertContains(r, "New data source")
        r = self.do("srcEdit", redraw="1", name="vision-1", kind="qc", mode="poll")
        self.assertContains(r, 'value="vision-1"')
        self.assertContains(r, "Address to collect from")
        r = self.do("srcSave", name="vision-1", kind="qc", mode="push", enabled="1", map_len="result.length", map_units="mm")
        self.assertContains(r, "/api/ingest/vision-1")      # the key, shown once
        self.assertContains(r, "X-Source-Key")

    def test_confirm_dialog_can_send_its_answer_to_a_panel(self):
        self.login_lead()
        r = self.c.get("/kiosk/m/confirm?a=srcDelete&id=3&t=%23sources")
        self.assertContains(r, 'hx-target="#sources"')
        r = self.c.get("/kiosk/m/confirm?a=srcDelete&id=3&t=body%3Ex")
        self.assertNotContains(r, "hx-target=")
