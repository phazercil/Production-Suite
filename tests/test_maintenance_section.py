"""Maintenance section: pending requests and their status, the inventory, and the parts used on each machine."""
from django.test import Client, TestCase
from django.utils import timezone

from hmi import engine
from hmi.models import Person, Ticket, TicketNote
from maintenance.models import InventoryItem, InventoryMove
from plant.demo import seed_demo

from .test_quality import sup_login


class MaintenanceTests(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        Ticket.objects.all().delete()
        now = timezone.now()
        self.t1 = Ticket.objects.create(t=now, line="Barbara", by="J. Ortiz", machine="Barbara", type="Mechanical", priority="High", notes="Belt slipping")
        self.t2 = Ticket.objects.create(t=now, line="Salvatore", by="M. Lee", machine="Salvatore", type="Electrical", status="Closed", updated=now)
        self.belt = InventoryItem.objects.create(name="Drive belt", part_no="BT-1240", qty=4, min_qty=2)
        self.mt = engine.Ctx(user=Person.objects.get(pin="3333"))      # maintenance
        self.op = engine.Ctx(user=Person.objects.get(pin="1111"))      # operator

    def run_(self, cmd_name, ctx=None, **p):
        return engine.execute(cmd_name, p, ctx or self.mt)

    def test_status_form_has_every_status_and_keeps_a_history(self):
        for st in ("Reviewed", "In progress", "Waiting on parts", "Open"):
            r = self.run_("ticketSet", id=self.t1.pk, status=st, note=f"to {st}")
            self.assertTrue(r["ok"], r)
            self.assertEqual(Ticket.objects.get(pk=self.t1.pk).status, st)
        r = self.run_("ticketSet", id=self.t1.pk, status="Closed", note="Belt replaced")
        self.assertTrue(r["ok"], r)
        t = Ticket.objects.get(pk=self.t1.pk)
        self.assertEqual((t.status, t.status_label, t.updated_by), ("Closed", "Completed", "K. Patel"))
        notes = list(TicketNote.objects.filter(ticket=t).values_list("text", flat=True))
        self.assertEqual(notes[0], "Open → Reviewed: to Reviewed")
        self.assertIn("Open → Completed: Belt replaced", notes)
        self.assertFalse(self.run_("ticketSet", id=self.t1.pk, status="Shelved")["ok"])
        self.assertFalse(self.run_("ticketSet", self.op, id=self.t1.pk, status="Open")["ok"])     # needs Work maintenance requests

    def test_completing_a_pm_request_here_starts_its_count_again(self):
        from datetime import timedelta

        from hmi import pm
        from hmi.models import PMTask
        now = timezone.now()
        task = PMTask.objects.create(machine="Barbara", title="Grease chain", every=7, unit="days", created=now - timedelta(days=8))
        tk = pm.check(now)[0]
        self.assertTrue(self.run_("ticketSet", id=tk.pk, status="Closed", note="Greased")["ok"])
        task.refresh_from_db()
        self.assertEqual(task.last_by, "K. Patel")
        self.assertEqual(pm.status(task, now)["state"], "ok")

    def test_parts_used_link_the_item_to_the_machine_and_request(self):
        r = self.run_("itemUse", item=self.belt.pk, qty="1", ticket=self.t1.pk)
        self.assertTrue(r["ok"], r)
        mv = InventoryMove.objects.get(kind="used")
        self.assertEqual((mv.machine, mv.ticket_id, mv.qty), ("Barbara", self.t1.pk, 1))
        self.assertEqual(InventoryItem.objects.get(pk=self.belt.pk).qty, 3)
        self.assertIn("Used 1 × Drive belt (BT-1240)", TicketNote.objects.filter(ticket=self.t1).last().text)
        r = self.run_("itemUse", item=self.belt.pk, qty="2", machine="Salvatore")   # no request
        self.assertTrue(r["ok"], r)
        self.assertIn("time to reorder", r["msg"])
        self.assertFalse(self.run_("itemUse", item=self.belt.pk, qty="1", machine="Nowhere")["ok"])
        self.assertFalse(self.run_("itemUse", item=self.belt.pk, qty="0", machine="Barbara")["ok"])
        r = self.run_("itemUseUndo", id=InventoryMove.objects.get(machine="Salvatore").pk)
        self.assertTrue(r["ok"], r)
        self.assertEqual(InventoryItem.objects.get(pk=self.belt.pk).qty, 3)

    def test_stock_add_and_count(self):
        self.assertTrue(self.run_("itemStock", id=self.belt.pk, kind="added", qty="6", note="PO 4471")["ok"])
        self.assertEqual(InventoryItem.objects.get(pk=self.belt.pk).qty, 10)
        self.assertTrue(self.run_("itemStock", id=self.belt.pk, kind="count", qty="7")["ok"])
        self.assertEqual(InventoryMove.objects.filter(kind="count").first().qty, -3)
        self.assertFalse(self.run_("itemStock", self.op, id=self.belt.pk, kind="added", qty="1")["ok"])   # Maintenance inventory permission
        r = self.run_("itemSave", name="Prox sensor", part_no="BT-1240")
        self.assertFalse(r["ok"])                                                       # part # taken
        r = self.run_("itemSave", name="Prox sensor", part_no="PS-12", qty="5", unit="", min_qty="1")
        self.assertTrue(r["ok"], r)
        it = InventoryItem.objects.get(part_no="PS-12")
        self.assertEqual((it.qty, it.unit, it.min_qty), (5, "each", 1))

    def test_pages(self):
        c = Client()
        self.assertContains(c.get("/maintenance/"), "Open Maintenance")                # the login gate
        self.assertTrue(sup_login(c, "3333", "section.maintenance", "/maintenance/")["ok"])
        r = c.get("/maintenance/").content.decode()
        self.assertIn("Belt slipping", r)
        self.assertNotIn("Electrical", r.split("class=\"mt-reqs\"")[1])   # completed isn't pending
        for v in ("Open", "Reviewed", "In progress", "Waiting on parts", "Completed"):
            self.assertIn(f">{v}</option>", r)
        self.assertIn("Electrical", c.get("/maintenance/?tab=done").content.decode())
        r = c.post("/maintenance/do/itemUse", {"item": self.belt.pk, "qty": "1", "machine": "Barbara", "back": "/maintenance/?tab=inventory"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("msg=Used", r["Location"])
        r = c.post("/maintenance/do/ticketSet", {"id": self.t1.pk, "status": "Waiting on parts", "back": "https://evil.example/"})
        self.assertTrue(r["Location"].startswith("/maintenance/?msg="))
        self.assertIn("Barbara · 1", c.get("/maintenance/?tab=inventory").content.decode())
        r = c.get("/maintenance/?tab=machines&machine=Barbara").content.decode()
        self.assertIn("Drive belt", r)
        self.assertEqual(c.post("/maintenance/do/somethingElse", {}).status_code, 302)
