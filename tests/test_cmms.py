"""Maintenance as a work-order system: assets, work orders, labor and cost, failures and corrective actions,
meters, calibration, reservations and maintenance windows."""
from datetime import timedelta

from django.test import Client, TestCase
from django.utils import timezone

from hmi import engine, pm
from hmi.models import Downtime, PMTask, ShiftRun, Ticket
from maintenance import cmms
from maintenance.models import Asset, Calibration, InventoryItem, MaintWindow, Meter, PMPart, Reservation
from plant.demo import seed_demo
from plant.models import Machine, Person


class CMMS(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.ctx = engine.Ctx(user=Person.objects.get(pin="9999"))
        self.now = timezone.now()

    def run_(self, cmd_, **p):
        r = engine.execute(cmd_, p, self.ctx)
        self.assertTrue(r["ok"], r)
        return r

    def test_assets_work_order_labor_cost_and_history(self):
        m = self.run_("assetSave", name="Barbara", kind="machine", machine="Barbara")["data"]["id"]
        knife = self.run_("assetSave", name="Knife assembly", kind="assembly", parent=m)["data"]["id"]
        self.assertEqual(Asset.objects.get(pk=knife).machine, "Barbara")
        self.assertFalse(engine.execute("assetSave", {"id": m, "name": "Barbara", "machine": "Barbara", "parent": knife}, self.ctx)["ok"])   # not under itself
        it = InventoryItem.objects.create(name="Blade", qty=5, unit_cost=40.0)
        t = Ticket.objects.create(t=self.now - timedelta(hours=3), machine="Barbara", line="Barbara", by="op", type="Blade / cutter")
        self.run_("woSet", id=t.pk, assigned="K. Patel", due=str(timezone.localdate()), est_hours="2", asset=knife, failure="Broken / worn part",
                  cause="Wear", remedy="Replaced part", outside_cost="25")
        self.run_("reserve", ticket=t.pk, item=it.pk, qty="2")
        self.assertEqual(InventoryItem.objects.get(pk=it.pk).free, 3)
        self.assertFalse(engine.execute("reserve", {"ticket": t.pk, "item": it.pk, "qty": "4"}, self.ctx)["ok"])   # only 3 free
        self.run_("itemUse", ticket=t.pk, item=it.pk, qty="1")
        self.assertEqual([(r.status, r.qty) for r in Reservation.objects.filter(ticket=t).order_by("status")], [("reserved", 1.0), ("used", 1.0)])
        self.run_("laborAdd", id=t.pk, who="K. Patel", hours="1.5")
        s = engine.settings_obj()
        s.config["rules"]["maintRate"] = 50
        s.save()
        t.refresh_from_db()
        c = cmms.ticket_cost(t)
        self.assertEqual((c["parts"], c["labor"], c["outside"], c["total"]), (40.0, 75.0, 25.0, 140.0))
        sh = ShiftRun.objects.filter(machine__name="Barbara").first()
        base = cmms.ticket_down(t, self.now)
        Downtime.objects.create(shift=sh, line="Barbara", start_time=self.now - timedelta(hours=2), end_time=self.now - timedelta(hours=1, minutes=30), minutes=30)
        self.assertAlmostEqual(cmms.ticket_down(t, self.now) - base, 30, delta=0.5)
        self.run_("ticketSet", id=t.pk, status="Closed")
        self.assertEqual(Reservation.objects.get(ticket=t, status="released").qty, 1.0)   # what was left goes back
        hist = cmms.history(Asset.objects.get(pk=m))
        self.assertTrue(any("Blade" in h["text"] for h in hist))
        self.assertTrue(any(h["kind"] == "request" for h in hist))
        rows = cmms.by_asset(self.now, 30, "Barbara")
        self.assertEqual(rows[0]["top"], [("Broken / worn part", 1)])
        self.assertEqual(cmms.workload(self.now)[0]["who"], cmms.workload(self.now)[0]["who"])

    def test_checklist_must_be_done(self):
        task = PMTask.objects.create(machine="Barbara", title="Grease chain", steps="Clean chain\nGrease chain", every=7, created=self.now - timedelta(days=10))
        it = InventoryItem.objects.create(name="Grease", qty=3)
        PMPart.objects.create(task=task, item=it, qty=1)
        t = pm.open_request(task, self.now)
        self.assertEqual(len(t.checklist), 2)
        self.assertEqual(Reservation.objects.get(ticket=t).qty, 1)                     # the PM's parts reserved
        r = engine.execute("ticketSet", {"id": t.pk, "status": "Closed"}, self.ctx)
        self.assertIn("checklist", r["error"])
        self.run_("woCheck", id=t.pk, i=0, done="1")
        self.run_("woCheck", id=t.pk, i=1, done="1")
        self.run_("ticketSet", id=t.pk, status="Closed")

    def test_repeat_failures_lead_to_a_corrective_action(self):
        for i in range(3):
            Ticket.objects.create(t=self.now - timedelta(days=10 * i), machine="Salvatore", line="Salvatore", by="op", type="Pleater jam", status="Closed",
                                  closed_at=self.now - timedelta(days=10 * i))
        s = cmms.suggestions(self.now)
        self.assertEqual((s[0]["machine"], s[0]["key"], s[0]["n"]), ("Salvatore", "Pleater jam", 3))
        ca = self.run_("caOpen", machine="Salvatore", key="Pleater jam")["data"]["id"]
        self.assertEqual(Ticket.objects.filter(ca_id=ca).count(), 3)
        self.assertEqual(cmms.suggestions(self.now), [])
        r = engine.execute("ticketAdd", {"machine": "Salvatore", "type": "Pleater jam"}, engine.Ctx(user=Person.objects.get(pin="9999"), line="Salvatore"))
        self.assertIn(f"corrective action #{ca}", r["msg"])
        self.assertEqual(Ticket.objects.get(pk=r["data"]["id"]).ca_id, ca)

    def test_meter_pm(self):
        mt = Meter.objects.create(machine="Barbara", name="Knife cycles", unit="cycles", value=1000)
        r = self.run_("pmSave", machine="Barbara", title="Sharpen knife", every="5000", unit="meter", meter=mt.pk)
        task = PMTask.objects.get(pk=r["data"]["id"])
        self.assertEqual(task.meter_at, 1000)
        self.run_("meterRead", id=mt.pk, value="4000")
        self.assertNotIn(task.pk, [t.pm_id for t in pm.check(self.now)])
        self.assertFalse(engine.execute("meterRead", {"id": mt.pk, "value": "3000"}, self.ctx)["ok"])   # can't go back without a reset
        self.run_("meterRead", id=mt.pk, value="6500")
        opened = [t for t in pm.check(self.now) if t.pm_id == task.pk]
        self.assertEqual(len(opened), 1)
        self.run_("woCheck", id=opened[0].pk, i=0, done="1") if opened[0].checklist else None
        self.run_("ticketSet", id=opened[0].pk, status="Closed")
        self.assertEqual(PMTask.objects.get(pk=task.pk).meter_at, 6500)
        # from the machine's data
        Meter.objects.create(machine="Barbara", name="Glue gun hours", source="field", field="glueHrs", value=0)
        cmms.meters_from_sample(Machine.objects.get(name="Barbara"), {"glueHrs": 12.5}, self.now)
        self.assertEqual(Meter.objects.get(name="Glue gun hours").value, 12.5)

    def test_calibration(self):
        g = Asset.objects.create(name="Caliper #3", machine="Barbara", kind="instrument", cal_every=180)
        self.assertEqual(cmms.calibration_rows(timezone.localdate())[0]["state"], "never")
        self.run_("calRecord", asset=g.pk, result="fail", as_found="+0.12 mm")
        g.refresh_from_db()
        self.assertFalse(g.active)
        self.run_("calRecord", asset=g.pk, result="adjusted", as_found="+0.12", as_left="0.00", cert="C-55")
        g.refresh_from_db()
        self.assertTrue(g.active)
        self.assertEqual(g.cal_due, timezone.localdate() + timedelta(days=180))
        self.assertEqual(Calibration.objects.filter(asset=g).count(), 2)

    def test_window_is_planned_time_for_the_scheduler_and_kiosk(self):
        from plant.util import extra_breaks
        a = (self.now + timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
        m = Machine.objects.get(name="Barbara")
        self.run_("windowSave", machine="Barbara", start=timezone.localtime(a).strftime("%Y-%m-%dT%H:%M"),
                  end=timezone.localtime(a + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M"), note="Rebuild glue pot")
        w = [x for x in extra_breaks(a - timedelta(hours=1), a + timedelta(hours=5)) if str(x["id"]).startswith("mw")]
        self.assertEqual(len(w), 1)
        self.assertEqual(w[0]["machines"], {m.pk})
        self.run_("windowCancel", id=MaintWindow.objects.get().pk)
        self.assertFalse([x for x in extra_breaks(a - timedelta(hours=1), a + timedelta(hours=5)) if str(x["id"]).startswith("mw")])

    def test_pages_render(self):
        c = Client()
        c.get("/maintenance/")
        import json
        c.post("/api/hmi/login", json.dumps({"pin": "9999"}), content_type="application/json")
        Asset.objects.create(name="Caliper", machine="Barbara", kind="instrument", cal_every=30)
        for tab in ("requests", "workload", "windows", "assets", "calibration", "reliability", "pm", "inventory"):
            r = c.get(f"/maintenance/{tab}/")
            self.assertEqual(r.status_code, 200, tab)
        self.assertContains(c.get("/maintenance/calibration/"), "Caliper")
