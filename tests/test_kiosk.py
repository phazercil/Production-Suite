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
            r = self.c.get("/kiosk/v/admin?tab=codata")
            self.assertContains(r, "Tooling for the part")
            r = self.do("codata", op="build", q="part_tooling", b_table="co_steps", b_f_part="part_no")
            self.assertContains(r, "Pick at least one column to show.")
            r = self.do("codata", op="build", q="part_tooling", b_table="co_steps", b_f_part="part_no", b_kind="note", b_name="instruction")
            self.assertContains(r, "`instruction` AS name")

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

    def test_a_shift_lead_can_rename_someone(self):
        from plant.models import Person
        self.login_lead()
        lead = Person.objects.get(pin="9999")
        self.assertContains(self.c.get("/kiosk/v/team"), 'aria-label="Name"')
        self.do("userSet", id=lead.id, f="name", v="  Caleb   Smith ")
        self.assertEqual(Person.objects.get(pk=lead.pk).name, "Caleb Smith")
        r = self.do("userSet", id=lead.id, f="name", v="   ")
        self.assertIn("Type a name", self.toast(r)["text"])

    def test_shift_lead_and_admin(self):
        """Admin: system settings. Shift lead: floor work and the day-to-day lists, never an Admin's login."""
        from plant.models import Person
        self.do("login", pin="4444")                           # S. Lead, a shift lead
        r = self.do("run", c="listAdd", k="reasons", v="Glue skips")
        self.assertNotIn("bad", json.dumps(self.toast(r)))
        self.assertIn("Glue skips", PlantSettings.load().config["reasons"])
        r = self.do("mdSet", id="Barbara", f="feed.type", v="none")
        self.assertContains(r, "Approval")                      # Machines need someone allowed to change them
        self.assertContains(r, "<b>Machines</b>")
        self.assertNotEqual(Machine.objects.get(name="Barbara").feed_type, "none")
        r = self.do("run", c="settingSet", k="webhook", v="http://192.168.100.9/x")
        self.assertIn("doesn't have permission", self.toast(r)["text"])
        self.do("run", c="settingSet", k="qcEvery", v="45")
        self.assertEqual(PlantSettings.load().settings["qcEvery"], 45)
        admin = Person.objects.get(pin="9999")
        r = self.do("userSet", id=admin.id, f="pin", v="123987")
        self.assertIn("manages permissions", self.toast(r)["text"])
        op = Person.objects.get(pin="1111")
        r = self.do("userSet", id=op.id, f="role", v="admin")
        self.assertIn("manages permissions", self.toast(r)["text"])
        self.do("userSet", id=op.id, f="role", v="lead")
        self.assertEqual(Person.objects.get(pk=op.pk).role, "lead")
        r = self.c.get("/kiosk/v/admin?tab=connections")
        self.assertContains(r, "Changing this needs permission for <b>Connections</b>")
        self.assertNotContains(r, 'id="accounts"')               # logins and secrets are an Admin's
        # the last Admin can't be demoted
        self.do("logout"); self.do("login", pin="9999")
        r = self.do("userSet", id=admin.id, f="role", v="lead")
        self.assertIn("only Admin", self.toast(r)["text"])

    def test_admin_is_its_own_section(self):
        r = self.c.get("/admin/")
        self.assertContains(r, 'href="/admin/" aria-current="page"')
        self.assertContains(r, 'hx-get="/kiosk/v/admin"')
        self.assertRedirects(self.c.get("/operator/admin/?tab=alerts"), "/admin/?tab=alerts", fetch_redirect_response=False)
        self.assertNotContains(self.c.get("/operator/"), 'href="/operator/admin/"')
        self.assertEqual(self.c.get("/db-admin/").status_code, 302)     # Django's own table editor moved here

    def test_permission_manager(self):
        from plant import perms
        from plant.models import Person
        lead, op = Person.objects.get(pin="4444"), Person.objects.get(pin="1111")
        self.assertFalse(perms.can(lead, "machines.edit"))
        self.do("login", pin="4444")
        r = self.do("run", c="permRole", key="machines.edit", role="lead", **{"v:b": "1"})
        self.assertContains(r, "Approval")                         # managing permissions needs it
        self.do("logout"); self.do("login", pin="9999")
        self.assertContains(self.c.get("/kiosk/v/admin?tab=permissions"), "Permissions by role")
        self.do("run", c="permRole", key="machines.edit", role="lead", **{"v:b": "1"})
        self.assertTrue(perms.can(lead, "machines.edit"))           # a role
        self.do("run", c="permPerson", id=lead.id, key="machines.edit", v="deny")
        self.assertFalse(perms.can(lead, "machines.edit"))          # a person wins over their role
        self.do("run", c="permPerson", id=op.id, key="labor.view", v="allow")
        self.assertTrue(perms.can(op, "labor.view"))
        r = self.do("run", c="permRole", key="permissions.manage", role="admin")   # unticked
        self.assertTrue(perms.can(Person.objects.get(pin="9999"), "permissions.manage"))   # can't lock the plant out
        r = self.c.get("/kiosk/v/admin?tab=permissions&who=%d" % lead.id)
        self.assertContains(r, "Permissions for one person")
        self.do("run", c="permReset")
        self.assertFalse(perms.can(lead, "machines.edit"))
        self.assertFalse(perms.can(op, "labor.view"))

    def test_sections_and_budget_only_link(self):
        from plant import perms
        from plant.models import Person
        r = Client().get("/operator/")
        for href in ("/supervisor/", "/maintenance/", "/admin/", "/labor/"):
            self.assertContains(r, f'href="{href}"')
        self.assertContains(Client().get("/supervisor/"), "packaging")
        self.assertContains(Client().get("/maintenance/"), "Maintenance")
        # /budget/: the Labor Budget alone, for someone allowed only that
        op = Person.objects.get(pin="1111")
        c = Client()
        r = c.get("/budget/")
        self.assertNotContains(r, 'class="suitetabs"')
        self.assertContains(r, "Open Labor Budget")
        r = c.post("/api/hmi/login", json.dumps({"pin": "1111", "perm": "labor.view"}), content_type="application/json").json()
        self.assertFalse(r["ok"])                                   # not allowed yet
        self.c.post("/kiosk/do/login", {"pin": "9999"})
        self.do("run", c="permPerson", id=op.id, key="labor.view", v="allow")
        self.assertTrue(perms.can(op, "labor.view"))
        r = c.post("/api/hmi/login", json.dumps({"pin": "1111", "perm": "labor.view"}), content_type="application/json").json()
        self.assertTrue(r["ok"], r)
        r = c.get("/budget/")
        self.assertContains(r, "MFG Labor")
        self.assertNotContains(r, 'class="suitetabs"')             # no other sections
        self.assertContains(c.get("/labor/"), 'class="suitetabs"')
        self.assertEqual(c.get("/api/labor/state").status_code, 200)
        r = c.post("/api/labor/save", json.dumps({"version": 0, "data": {"lines": [], "schedules": []}}), content_type="application/json")
        self.assertEqual(r.status_code, 403)                         # viewing isn't changing

    def test_supervisor_override(self):
        from hmi.models import AuditLog, Downtime
        d = Downtime.objects.filter(line="Barbara").exclude(reason=None).first() or Downtime.objects.filter(line="Barbara").first()
        Downtime.objects.filter(pk=d.pk).update(reason="Jam or misfeed", by="M. Lee")
        self.do("login", pin="1111")                                     # J. Ortiz, an operator
        r = self.do("downtimeSet", id=d.id, reason="Something not on the list")
        self.assertIn("Pick a reason from the list", json.dumps(self.toast(r)))
        self.assertContains(self.c.get("/kiosk/r/rail"), "Supervisor")
        r = self.c.get("/kiosk/m/login?sup=1")
        self.assertContains(r, "Supervisor override")
        self.assertContains(r, "K. Patel<br>")                             # maintenance may override
        self.assertNotContains(r, "J. Ortiz<br>")
        r = self.do("supStart", pin="1111")
        self.assertContains(r, "can&#x27;t do this")
        r = self.do("supStart", pin="3333")                                # K. Patel
        self.assertIn("Supervisor override: K. Patel", self.toast(r)["text"])
        self.assertContains(self.c.get("/kiosk/r/rail"), "End override")
        self.assertContains(self.c.get("/kiosk/r/rail"), "Log out")        # the operator is still logged in
        r = self.do("downtimeSet", id=d.id, reason="Something not on the list")
        self.assertEqual(Downtime.objects.get(pk=d.pk).reason, "Something not on the list")
        self.assertTrue(AuditLog.objects.filter(what__startswith="Supervisor override").exists())
        self.do("supEnd")
        self.assertContains(self.c.get("/kiosk/r/rail"), ">Supervisor<")
        r = self.do("downtimeSet", id=d.id, reason="Another")
        self.assertIn("Pick a reason", json.dumps(self.toast(r)))

    def qc_shift(self):
        from hmi.models import ShiftRun
        sh = ShiftRun.objects.filter(line="Barbara", active=True).select_related("mo").first()
        self.assertIsNotNone(sh, "the demo has a shift running on Barbara")
        return sh

    def test_qc_items_per_stage(self):
        from hmi import qc
        self.do("login", pin="4444")
        self.do("run", c="qcItemDel", stage="s1", i="6")                 # Cut
        self.do("run", c="qcItemAdd", stage="s1", label="Frame square", type="check")
        self.do("run", c="qcItemAdd", stage="s1", label="Glue temp", type="number", min="340", max="360")
        keys = [i["key"] for i in qc.items_for("s1")]
        self.assertNotIn("cut", keys)
        self.assertEqual(keys[-2:], ["c1", "c2"])
        self.assertEqual([i["key"] for i in qc.items_for("eb")], ["glue", "scrap"])   # each stage its own
        self.assertContains(self.c.get("/kiosk/v/admin?tab=qc"), "Frame square")
        r = self.c.get("/kiosk/m/qc")
        self.assertContains(r, "Frame square")
        self.assertContains(r, "Glue temp")
        self.assertNotContains(r, ">Cut<")

    def test_qc_after_changeover_late_and_supervisor_review(self):
        from django.utils import timezone

        from hmi import engine
        from hmi.models import QCCheck, ShiftRun
        from plant.models import MO
        sh = self.qc_shift()
        PlantSettings.objects.filter(pk=1)   # autoQC on in the demo?
        st = PlantSettings.load(); st.settings = {**st.settings, "autoQC": True, "qcEvery": 30}; st.save()
        # right after a changeover: due as soon as the machine runs
        ShiftRun.objects.filter(pk=sh.pk).update(qc_after_co=True, qc_due_at=None, state="running")
        engine.tick(Machine.objects.get(name="Barbara"), {"ppm": 200}, timezone.now())
        sh.refresh_from_db()
        self.assertIsNotNone(sh.qc_due_at)
        self.do("login", pin="1111")
        self.assertContains(self.c.get("/kiosk/m/qc"), "first check after the changeover")
        # 25 minutes later it is late: a reason is needed
        ShiftRun.objects.filter(pk=sh.pk).update(qc_due_at=timezone.now() - timezone.timedelta(minutes=25))
        self.assertContains(self.c.get("/kiosk/m/qc"), "min late")
        spec = engine.spec_for(sh.mo.part)
        good = {"len": spec["len"], "wid": spec["wid"], "ph": spec["ph"], "sbl": spec["sb"], "sbr": spec["sb"], "glue": "Pass", "cut": "Pass", "scrap": "0"}
        before = QCCheck.objects.count()
        r = self.do("qcSave", **good)
        self.assertContains(r, "late")
        self.assertEqual(QCCheck.objects.count(), before)
        r = self.do("qcSave", lateReason="Jam right after start-up", **{**good, "len": spec["len"] + 1})
        q = QCCheck.objects.filter(shift=sh).latest("id")
        self.assertEqual((q.passed, q.review, q.fails, q.late_reason), (False, "pending", ["len"], "Jam right after start-up"))
        sh.refresh_from_db()
        self.assertFalse(sh.qc_after_co)
        self.assertContains(self.c.get("/kiosk/v/qc-log"), "OUT OF SPEC")
        self.assertNotContains(self.c.get("/kiosk/v/qc-log"), ">Disapprove<")   # an operator can't
        r = self.do("qcReview", id=q.id, decision="approved")
        self.assertContains(r, "Approval")
        # the supervisor takes over the screen and disapproves, scrapping 12
        self.do("supStart", pin="4444")
        self.assertContains(self.c.get("/kiosk/v/qc-log"), ">Disapprove<")
        ShiftRun.objects.filter(pk=sh.pk).update(count=100, mo_count=50)
        MO.objects.filter(pk=sh.mo_id).update(produced=500, scrap=0)
        r = self.do("qcReview", id=q.id, decision="disapproved", scrap="12")
        q.refresh_from_db(); sh.refresh_from_db()
        self.assertEqual((q.review, q.scrapped, q.reviewed_by), ("disapproved", 12, "S. Lead"))
        self.assertEqual((sh.count, sh.mo_count), (88, 38))
        mo = MO.objects.get(pk=sh.mo_id)
        self.assertEqual((mo.produced, mo.scrap), (488, 12))
        # and corrects the check
        self.assertContains(self.c.get(f"/kiosk/m/qc?edit={q.id}"), "Save changes")
        self.do("qcSave", id=q.id, **good)
        q.refresh_from_db()
        self.assertTrue(q.passed)
        self.assertEqual(q.edited_by, "S. Lead")
