"""Planned maintenance, QC / PM alerts, roll-to-MO traceability and reading roll tags from a picture."""
import io
import json
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.utils import timezone

from hmi import alerts, engine, pm, rolltag, trace
from hmi.models import (AlertContact, AlertLog, MoRun, PMTask, PpmReading, QCCheck, RollTag, RollTagPhoto, ShiftRun, Ticket)
from plant.demo import seed_demo
from plant.models import Machine, Person, PlantSettings

from .test_maintenance import picture


def post(c, path, data):
    return c.post(path, json.dumps(data), content_type="application/json")


def cmd(c, name, p, line="Barbara"):
    return post(c, "/api/hmi/cmd", {"name": name, "p": p, "line": line}).json()


def login(c, pin):
    r = post(c, "/api/hmi/login", {"pin": pin}).json()
    assert r["ok"], r
    return r


def ctx(pin="4444", t=None):
    return engine.Ctx(user=Person.objects.get(pin=pin), line="Barbara", t=t)


def readings(line, start, minutes, units=3, run=True):
    PpmReading.objects.bulk_create([PpmReading(line=line, t=start + timedelta(minutes=i), ppm=180 if run else 0, units=units if run else 0, run=run)
                                    for i in range(minutes)])


def sync_thread(target, **kw):
    class T:
        def start(self):
            target()
    return T()


class Base(TestCase):
    def setUp(self):
        cache.clear()
        engine._planned_cache.clear()
        seed_demo(reset=True)
        self.bar = Machine.objects.get(name="Barbara")
        self.t = timezone.now()
        PpmReading.objects.all().delete()   # (the demo's readings and planned maintenance)
        PMTask.objects.all().delete()


def on_shift(op, line="Barbara"):
    """J. Ortiz logged in on a line with a shift running (the demo may already have one going)."""
    login(op, "1111")
    if not ShiftRun.objects.filter(line=line, active=True).exists():
        r = cmd(op, "startShift", {"line": line, "mo": "309215", "safety": [True] * 5}, line)
        assert r["ok"], r
    p = Person.objects.get(pin="1111")
    ShiftRun.objects.filter(line=line, active=True).update(person=p, user_name=p.name)
    return ShiftRun.objects.get(line=line, active=True)


# ------------------------------------------------------------------ planned maintenance
class PMTests(Base):
    def test_a_task_by_days_opens_one_request_when_due_and_completing_it_starts_again(self):
        task = PMTask.objects.create(machine="Barbara", title="Grease chain", every=7, unit="days", created=self.t - timedelta(days=8),
                                     steps="Grease all 4 points")
        opened = pm.check(self.t)
        self.assertEqual(len(opened), 1)
        tk = opened[0]
        self.assertEqual((tk.pm_id, tk.machine, tk.type, tk.status), (task.id, "Barbara", "PM · Grease chain", "Open"))
        self.assertIn("Grease all 4 points", tk.notes)
        self.assertEqual(pm.check(self.t), [])            # one request at a time
        r = engine.execute("ticketStatus", {"id": tk.id, "close": True, "note": "done"}, ctx("3333"))
        self.assertTrue(r["ok"], r)
        task.refresh_from_db()
        self.assertEqual(task.last_by, "K. Patel")
        self.assertEqual(pm.status(task, self.t)["state"], "ok")
        self.assertEqual(pm.check(self.t + timedelta(days=6)), [])
        self.assertEqual(len(pm.check(self.t + timedelta(days=7, minutes=1))), 1)

    def test_run_hours_and_filters_come_from_the_machine_readings(self):
        start = self.t - timedelta(hours=5)
        hours = PMTask.objects.create(machine="Barbara", title="Blade", every=2, unit="hours", created=start)
        units = PMTask.objects.create(machine="Barbara", title="Glue filter", every=500, unit="units", created=start)
        readings("Barbara", start, 90, units=3)                                # 1.5 h running, 270 filters
        readings("Barbara", start + timedelta(minutes=90), 60, run=False)      # stopped: no hours, no filters
        readings("Salvatore", start, 200, units=5)                             # another machine doesn't count
        self.assertAlmostEqual(pm.used(hours, self.t), 1.5)
        self.assertEqual(pm.used(units, self.t), 270)
        self.assertEqual(pm.status(hours, self.t)["state"], "ok")
        readings("Barbara", start + timedelta(minutes=150), 60, units=5)       # +1 h, +300 filters
        self.assertEqual({x.pm_id for x in pm.check(self.t)}, {hours.id, units.id})
        st = pm.status(hours, self.t, pm.open_ticket(hours))
        self.assertEqual((st["state"], st["pct"]), ("open", 125))

    def test_save_rules(self):
        lead = ctx("4444")
        r = engine.execute("pmSave", {"machine": "Compressor", "title": "Drain", "every": 10, "unit": "hours"}, lead)
        self.assertFalse(r["ok"])
        self.assertIn("only go by days", r["error"])
        self.assertFalse(engine.execute("pmSave", {"machine": "Nope", "title": "x", "every": 1}, lead)["ok"])
        self.assertFalse(engine.execute("pmSave", {"machine": "Barbara", "title": "x", "every": 0}, lead)["ok"])
        r = engine.execute("pmSave", {"machine": "Compressor", "title": "Drain tank", "every": 7, "unit": "days", "lastDone": "2020-01-01"}, lead)
        self.assertTrue(r["ok"], r)
        task = PMTask.objects.get(pk=r["data"]["id"])
        self.assertEqual(task.last_done.year, 2020)
        self.assertEqual(pm.status(task, self.t)["state"], "due")
        op = engine.execute("pmSave", {"machine": "Barbara", "title": "x", "every": 1}, ctx("1111"))
        self.assertFalse(op["ok"])                                             # operators can't change the schedule

    def test_log_done_early_and_remove(self):
        task = PMTask.objects.create(machine="Barbara", title="Belts", every=30, unit="days", created=self.t - timedelta(days=20))
        r = engine.execute("pmDone", {"id": task.id, "note": "tightened"}, ctx("3333"))
        self.assertTrue(r["ok"], r)
        task.refresh_from_db()
        self.assertIsNotNone(task.last_done)
        self.assertEqual([x.repairs.last().text for x in pm.history(task)], ["tightened"])
        engine.execute("pmOpenNow", {"id": task.id}, ctx("4444"))
        self.assertEqual(Ticket.objects.filter(pm=task, status="Open").count(), 1)
        self.assertTrue(engine.execute("pmDelete", {"id": task.id}, ctx("4444"))["ok"])
        task.refresh_from_db()   # kept (inactive) for its history; its open request went
        self.assertFalse(task.active)
        self.assertEqual(Ticket.objects.filter(pm=task).exclude(status="Closed").count(), 0)

    def test_maintenance_section(self):
        c = Client()
        self.assertContains(c.get("/maintenance/"), "Open Maintenance")   # its PIN login
        login(c, "3333")
        task = PMTask.objects.create(machine="Barbara", title="Grease chain", every=7, unit="days", created=self.t - timedelta(days=8))
        r = c.get("/maintenance/?tab=pm")
        self.assertContains(r, "Grease chain")
        self.assertContains(r, "DUE")
        Ticket.objects.create(t=self.t, by="PM schedule", machine="Barbara", type="PM · Grease chain", pm=task)
        self.assertContains(c.get("/maintenance/"), "Opened by the PM schedule")   # tagged under Pending requests
        r = c.post("/maintenance/", {"action": "pmSave", "machine": "Salvatore", "title": "Check guards", "every": "14", "unit": "days",
                                     "priority": "High", "active": "1"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(PMTask.objects.filter(machine="Salvatore", title="Check guards", priority="High").exists())
        r = c.post("/maintenance/", {"action": "pmSave", "machine": "Salvatore", "title": "", "every": "14"})
        self.assertContains(r, "Say what the job is")
        for tab in ("requests", "done", "pm", "history", "inventory", "machines"):
            self.assertEqual(c.get(f"/maintenance/?tab={tab}").status_code, 200)


# ------------------------------------------------------------------ alerts
class AlertTests(Base):
    def start(self):
        return on_shift(Client())

    def run_check(self, now=None):
        sent = []
        with mock.patch("hmi.alerts.send_email", side_effect=lambda to, subj, body: sent.append(subj)), \
                mock.patch("hmi.alerts.threading.Thread", side_effect=sync_thread), mock.patch("hmi.alerts.close_old_connections"), \
                mock.patch("hmi.engine.planned_windows", return_value=[]):
            alerts.check(now)
        return sent

    def test_qc_overdue_once_per_check(self):
        AlertContact.objects.create(name="Quinn", email="q@plant.local", down_after=0, qc_late=15)
        sh = self.start()
        ShiftRun.objects.filter(pk=sh.pk).update(qc_last=self.t - timedelta(minutes=40), qc_after_co=False)   # due 30 min after: 10 min over
        self.assertEqual(self.run_check(), [])
        ShiftRun.objects.filter(pk=sh.pk).update(qc_last=self.t - timedelta(minutes=50))                      # 20 min over
        self.assertEqual(self.run_check(), ["QC check overdue on Barbara"])
        self.assertEqual(self.run_check(), [])
        ShiftRun.objects.filter(pk=sh.pk).update(stop_start=self.t - timedelta(minutes=60), state="stopped")
        self.assertEqual(self.run_check(), [])            # down_after 0: not told about stops

    def test_failed_check_and_pm_due(self):
        AlertContact.objects.create(name="Sup", email="s@plant.local", down_after=0, qc_fail=True, pm_due=True, lines=["Barbara"])
        QCCheck.objects.create(t=self.t, line="Barbara", mo="309215", item="MG4062", by="J. Ortiz", passed=False, review="pending", fails=["len"])
        QCCheck.objects.create(t=self.t, line="Salvatore", mo="1", item="X", by="M. Lee", passed=False, review="pending")   # not their line
        PMTask.objects.create(machine="Barbara", title="Grease chain", every=1, unit="days", created=self.t - timedelta(days=2))
        pm.check(self.t)
        self.assertEqual(sorted(self.run_check()), ["Planned maintenance due on Barbara", "QC check failed on Barbara"])
        self.assertEqual(self.run_check(), [])
        self.assertEqual(set(AlertLog.objects.values_list("kind", flat=True)), {"qcfail", "pmdue"})

    def test_contact_needs_something_to_be_told(self):
        c = Client()
        login(c, "9999")
        r = cmd(c, "alertContactSave", {"contact": {"name": "A", "email": "a@plant.local", "downAfter": 0}})
        self.assertFalse(r["ok"])
        r = cmd(c, "alertContactSave", {"contact": {"name": "A", "email": "a@plant.local", "downAfter": 0, "qcLate": 20, "qcFail": True}})
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["data"]["downAfter"], r["data"]["qcLate"], r["data"]["qcFail"], r["data"]["pmDue"]), (0, 20, True, False))


# ------------------------------------------------------------------ traceability
class TraceTests(Base):
    def test_rolls_split_across_mos_and_mos_list_their_rolls(self):
        L = "Salvatore"
        RollTag.objects.filter(line=L).delete()
        MoRun.objects.filter(line=L).delete()
        ShiftRun.objects.filter(line=L, active=True).update(active=False)
        t0 = self.t - timedelta(hours=4)
        readings(L, t0, 180, units=2)                                               # 2 filters a minute for 3 h
        MoRun.objects.create(line=L, mo="A1", item="P1", start_time=t0, end_time=t0 + timedelta(minutes=100))
        MoRun.objects.create(line=L, mo="B2", item="P2", start_time=t0 + timedelta(minutes=100), end_time=t0 + timedelta(minutes=180))
        r1 = RollTag.objects.create(line=L, t=t0, roll="R1", lot="LOT-9", material="SYN", by="x")
        r2 = RollTag.objects.create(line=L, t=t0 + timedelta(minutes=60), roll="R2", lot="LOT-9", material="SYN", by="x")
        RollTag.objects.filter(pk=r2.pk).update(off_t=t0 + timedelta(minutes=150))
        u1 = trace.roll_usage(r1, self.t)
        self.assertEqual([(m["mo"], m["units"]) for m in u1["mos"]], [("A1", 120)])
        u2 = trace.roll_usage(RollTag.objects.get(pk=r2.pk), self.t)
        self.assertEqual([(m["mo"], m["units"]) for m in u2["mos"]], [("A1", 80), ("B2", 100)])
        self.assertFalse(u2["on"])
        b2 = trace.mo_rolls("B2", self.t)
        self.assertEqual([(r["tag"].roll, r["units"]) for r in b2["rolls"]], [("R2", 100)])
        self.assertEqual((b2["units"], b2["noRoll"]), (160, 60))                 # after roll off: made with no roll scanned
        R = trace.search("LOT-9", self.t)
        self.assertEqual({r["tag"].roll for r in R["rolls"]}, {"R1", "R2"})
        self.assertEqual(trace.search("A1", self.t)["mos"][0]["mo"], "A1")
        self.assertIn(["R2", "LOT-9", "SYN", "", L], [row[:5] for row in trace.csv_rows(R)])

    def test_quality_traceability_tab_and_roll_off(self):
        op = Client()
        on_shift(op)
        self.assertTrue(cmd(op, "rollAdd", {"roll": "R-77", "lot": "L-5", "material": "SYN"})["ok"])
        self.assertEqual(trace.current_roll("Barbara").roll, "R-77")
        self.assertTrue(cmd(op, "rollOff", {})["ok"])
        self.assertIsNone(trace.current_roll("Barbara"))
        self.assertFalse(cmd(op, "rollOff", {})["ok"])
        c = Client()
        post(c, "/api/hmi/login", {"pin": "4444", "perm": "section.quality"})
        r = c.get("/quality/?tab=trace&q=L-5")
        self.assertContains(r, "R-77")
        self.assertContains(r, "Reading roll tags from a picture")
        self.assertContains(r, "<th>Went into</th>", html=False)
        self.assertNotContains(r, "No quality problems for these filters")
        self.assertIn("R-77", c.get("/quality/?tab=trace&q=L-5&csv=1").content.decode())


# ------------------------------------------------------------------ reading roll tags
class RollTagReadTests(Base):
    def read(self, lines, codes=(), media=()):
        return rolltag.read(lines, list(codes), rolltag.settings(), media=list(media))["fields"]

    def test_labels_same_line_and_below(self):
        f = self.read(["ACME FILTRATION", "Product: SYN-200 GREY", "Lot No: L240917", "Roll No.: 0098812", "Length: 2,500 LF"])
        self.assertEqual(f, {"roll": "0098812", "lot": "L240917", "material": "SYN-200 GREY", "feet": 2500})
        f = self.read([{"text": "LOT #", "box": [10, 10, 60, 30]}, {"text": "ROLL ID", "box": [200, 10, 280, 30]},
                       {"text": "4471A", "box": [12, 40, 70, 60]}, {"text": "R-55102", "box": [198, 40, 290, 60]}])
        self.assertEqual((f["lot"], f["roll"]), ("4471A", "R-55102"))
        self.assertEqual(self.read(["Lot 4471 Roll 0098", "Meters: 900"]), {"lot": "4471", "roll": "0098", "feet": 2953})

    def test_barcodes_patterns_and_media(self):
        self.assertEqual(self.read([], ["(10)LOT77(21)R999"]), {"lot": "LOT77", "roll": "R999"})
        self.assertEqual(self.read(["HP-80 media 12in"], ["R123456"], media=["HP-80"]), {"roll": "R123456", "material": "HP-80"})
        st = PlantSettings.load()
        st.config = {**st.config, "rollTag": {"labels": {"roll": []}, "patterns": {"roll": r"\b(RX\d{5})\b"}, "barcode": ""}}
        st.save()
        self.assertEqual(self.read(["Order RX12345 shipped"], ["IGNORED"])["roll"], "RX12345")
        self.assertTrue(rolltag.check_pattern("(a+)+"))
        self.assertTrue(rolltag.check_pattern("("))

    def test_settings_saved_from_quality(self):
        lead = ctx("4444")
        r = engine.execute("rollTagSet", {"roll_labels": "Spool, Roll", "lot_labels": "Charge", "material_labels": "", "feet_labels": "Länge",
                                          "roll_pattern": "", "barcode": "lot"}, lead)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.read(["Spool: 55", "Charge 7"]), {"roll": "55", "lot": "7"})
        self.assertFalse(engine.execute("rollTagSet", {"roll_labels": "x", "roll_pattern": "(a+)+"}, lead)["ok"])
        engine.execute("rollTagReset", {}, lead)
        self.assertIn("lot", rolltag.settings()["labels"]["lot"])

    def test_kiosk_picture_fills_the_dialog_and_is_kept_with_the_roll(self):
        op = Client()
        on_shift(op)
        op.get("/operator/")
        ocr = {"lines": [{"text": "Roll No: R-4401", "box": [0, 0, 100, 20]}, {"text": "Lot: L-88", "box": [0, 30, 100, 50]},
                         {"text": "Length 1,000 ft", "box": [0, 60, 100, 80]}], "barcodes": []}
        r = op.post("/kiosk/do/rollRead", {"ocr:j": json.dumps(ocr), "tagphoto": SimpleUploadedFile("tag.jpg", picture(800, 600), "image/jpeg")},
                    HTTP_HX_REQUEST="true", HTTP_X_IN_MODAL="1")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn('value="R-4401"', html)
        self.assertIn('value="L-88"', html)
        self.assertIn('value="1000"', html)
        self.assertIn("Read Roll ID, Lot, Footage", html)
        ph = RollTagPhoto.objects.get()
        self.assertIsNone(ph.roll)
        self.assertEqual(ph.found["roll"], "R-4401")
        r = op.post("/kiosk/do/rollAdd", {"roll": "R-4401", "lot": "L-88", "feet": "1000", "material": "SYN", "photo": str(ph.pk)},
                    HTTP_HX_REQUEST="true", HTTP_X_IN_MODAL="1")
        self.assertEqual(r.status_code, 200)
        ph.refresh_from_db()
        self.assertEqual(ph.roll.roll, "R-4401")
        self.assertEqual(op.get(f"/api/hmi/roll-photo/{ph.pk}?size=thumb")["Content-Type"], "image/jpeg")
        self.assertContains(op.get("/kiosk/m/rolls"), "On the line now")

    def test_kiosk_when_nothing_could_be_read(self):
        op = Client()
        on_shift(op)
        op.get("/operator/")
        r = op.post("/kiosk/do/rollRead", {"ocr:j": json.dumps({"error": "no WebAssembly"})}, HTTP_HX_REQUEST="true", HTTP_X_IN_MODAL="1")
        self.assertContains(r, "couldn&#x27;t read the picture")
