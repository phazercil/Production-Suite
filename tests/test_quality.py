"""Quality: the quality report, the plant-wide tolerances every QC check uses, and running a line by filter number
when it has no MO."""
import json
from datetime import timedelta

from django.core.cache import cache
from django.test import Client, TestCase
from django.utils import timezone

from hmi import engine, qc
from hmi.models import AuditLog, AutoQC, Downtime, QCCheck, ShiftRun
from plant.demo import seed_demo
from plant.models import MO, Person, PlantSettings

from .test_suite import no_planned_time


def set_tols(t):
    st = PlantSettings.load()
    st.config = {**st.config, "qcTol": t}
    st.save()


class ToleranceTests(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.part = {"len": 9.25, "wid": 8.0, "ph": 1.0, "sb": 1.2, "tol": 0.06}
        self.items = qc.items_for("s1")

    def test_part_tolerance_until_the_plant_sets_one(self):
        vals = {"len": 9.30, "wid": 8.0, "ph": 1.0, "sbl": 1.2, "sbr": 1.2, "glue": "Pass", "cut": "Pass", "scrap": 0}
        self.assertEqual(qc.judge(self.items, vals, self.part), (True, []))                 # 0.05 over, part's ±0.06
        set_tols({"len": {"minus": 0.06, "plus": 0.03}})                                    # length: −0.06 / +0.03 for every filter
        self.assertEqual(qc.judge(self.items, vals, self.part), (False, ["len"]))
        self.assertEqual(qc.judge(self.items, {**vals, "len": 9.20}, self.part), (True, []))  # 0.05 under is fine
        self.assertEqual(qc.judge(self.items, {**vals, "len": 9.25, "wid": 8.06}, self.part)[0], True)   # width still the part's ±0.06
        self.assertEqual(qc.fmt_tol("len", self.part), "+0.03 / −0.06")
        self.assertEqual(qc.fmt_tol("wid", self.part), "±0.06")
        set_tols({"sb": {"minus": 0.02, "plus": 0.02}})
        self.assertEqual(qc.judge(self.items, {**vals, "sbr": 1.23}, self.part), (False, ["sbr"]))   # both side bands use it

    def test_setting_them(self):
        lead, op = Person.objects.get(pin="4444"), Person.objects.get(pin="1111")
        r = engine.execute("qcTolSet", {"len_minus": "0.06", "len_plus": "0.03"}, engine.Ctx(user=op))
        self.assertFalse(r["ok"])                                                           # operators can't
        r = engine.execute("qcTolSet", {"len_minus": "abc"}, engine.Ctx(user=lead))
        self.assertIn("in inches", r["error"])
        r = engine.execute("qcTolSet", {"len_minus": "0.06", "len_plus": "0.03", "wid_plus": "0.05", "ph_minus": "", "ph_plus": ""},
                           engine.Ctx(user=lead))
        self.assertTrue(r["ok"], r)
        self.assertEqual(qc.plant_tols(), {"len": {"minus": 0.06, "plus": 0.03}, "wid": {"minus": 0.05, "plus": 0.05}})   # one box: both ways
        self.assertIn("QC tolerances: Length: +0.03 / −0.06; Width: ±0.05; Pleat height: each part's own", AuditLog.objects.latest("id").what)
        engine.execute("qcTolSet", {}, engine.Ctx(user=lead))                               # all blank: back to each part's own
        self.assertEqual(qc.plant_tols(), {})

    def test_machine_vision_uses_them(self):
        from hmi import inbound
        from hmi.models import DataSource
        src = DataSource.objects.create(name="Vision", kind="qc", key_hash="x", mapping={"line": "line", "part": "part", "len": "len"})
        part = ShiftRun.objects.filter(line="Barbara", active=True).first().mo.part
        sp = engine.spec_for(part)
        inbound.ingest(src, {"line": "Barbara", "part": part, "len": sp["len"] + 0.04})
        self.assertTrue(AutoQC.objects.latest("id").passed)
        set_tols({"len": {"minus": 0.03, "plus": 0.03}})
        inbound.ingest(src, {"line": "Barbara", "part": part, "len": sp["len"] + 0.04})
        self.assertFalse(AutoQC.objects.latest("id").passed)


def login(c, pin):
    c.get("/quality/")
    return c.post("/api/hmi/login", json.dumps({"pin": pin, "perm": "section.quality"}), content_type="application/json",
                  HTTP_X_CSRFTOKEN=c.cookies["csrftoken"].value).json()


class QualityPageTests(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.c = Client(enforce_csrf_checks=True)

    def test_shift_leads_only(self):
        r = self.c.get("/quality/")
        self.assertContains(r, "Open Quality")
        self.assertContains(r, 'data-perm="section.quality"')
        self.assertFalse(login(self.c, "1111")["ok"])                                      # an operator
        self.assertContains(self.c.get("/quality/"), "Open Quality")
        self.assertContains(self.c.get("/supervisor/"), "Open Supervisor")                  # the same PIN page, its own words

    def test_report_lists_the_problems(self):
        self.assertTrue(login(self.c, "4444")["ok"])
        sh = ShiftRun.objects.filter(line="Barbara", active=True).select_related("mo").first()
        sp = engine.spec_for(sh.mo.part)
        now = timezone.now()
        QCCheck.objects.create(shift=sh, t=now, line="Barbara", mo=sh.mo.number, item=sh.mo.part, by="J. Ortiz", passed=False, review="pending",
                               values={"len": sp["len"] + 0.2, "glue": "Fail"}, fails=["len", "glue"], scrap=4)
        AutoQC.objects.create(source_name="Camera 1", t=now, line="Barbara", item=sh.mo.part, wid=sp["wid"] - 0.5, passed=False)
        Downtime.objects.create(line="Barbara", start_time=now - timedelta(minutes=30), end_time=now - timedelta(minutes=10), minutes=20,
                                reason="Quality hold", notes="Glue bead skipping", shift=sh)
        r = self.c.get("/quality/")
        self.assertContains(r, "Quality report")
        self.assertContains(r, "over by 0.2")
        self.assertContains(r, "Camera 1")
        self.assertContains(r, "Glue bead skipping")
        self.assertContains(r, ">Waiting<")
        kp = dict(r.context["R"]["kpis"])
        self.assertGreaterEqual(kp["Out of spec"], 1)
        self.assertEqual(kp["Quality hold"], "20 min")
        self.assertIn(("Length", 1), [x for x in r.context["R"]["byWhat"] if x[0] == "Length"] or [("Length", 0)])
        only = self.c.get("/quality/?kind=vision")
        self.assertEqual({x["kind"] for x in only.context["R"]["rows"]}, {"Machine vision"})
        csv = self.c.get("/quality/?kind=hold&csv=1")
        self.assertEqual(csv["Content-Type"].split(";")[0], "text/csv")
        self.assertIn("Glue bead skipping", csv.content.decode())

    def test_tolerances_tab(self):
        self.assertTrue(login(self.c, "4444")["ok"])
        r = self.c.get("/quality/?tab=tol")
        self.assertContains(r, "Tolerances for every filter")
        self.assertContains(r, 'name="len_minus"')
        tok = self.c.cookies["csrftoken"].value
        r = self.c.post("/quality/?tab=tol", {"len_minus": "nope", "csrfmiddlewaretoken": tok})
        self.assertContains(r, "in inches")
        self.assertContains(r, 'value="nope"')                                              # what was typed stays
        r = self.c.post("/quality/?tab=tol", {"len_minus": "0.05", "len_plus": "0.02", "csrfmiddlewaretoken": tok})
        self.assertEqual(r.status_code, 302)
        self.assertContains(self.c.get(r["Location"]), "Tolerances saved")
        self.assertEqual(qc.plant_tols()["len"], {"minus": 0.05, "plus": 0.02})


class NoMoTests(TestCase):
    def setUp(self):
        no_planned_time(self)
        cache.clear()
        seed_demo(reset=True)
        self.c = Client(HTTP_HX_REQUEST="true")
        self.sh = ShiftRun.objects.filter(line="Barbara", active=True).first()
        self.c.post("/kiosk/do/station", {"line": "Barbara"})

    def to_step2(self):
        lead = engine.Ctx(user=Person.objects.get(pin="9999"), line="Barbara")
        self.assertTrue(engine.execute("coOpen", {}, lead)["ok"])
        self.assertTrue(engine.execute("coEnd", {"checks": [True] * 10, "qty": 10}, lead)["ok"])

    def test_operator_needs_a_supervisor(self):
        self.to_step2()
        self.c.post("/kiosk/do/login", {"pin": "1111"})
        r = self.c.get("/kiosk/m/changeover")
        self.assertContains(r, "No MO? Run by filter number")
        r = self.c.get("/kiosk/m/nomo")
        self.assertContains(r, "Running without an MO needs a supervisor")
        self.assertContains(r, 'name="next" value="nomo"')
        r = self.c.post("/kiosk/do/coPickPart", {"part": "MG4062HP", "why": "S2K down"})
        self.assertFalse(MO.objects.filter(source="floor").exists())                          # not without a supervisor
        r = self.c.post("/kiosk/do/supStart", {"pin": "4444", "next": "nomo"})                # the Supervisor's PIN
        self.assertContains(r, "Run without an MO")
        self.assertContains(r, 'name="part"')
        r = self.c.post("/kiosk/do/coPickPart", {"part": " mg4062hp ", "qty": "", "why": ""}, HTTP_X_IN_MODAL="1")
        self.assertContains(r, "Say why")
        r = self.c.post("/kiosk/do/coPickPart", {"part": "mg 4062hp", "qty": "250", "why": "S2K down"}, HTTP_X_IN_MODAL="1")
        self.assertContains(r, "Set up")
        m = MO.objects.get(source="floor")
        self.assertTrue(m.number.startswith("NOMO-BARBARA-"))
        self.assertEqual((m.part, m.qty, m.line.name, m.status), ("MG4062HP", 250, "Barbara", "OPEN"))
        self.sh.refresh_from_db()
        self.assertEqual((self.sh.co["newMo"], self.sh.co["step"], self.sh.co["noMo"]["by"]), (m.id, 3, "S. Lead"))
        self.assertIn("set it to run MG4062HP × 250", AuditLog.objects.latest("id").what)
        # and it starts like any MO
        lead = engine.Ctx(user=Person.objects.get(pin="9999"), line="Barbara")
        sh = ShiftRun.objects.get(pk=self.sh.pk)
        sh.co = {**sh.co, "done": list(range(len(sh.co.get("steps") or PlantSettings.load().config["coSteps"])))}
        sh.save(update_fields=["co"])
        r = engine.execute("coFinish", {}, lead)
        self.assertTrue(r["ok"], r)
        self.sh.refresh_from_db()
        self.assertEqual(self.sh.mo_id, m.id)

    def test_only_after_ending_the_mo_and_a_real_number(self):
        lead = engine.Ctx(user=Person.objects.get(pin="4444"), line="Barbara")
        r = engine.execute("coPickPart", {"part": "MG4062HP", "why": "x"}, lead)
        self.assertIn("End the current MO first", r["error"])
        self.to_step2()
        self.assertIn("letters and numbers", engine.execute("coPickPart", {"part": "MG<4062>", "why": "x"}, lead)["error"])
        self.assertIn("whole number", engine.execute("coPickPart", {"part": "MG4062HP", "qty": "1.5", "why": "x"}, lead)["error"])
        r = engine.execute("coPickPart", {"part": "ZZ9999", "why": "new part"}, lead)
        self.assertIn("isn't in the cost sheet, Parts & specs or the asset database", r["error"])   # a filter number typed wrong
        self.assertFalse(MO.objects.filter(source="floor").exists())
        r = engine.execute("coPickPart", {"part": "MG4062HP", "why": "S2K down"}, lead)
        self.assertTrue(r["ok"], r)
        self.assertEqual(MO.objects.get(source="floor").qty, 0)


class PartInfoTests(TestCase):
    def setUp(self):
        no_planned_time(self)
        cache.clear()
        seed_demo(reset=True)
        self.c = Client(HTTP_HX_REQUEST="true")
        self.c.post("/kiosk/do/station", {"line": "Barbara"})

    def test_drawing_and_dimensions_with_the_plant_tolerances(self):
        part = ShiftRun.objects.filter(line="Barbara", active=True).first().mo.part
        sp = engine.spec_for(part)
        r = self.c.get("/operator/part-info/")
        self.assertContains(r, ">Part Info<")
        r = self.c.get("/kiosk/v/part-info")
        self.assertContains(r, f"Part Info · {part}")
        self.assertContains(r, "running now")
        self.assertContains(r, 'aria-label="Drawing of')
        self.assertContains(r, f"{sp['len'] - sp['tol']:.3f}")                    # min with the part's own tolerance
        set_tols({"len": {"minus": 0.06, "plus": 0.03}})
        r = self.c.get("/kiosk/v/part-info")
        self.assertContains(r, "+0.03 / −0.06")
        self.assertContains(r, f"{sp['len'] + 0.03:.3f}")
        r = self.c.get("/kiosk/v/part-info?part=mg3045")                            # another part, kept while browsing
        self.assertContains(r, "Part Info · MG3045")
        self.assertContains(self.c.get("/kiosk/v/part-info"), "Part Info · MG3045")
        self.assertContains(self.c.get("/kiosk/v/part-info?part="), f"Part Info · {part}")   # back to the one running


def sup_login(c, pin, perm="section.supervisor", page="/supervisor/"):
    c.get(page)
    return c.post("/api/hmi/login", json.dumps({"pin": pin, "perm": perm}), content_type="application/json",
                  HTTP_X_CSRFTOKEN=c.cookies["csrftoken"].value).json()


class ProcessOutTests(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.c = Client(enforce_csrf_checks=True)
        self.sh = ShiftRun.objects.filter(line="Barbara", active=True).select_related("mo").first()
        self.sh.count, self.sh.mo_count = 500, 300
        self.sh.save()
        sp = engine.spec_for(self.sh.mo.part)
        self.q = QCCheck.objects.create(shift=self.sh, t=timezone.now(), line="Barbara", mo=self.sh.mo.number, item=self.sh.mo.part, by="Caleb",
                                        passed=False, review="pending", values={"len": sp["len"] + 1}, fails=["len"])

    def post(self, url, data):
        return self.c.post(url, {**data, "csrfmiddlewaretoken": self.c.cookies["csrftoken"].value})

    def test_process_out_from_supervisor(self):
        self.assertTrue(sup_login(self.c, "4444")["ok"])
        r = self.c.get("/supervisor/?tab=qc&result=fail")
        self.assertContains(r, f"/supervisor/processout/{self.q.pk}/")
        url = f"/supervisor/processout/{self.q.pk}/?back=/supervisor/%3Ftab%3Dqc"
        r = self.c.get(url)
        self.assertContains(r, "How many failed the QC")
        self.assertContains(r, "Glue skip / poor bond")
        self.assertContains(self.post(url, {"qty": "0", "issue": "Bad cut"}), "at least 1")
        self.assertContains(self.post(url, {"qty": "12", "issue": ""}), "Pick what the quality issue was")
        self.assertContains(self.post(url, {"qty": "12", "issue": "Other"}), "Describe the issue")
        r = self.post(url, {"qty": "12", "issue": "Glue skip / poor bond", "note": "Skips on the left side band"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].startswith("/supervisor/?tab=qc"))
        self.q.refresh_from_db()
        self.sh.refresh_from_db()
        self.assertEqual((self.q.review, self.q.scrapped, self.q.issue, self.q.issue_note, self.q.reviewed_by),
                         ("disapproved", 12, "Glue skip / poor bond", "Skips on the left side band", "S. Lead"))
        self.assertEqual((self.sh.count, self.sh.mo_count), (488, 300))                    # off the count
        self.post(url, {"qty": "10", "issue": "Bad cut"})                                     # changed: the count follows
        self.sh.refresh_from_db()
        self.assertEqual(self.sh.count, 490)
        self.assertContains(self.c.get("/supervisor/?tab=qc"), "Processed out")
        r = self.c.get("/quality/")
        self.assertContains(r, "Processed out")
        self.assertIn(("Bad cut", 10), r.context["R"]["byIssue"])
        self.assertContains(self.c.get(f"/supervisor/edit/qc/{self.q.pk}/"), "Change process out")

    def test_needs_approve_permission(self):
        lead = Person.objects.get(pin="4444")
        st = PlantSettings.load()
        st.config = {**st.config, "perms": {"people": {str(lead.pk): {"qc.approve": False}}}}
        st.save()
        self.assertTrue(sup_login(self.c, "4444")["ok"])
        url = f"/supervisor/processout/{self.q.pk}/"
        self.assertContains(self.c.get(url), "needs the <b>Approve out-of-spec QC</b>")
        self.post(url, {"qty": "5", "issue": "Bad cut"})
        self.q.refresh_from_db()
        self.assertEqual(self.q.issue, "")


class QcChecksTabTests(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.c = Client(enforce_csrf_checks=True)
        self.assertTrue(sup_login(self.c, "4444", "section.quality", "/quality/")["ok"])

    def post(self, data):
        return self.c.post("/quality/?tab=checks", {**data, "csrfmiddlewaretoken": self.c.cookies["csrftoken"].value})

    def test_qc_items_and_issues_live_in_quality(self):
        from hmi import kadmin
        self.assertNotIn("qc", dict(kadmin.TABS))                                             # moved out of Admin
        r = self.c.get("/quality/?tab=checks")
        self.assertContains(r, "Stage 1")
        self.assertContains(r, "Side band height L")
        r = self.post({"action": "qcItemAdd", "stage": "s1", "label": "Frame square", "type": "check"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("Frame square", [i["label"] for i in qc.items_for("s1")])
        n = len(qc.items_for("s1"))
        self.post({"action": "qcItemMove", "stage": "s1", "i": str(n - 1), "d": "-1"})
        self.assertEqual(qc.items_for("s1")[n - 2]["label"], "Frame square")
        self.post({"action": "qcItemDel", "stage": "s1", "i": str(n - 2)})
        self.assertNotIn("Frame square", [i["label"] for i in qc.items_for("s1")])
        self.post({"action": "qualityIssuesSet", "issues": "Glue skip\nBad cut\n\nother\nBad cut"})
        self.assertEqual(PlantSettings.load().config["qualityIssues"], ["Glue skip", "Bad cut", "Other"])


class FreshDimensionTests(TestCase):
    """A QC check is judged against the asset database's drawing as it is now, and never against made-up numbers."""

    def setUp(self):
        from hmi.models import SavedQuery
        no_planned_time(self)
        cache.clear()
        seed_demo(reset=True)
        SavedQuery.objects.create(name="part_spec", sql="SELECT 1 FROM part_attribute", saved_by="x")
        self.sh = ShiftRun.objects.filter(line="Barbara", active=True).select_related("mo").first()
        self.ctx = engine.Ctx(user=Person.objects.get(pin="4444"), line="Barbara")

    def save_check(self, length, asset_len):
        from unittest import mock
        sp = self.spec = getattr(self, "spec", None) or engine.spec_for(self.sh.mo.part)   # the other values as first on file
        good = {"len": length, "wid": sp["wid"], "ph": sp["ph"], "sbl": sp["sb"], "sbr": sp["sb"], "glue": "Pass", "cut": "Pass", "scrap": "0",
                "lateReason": "x"}
        rows = {"columns": [], "rows": [{"len": asset_len}], "ms": 1, "truncated": False}
        with mock.patch.object(engine.integrations, "assets_ready", return_value=True), \
                mock.patch.object(engine.integrations, "run_saved", return_value=rows) as rs:
            r = engine.execute("qcSave", good, self.ctx)
        return r, rs

    def test_judged_against_the_latest_drawing(self):
        r, rs = self.save_check(9.80, "9.80")                                                   # the drawing changed to 9.80
        self.assertTrue(rs.called)
        self.assertTrue(r["ok"], r)
        self.assertTrue(QCCheck.objects.latest("id").passed)
        self.assertEqual(engine.spec_for(self.sh.mo.part)["len"], 9.80)
        r, rs = self.save_check(9.80, "9.20")                                                   # and again: read every save
        self.assertTrue(rs.called)
        self.assertFalse(QCCheck.objects.latest("id").passed)

    def test_no_part_no_check(self):
        ShiftRun.objects.filter(pk=self.sh.pk).update(mo=None, co=None)
        r = engine.execute("qcSave", {"len": "1"}, self.ctx)
        self.assertIn("no part to check against", r["error"])
        c = Client(HTTP_HX_REQUEST="true")
        c.post("/kiosk/do/station", {"line": "Barbara"})
        c.post("/kiosk/do/login", {"pin": "1111"})
        self.assertIn("No MO is running", c.get("/kiosk/m/qc").headers.get("HX-Trigger", ""))
        self.assertIn("No MO is running", c.get("/kiosk/m/drawing").headers.get("HX-Trigger", ""))

    def test_supervisor_corrects_a_check_with_no_part(self):
        sp = engine.spec_for(self.sh.mo.part)
        q = QCCheck.objects.create(shift=self.sh, t=timezone.now(), line="Barbara", mo="", item="", by="Caleb", passed=False, review="pending",
                                   values={"len": sp["len"]}, fails=["len"], len=sp["len"])
        c = Client(enforce_csrf_checks=True)
        self.assertTrue(sup_login(c, "4444")["ok"])
        r = c.get(f"/supervisor/edit/qc/{q.pk}/")
        self.assertContains(r, "Missing: the check was saved with no MO running")
        vals = {"len": sp["len"], "wid": sp["wid"], "ph": sp["ph"], "sbl": sp["sb"], "sbr": sp["sb"], "glue": "Pass", "cut": "Pass", "scrap": "0",
                "by": "Caleb", "review": "", "late_reason": "", "why": "No MO was running", "action": "save",
                "csrfmiddlewaretoken": c.cookies["csrftoken"].value}
        r = c.post(f"/supervisor/edit/qc/{q.pk}/", {**vals, "item": ""})
        self.assertContains(r, "Enter the part")
        c.post(f"/supervisor/edit/qc/{q.pk}/", {**vals, "item": self.sh.mo.part.lower()})
        q.refresh_from_db()
        self.assertEqual((q.item, q.passed), (self.sh.mo.part, True))


class PartsTabTests(TestCase):
    """Quality › Parts & specs: the table, importing pasted part numbers from the asset database, and units."""

    def setUp(self):
        from hmi.models import SavedQuery
        seed_demo(reset=True)
        cache.clear()
        self.c = Client(enforce_csrf_checks=True)
        self.assertTrue(sup_login(self.c, "4444", "section.quality", "/quality/")["ok"])
        SavedQuery.objects.create(name="part_spec", sql="SELECT 1 FROM part_attribute", saved_by="x")

    def post(self, data):
        return self.c.post("/quality/?tab=parts", {**data, "csrfmiddlewaretoken": self.c.cookies["csrftoken"].value})

    def test_moved_from_admin_and_saved_as_a_table(self):
        from hmi import kadmin
        from plant.models import PartSpec
        self.assertNotIn("parts", dict(kadmin.TABS))
        r = self.c.get("/quality/?tab=parts")
        self.assertContains(r, "Parts &amp; QC specs")
        self.assertContains(r, "MG4062HP")
        rows = list(PartSpec.objects.order_by("key"))
        data = {"action": "partsSave", "k": [x.key for x in rows], "rev": [x.rev for x in rows], "media": [x.media for x in rows]}
        for f in ("pleats", "len", "wid", "ph", "sb", "tol"):
            data[f] = [str(getattr(x, f)) for x in rows]
        data["len"][0] = "9.999"
        r = self.post(data)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(PartSpec.objects.get(key=rows[0].key).len, 9.999)
        data["wid"][1] = "0"
        self.assertContains(self.post(data), "enter a number above 0")

    def run_import(self, text, rows_by_part):
        from unittest import mock
        def fake(sql, params, limit=500, db="assets"):
            return {"columns": [], "rows": rows_by_part.get(params["part"], []), "ms": 1, "truncated": False}
        with mock.patch.object(engine.integrations, "assets_ready", return_value=True), \
                mock.patch.object(engine.integrations, "run_saved", side_effect=fake):
            return self.post({"action": "partsImport", "parts": text})

    def test_paste_part_numbers_to_import(self):
        from plant.models import PartSpec
        r = self.run_import("ZZ100, zz200\nZZ300\tZZ100", {"ZZ100": [{"len": "241", "len_uom": "mm", "wid": "8.0", "wid_uom": "in", "sb": "30",
                                                                          "uom": "MM", "ph": "1.17", "ph_uom": "in"}],
                                                         "ZZ200": [{"len": "10"}]})
        self.assertEqual(r.status_code, 302)
        msg = self.c.get(r["Location"]).context["msg"]
        self.assertIn("Imported 2 of 3", msg)
        self.assertIn("ZZ300", msg)
        p = PartSpec.objects.get(key="ZZ100")
        self.assertEqual((p.len, p.wid, p.ph, p.sb), (round(241 / 25.4, 4), 8.0, 1.17, round(30 / 25.4, 4)))   # each by its own unit
        self.assertFalse(PartSpec.objects.filter(key="ZZ300").exists())

    def test_units_when_there_is_no_unit_column(self):
        from plant.models import PartSpec
        self.post({"action": "assetUnitsSet", "len": "mm", "wid": "in"})
        self.assertEqual(PlantSettings.load().config["assetUnits"], {"len": "mm"})
        self.run_import("ZZ400", {"ZZ400": [{"len": "254", "wid": "8"}]})
        p = PartSpec.objects.get(key="ZZ400")
        self.assertEqual((p.len, p.wid), (10.0, 8.0))

    def test_unit_column_in_the_folded_query(self):
        from hmi import kadmin
        from hmi.connector import parse_sql
        sql = kadmin.build_codata_sql("part_spec", {"table": "part_attribute", "f_part": "partnumber", "len": "value", "wid": "value", "uom": "uom",
                                                    "slotWhere": {"len": [{"col": "PAID", "val": "30"}], "wid": [{"col": "PAID", "val": "31"}]}}, "mysql")
        self.assertIn("MAX(CASE WHEN `PAID` = '30' THEN `uom` END) AS len_uom", sql)
        self.assertIn("MAX(`uom`) AS uom", sql)
        back = parse_sql(sql)
        self.assertEqual(back["map"], {"len": "value", "wid": "value", "uom": "uom", "f_part": "partnumber"})
        self.assertEqual(set(back["slotWhere"]), {"len", "wid"})


class AlertToggleTests(TestCase):
    def test_on_off_per_person(self):
        from hmi.models import AlertContact
        seed_demo(reset=True)
        a = AlertContact.objects.create(name="Caleb", email="c@example.com")
        c = Client(HTTP_HX_REQUEST="true")
        c.post("/kiosk/do/login", {"pin": "9999"})
        r = c.post("/kiosk/do/alertToggle", {"id": a.pk, "on": "0"})
        self.assertContains(r, "Alerts for Caleb")
        a.refresh_from_db()
        self.assertFalse(a.active)
        c.post("/kiosk/do/alertToggle", {"id": a.pk, "on": "1"})
        a.refresh_from_db()
        self.assertTrue(a.active)


class LineStatusTests(TestCase):
    def test_a_card_per_line(self):
        seed_demo(reset=True)
        c = Client(enforce_csrf_checks=True)
        self.assertTrue(sup_login(c, "4444")["ok"])
        r = c.get("/supervisor/")
        self.assertContains(r, "Line status")
        sh = ShiftRun.objects.filter(line="Barbara", active=True).select_related("mo").first()
        card = next(x for x in r.context["cards"] if x["m"].name == "Barbara")
        self.assertEqual(card["sh"].pk, sh.pk)
        self.assertContains(r, sh.mo.number)
        self.assertContains(r, f"/supervisor/log/{sh.pk}/")
        self.assertIn("Running", r.context["totals"])
        self.assertContains(c.get("/supervisor/?tab=logs"), "Shift logs")


class RecheckTests(TestCase):
    """Once a failed QC check is disapproved or processed out (its scrap counted), a re-check is due on the line right
    away, to prove the issue is corrected."""

    def setUp(self):
        from unittest import mock
        no_planned_time(self)
        p = mock.patch.object(engine, "roll_over", side_effect=lambda sh, t: sh)   # no shift change mid-test
        p.start()
        self.addCleanup(p.stop)
        seed_demo(reset=True)
        st = PlantSettings.load()
        st.settings = {**st.settings, "autoQC": False}   # due even with QC reminders off
        st.save()
        self.sh = ShiftRun.objects.filter(line="Barbara", active=True).select_related("mo").first()
        self.lead = engine.Ctx(user=Person.objects.get(pin="4444"), line="Barbara")
        self.op = engine.Ctx(user=Person.objects.get(pin="1111"), line="Barbara")
        sp = engine.spec_for(self.sh.mo.part)
        self.good = {"len": sp["len"], "wid": sp["wid"], "ph": sp["ph"], "sbl": sp["sb"], "sbr": sp["sb"], "glue": "Pass", "cut": "Pass",
                     "scrap": "0", "lateReason": "fixing the glue gun"}
        self.bad = {**self.good, "len": sp["len"] + 1}
        self.q = QCCheck.objects.create(shift=self.sh, t=timezone.now() - timedelta(minutes=5), line="Barbara", mo=self.sh.mo.number,
                                        item=self.sh.mo.part, by="J. Ortiz", passed=False, review="pending", fails=["len"])

    def due(self):
        self.sh.refresh_from_db()
        return qc.due_at(self.sh, PlantSettings.load().settings, timezone.now())

    def process_out(self, q=None, qty=4):
        r = engine.execute("qcProcessOut", {"id": (q or self.q).pk, "qty": qty, "issue": "Bad cut"}, self.lead)
        self.assertTrue(r["ok"], r)
        return r

    def test_due_right_after_the_scrap_is_counted(self):
        self.assertIsNone(self.due())                                    # waiting for review: nothing due yet
        r = self.process_out()
        self.assertIn("re-check is due", r["msg"])
        self.q.refresh_from_db()
        self.assertIsNotNone(self.q.recheck_due)
        self.assertEqual(self.due(), self.q.recheck_due)                 # due now, even with QC reminders off
        self.assertEqual(self.q.recheck_status()["state"], "due")
        first = self.q.recheck_due
        self.process_out(qty=6)                                          # changing the count doesn't move it
        self.q.refresh_from_db()
        self.assertEqual(self.q.recheck_due, first)

    def test_approved_needs_no_recheck(self):
        r = engine.execute("qcReview", {"id": self.q.pk, "decision": "approved"}, self.lead)
        self.assertTrue(r["ok"], r)
        self.q.refresh_from_db()
        self.assertIsNone(self.q.recheck_due)
        self.assertIsNone(self.due())

    def test_passing_recheck_proves_it_corrected(self):
        self.process_out()
        r = engine.execute("qcSave", self.good, self.op)
        self.assertTrue(r["ok"], r)
        self.assertIn("re-check passed", r["msg"])
        new = QCCheck.objects.latest("id")
        self.q.refresh_from_db()
        self.assertEqual(self.q.recheck_id, new.pk)
        self.assertEqual(self.q.recheck_status()["state"], "passed")
        self.assertIsNone(self.due())                                    # back to normal (reminders off)
        self.assertIn("QC re-check on Barbara", AuditLog.objects.latest("id").what)

    def test_failing_recheck_goes_round_again(self):
        self.process_out()
        engine.execute("qcSave", self.bad, self.op)
        again = QCCheck.objects.latest("id")
        self.q.refresh_from_db()
        self.assertEqual(self.q.recheck_status()["state"], "failed")
        self.assertEqual(again.review, "pending")
        self.assertIsNone(self.due())                                    # its review comes first
        self.process_out(again)
        again.refresh_from_db()
        self.assertEqual(self.due(), again.recheck_due)                  # then another re-check
        engine.execute("qcSave", self.good, self.op)
        again.refresh_from_db()
        self.assertEqual(again.recheck_status()["state"], "passed")

    def test_shown_on_kiosk_supervisor_and_quality(self):
        self.process_out()
        k = Client(HTTP_HX_REQUEST="true")
        k.post("/kiosk/do/station", {"line": "Barbara"})
        k.post("/kiosk/do/login", {"pin": "1111"})
        self.assertContains(k.get("/kiosk/v/qc-log"), "Re-check now")
        self.assertContains(k.get("/kiosk/v/qc-log"), "RE-CHECK DUE")
        self.assertContains(k.get("/kiosk/m/qc"), "Fix the issue first")
        c = Client(enforce_csrf_checks=True)
        self.assertTrue(sup_login(c, "4444")["ok"])
        self.assertContains(c.get("/supervisor/?tab=status"), "Re-check due")
        self.assertContains(c.get("/supervisor/?tab=qc"), "RE-CHECK DUE")
        engine.execute("qcSave", self.good, self.op)
        self.assertNotContains(c.get("/supervisor/?tab=status"), "Re-check due")
        self.assertContains(c.get("/supervisor/?tab=qc"), "RE-CHECK PASSED")
        q = Client(enforce_csrf_checks=True)
        self.assertTrue(sup_login(q, "4444", "section.quality", "/quality/")["ok"])
        self.assertContains(q.get("/quality/"), "RE-CHECK PASSED")
        self.assertContains(k.get("/kiosk/v/qc-log"), "re-check after")



class NoGuessedSpecsTests(TestCase):
    """What the asset database doesn't have is left blank, never guessed; values typed in Parts & specs are kept; Refresh
    re-reads every part."""

    def setUp(self):
        from hmi.models import SavedQuery
        cache.clear()
        seed_demo(reset=True)
        SavedQuery.objects.create(name="part_spec", sql="SELECT 1 FROM part_attribute", saved_by="x")
        self.ctx = engine.Ctx(user=Person.objects.get(pin="4444"))

    def pull(self, row, keys=("ZZ100",), cmd="partsImport"):
        from unittest import mock
        rows = {"columns": [], "rows": [row], "ms": 1, "truncated": False}
        with mock.patch.object(engine.integrations, "assets_ready", return_value=True), \
                mock.patch.object(engine.integrations, "run_saved", return_value=rows):
            return engine.execute(cmd, {"parts": "\n".join(keys)}, self.ctx)

    def test_unknown_values_are_blank_and_typed_ones_kept(self):
        from plant.models import PartSpec
        r = self.pull({"len": "9.5", "wid": "8.0", "rev": "B"})
        self.assertTrue(r["ok"], r)
        ps = PartSpec.objects.get(key="ZZ100")
        self.assertEqual((ps.len, ps.wid, ps.ph, ps.sb, ps.tol, ps.pleats), (9.5, 8.0, None, None, None, None))   # no guesses
        self.assertIsNone(engine.spec_for("ZZ100")["ph"])
        engine.execute("partsSave", {"rows": [{"k": "ZZ100", "pleats": "30", "len": "9.5", "wid": "8.0", "ph": "1.1", "sb": "",
                                               "tol": "", "rev": "B", "media": ""}]}, self.ctx)
        ps.refresh_from_db()
        self.assertEqual((ps.ph, sorted(ps.hand)), (1.1, ["ph", "pleats"]))
        self.pull({"len": "9.6"})                                                             # the database still has no pleat height
        ps.refresh_from_db()
        self.assertEqual((ps.len, ps.ph, ps.pleats, ps.wid), (9.6, 1.1, 30, None))           # typed values stay; unknown ones blank
        self.assertIsNone(engine.part_spec("NOTLISTED")["len"])                              # a part not listed: nothing made up

    def test_refresh_reads_every_part_again(self):
        from plant.models import PartSpec
        self.pull({"len": "9.5"})
        n = PartSpec.objects.count()
        r = self.pull({"len": "9.7"}, keys=(), cmd="partsRefresh")
        self.assertTrue(r["ok"], r)
        self.assertIn("Refreshed from the asset database", r["msg"])
        self.assertEqual(PartSpec.objects.get(key="ZZ100").len, 9.7)
        self.assertGreaterEqual(PartSpec.objects.count(), n)
        c = Client(enforce_csrf_checks=True)
        self.assertTrue(sup_login(c, "4444", "section.quality", "/quality/")["ok"])
        from unittest import mock
        with mock.patch.object(engine.integrations, "assets_ready", return_value=True):
            self.assertContains(c.get("/quality/?tab=parts"), "Refresh from the asset database")


class AddedMeasurementTests(TestCase):
    """A measurement added in Tolerances becomes a column in Parts & specs for every filter, a role in Part specs, a
    QC item, and the page says when the asset database doesn't read it yet."""

    def setUp(self):
        cache.clear()
        seed_demo(reset=True)
        self.lead = engine.Ctx(user=Person.objects.get(pin="4444"))
        self.c = Client(enforce_csrf_checks=True)
        self.assertTrue(sup_login(self.c, "4444", "section.quality", "/quality/")["ok"])

    def post(self, data):
        return self.c.post("/quality/", {**data, "csrfmiddlewaretoken": self.c.cookies["csrftoken"].value})

    def test_add_wire_measure_and_remove(self):
        from hmi.models import SavedQuery
        from hmi.snapshot import qdef
        from plant.models import PartSpec
        r = self.post({"action": "specFeatureAdd", "label": "Frame thickness", "minus": "0.02", "plus": "0.03"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(qc.features(), [{"key": "x1", "label": "Frame thickness"}])
        self.assertEqual(qc.plant_tols()["x1"], {"minus": 0.02, "plus": 0.03})
        self.assertFalse(engine.execute("specFeatureAdd", {"label": "frame thickness"}, self.lead)["ok"])   # no duplicates
        r = self.c.get("/quality/?tab=parts")
        self.assertContains(r, "<th>Frame thickness</th>", html=False)
        self.assertContains(r, "Not connected to the asset database yet")
        self.assertIn(["x1", "Frame thickness"], qdef()["part_spec"]["roles"])                # a role in the Connector builder
        # once Part specs reads it, every filter gets its value
        SavedQuery.objects.create(name="part_spec", sql="SELECT a.v AS len, b.v AS x1 FROM t", saved_by="x")
        self.assertNotContains(self.c.get("/quality/?tab=parts"), "Not connected to the asset database yet")
        from unittest import mock
        rows = {"columns": [], "rows": [{"len": "9.5", "x1": "0.75"}], "ms": 1, "truncated": False}
        with mock.patch.object(engine.integrations, "assets_ready", return_value=True), \
                mock.patch.object(engine.integrations, "run_saved", return_value=rows):
            self.assertTrue(engine.execute("partsImport", {"parts": "ZZ200"}, self.lead)["ok"])
        self.assertEqual(PartSpec.objects.get(key="ZZ200").extra, {"x1": 0.75})
        self.assertEqual(engine.spec_for("ZZ200")["x1"], 0.75)
        # a QC check can measure it, against the part's value and its tolerance
        self.assertTrue(engine.execute("qcItemAdd", {"stage": "s1", "key": "x1"}, self.lead)["ok"])
        it = next(i for i in qc.items_for("s1") if i["key"] == "x1")
        self.assertEqual(qc.judge([it], {"x1": 0.78}, engine.spec_for("ZZ200")), (True, []))
        self.assertEqual(qc.judge([it], {"x1": 0.79}, engine.spec_for("ZZ200")), (False, ["x1"]))
        # typed in Parts & specs
        engine.execute("partsSave", {"rows": [{"k": "ZZ200", "pleats": "", "len": "9.5", "wid": "", "ph": "", "sb": "", "tol": "",
                                               "x1": "0.8", "rev": "", "media": ""}]}, self.lead)
        self.assertEqual(PartSpec.objects.get(key="ZZ200").extra, {"x1": 0.8})
        self.post({"action": "specFeatureDel", "key": "x1"})
        self.assertEqual(qc.features(), [])
        self.assertNotIn("x1", [i["key"] for i in qc.items_for("s1")])
