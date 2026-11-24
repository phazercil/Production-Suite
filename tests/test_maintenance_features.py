"""Maintenance: PM parts lists, low-stock alerts and the reorder list, lockout / return to service, reliability."""
from datetime import timedelta

from django.test import Client, TestCase
from django.utils import timezone

from hmi import engine
from hmi.models import Notice, PMTask, Person, ShiftRun, Ticket
from maintenance.models import InventoryItem, InventoryMove, PMPart
from plant.demo import seed_demo
from plant.models import Machine

from .test_quality import sup_login


class MaintFeatures(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.mt = engine.Ctx(user=Person.objects.get(pin="3333"))
        self.lead = engine.Ctx(user=Person.objects.get(pin="9999"))
        self.belt = InventoryItem.objects.create(name="Drive belt", part_no="BT-1", qty=3, min_qty=1)
        self.task = PMTask.objects.create(machine="Barbara", title="Belts", every=30, unit="days", active=True, created=timezone.now())

    def test_pm_parts_list_and_use(self):
        r = engine.execute("pmPartSet", {"task": self.task.pk, "item": self.belt.pk, "qty": "2"}, self.lead)
        self.assertTrue(r["ok"], r)
        t = Ticket.objects.create(t=timezone.now(), machine="Barbara", by="PM schedule", type="PM · Belts", pm=self.task)
        Notice.objects.all().delete()
        r = engine.execute("itemUseBom", {"ticket": t.pk}, self.mt)
        self.assertTrue(r["ok"], r)
        self.belt.refresh_from_db()
        self.assertEqual(self.belt.qty, 1)
        self.assertTrue(Notice.objects.filter(text__startswith="Reorder Drive belt").exists())     # reached its reorder point
        self.assertFalse(engine.execute("itemUseBom", {"ticket": t.pk}, self.mt)["ok"])           # not twice
        c = Client()
        self.assertTrue(sup_login(c, "3333", "section.maintenance", "/maintenance/")["ok"])
        self.assertIn("Drive belt", c.get("/maintenance/reorder.csv").content.decode())
        self.assertContains(c.get("/maintenance/?tab=pm"), "Parts: 2 × Drive belt")

    def test_lockout_and_return(self):
        bar = Machine.objects.get(name="Barbara")
        t = Ticket.objects.create(t=timezone.now(), machine="Barbara", by="J", type="Mechanical", priority="High")
        self.assertTrue(engine.execute("lockout", {"id": t.pk}, self.mt)["ok"])
        bar.refresh_from_db()
        self.assertFalse(bar.active)
        ShiftRun.objects.filter(machine=bar).update(active=False)
        op = Person.objects.get(pin="1111")
        op.trained.add(bar)
        r = engine.execute("startShift", {"line": "Barbara", "safety": [True] * len(engine.config()["safety"])}, engine.Ctx(user=op, line="Barbara"))
        self.assertFalse(r["ok"])
        self.assertIn("locked out", r["error"])
        from datakiosk.screen import line_card
        self.assertEqual(line_card("Barbara")["pill"], ("bad", "Locked out"))
        self.assertFalse(engine.execute("returnService", {"id": t.pk, "note": ""}, self.mt)["ok"])   # a note is required
        self.assertTrue(engine.execute("returnService", {"id": t.pk, "note": "Guards on, test run OK"}, self.mt)["ok"])
        bar.refresh_from_db()
        self.assertTrue(bar.active)

    def test_reliability(self):
        from maintenance import reliability
        now = timezone.now()
        Ticket.objects.all().delete()
        for i in range(3):
            Ticket.objects.create(t=now - timedelta(days=i + 1), machine="Barbara", by="J", type="Pleater jam", status="Closed",
                                  updated=now - timedelta(days=i + 1) + timedelta(hours=2))
        R = reliability.build(now, 30)
        row = next(r for r in R["rows"] if r["machine"] == "Barbara")
        self.assertEqual((row["failures"], row["mttr"]), (3, 2.0))
        self.assertEqual(R["repeats"][0]["n"], 3)
        c = Client()
        self.assertTrue(sup_login(c, "3333", "section.maintenance", "/maintenance/")["ok"])
        self.assertContains(c.get("/maintenance/?tab=reliability&days=30"), "MTBF")
        InventoryMove.objects.all()   # (parts history is covered by the page rendering)
        self.assertEqual(PMPart.objects.count(), 0)
