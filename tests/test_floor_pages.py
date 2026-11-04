"""Maintenance requests with repair notes, and the Downtime page header."""
import json

from django.core.cache import cache
from django.test import Client, TestCase

from hmi.models import Ticket, TicketNote
from plant.demo import seed_demo

from .test_suite import no_planned_time


class FloorPageTests(TestCase):
    def setUp(self):
        no_planned_time(self)
        cache.clear()
        seed_demo(reset=True)
        self.c = Client(HTTP_HX_REQUEST="true")

    def do(self, action, **data):
        return self.c.post(f"/kiosk/do/{action}", data)

    def login(self, pin):
        self.assertIn("Logged in", self.do("login", pin=pin).headers.get("HX-Trigger", ""))

    def test_repair_notes_and_completing_a_request(self):
        t = Ticket.objects.get()
        r = self.c.get("/kiosk/v/maintenance")
        self.assertContains(r, "Pleater jam")
        self.assertContains(r, "No repair notes yet.")
        # an operator can't close a request: it asks for someone who can
        self.login("1111")
        r = self.do("ticketNote", id=t.id, text="looked at it")
        self.assertFalse(TicketNote.objects.exists())
        self.assertContains(r, "K. Patel")              # the login dialog lists who can do it
        self.do("logout")
        self.login("3333")                              # maintenance
        self.do("ticketNote", id=t.id, text="Score wheel guide worn; shimmed it.")
        t.refresh_from_db()
        self.assertEqual(t.status, "In progress")       # a note starts the work
        r = self.c.get(f"/kiosk/m/ticketDone?id={t.id}")
        self.assertContains(r, "What was done?")
        self.do("ticketDone", id=t.id, close="1", note="Replaced the guide.")
        t.refresh_from_db()
        self.assertEqual((t.status, t.updated_by), ("Closed", "K. Patel"))
        self.assertEqual(list(TicketNote.objects.values_list("kind", flat=True)), ["note", "closed"])
        r = self.c.get("/kiosk/v/maintenance")
        self.assertContains(r, "No open maintenance requests.")
        r = self.c.get("/kiosk/v/maintenance?show=done")
        self.assertContains(r, "Replaced the guide.")
        self.assertContains(r, "COMPLETED")
        r = self.do("ticketStatus", id=t.id)
        self.assertIn("already completed", json.dumps(dict(r.headers)) + r.content.decode())

    def test_downtime_header(self):
        r = self.c.get("/kiosk/v/downtime")
        self.assertContains(r, "Shift running time")
        self.assertContains(r, "Overall shift downtime")
        self.assertContains(r, " stops")


class TeamEditTests(TestCase):
    def setUp(self):
        no_planned_time(self)
        cache.clear()
        seed_demo(reset=True)
        self.c = Client(HTTP_HX_REQUEST="true")
        self.assertIn("Logged in", self.c.post("/kiosk/do/login", {"pin": "4444"}).headers.get("HX-Trigger", ""))   # shift lead

    def test_edit_dialog_saves_only_on_save_and_all_or_nothing(self):
        from plant.models import Person
        op = Person.objects.get(name="J. Ortiz")
        r = self.c.get("/kiosk/v/team")
        self.assertContains(r, f"/kiosk/m/editPerson?id={op.id}")
        self.assertNotContains(r, 'hx-post="/kiosk/do/userSet"')        # nothing saves as you type any more
        r = self.c.get(f"/kiosk/m/editPerson?id={op.id}")
        self.assertContains(r, "Nothing changes until you press Save.")
        # a PIN someone else uses: nothing at all is saved, not even the new name
        r = self.c.post("/kiosk/do/personSave", {"id": op.id, "name": "Jo Ortiz", "pin": "2222", "role": "operator", "hasTrained": "1",
                                                 "trained:l": ["Barbara", "Salvatore"], "hasActive": "1", "active": "1"})
        self.assertContains(r, "already uses that PIN")
        op.refresh_from_db()
        self.assertEqual((op.name, op.pin), ("J. Ortiz", "1111"))
        self.assertEqual(list(op.trained.values_list("name", flat=True)), ["Barbara"])
        self.c.post("/kiosk/do/personSave", {"id": op.id, "name": "Jo Ortiz", "pin": "1212", "role": "maintenance", "hasTrained": "1",
                                             "trained:l": ["Salvatore"], "hasActive": "1", "active": "1"})
        op.refresh_from_db()
        self.assertEqual((op.name, op.pin, op.role), ("Jo Ortiz", "1212", "maintenance"))
        self.assertEqual(list(op.trained.values_list("name", flat=True)), ["Salvatore"])
        # unticking every machine
        self.c.post("/kiosk/do/personSave", {"id": op.id, "name": "Jo Ortiz", "pin": "1212", "role": "maintenance", "hasTrained": "1"})
        self.assertFalse(op.trained.exists())
        # a shift lead can't open an Admin
        admin = Person.objects.get(role="admin")
        r = self.c.get(f"/kiosk/m/editPerson?id={admin.id}")
        self.assertNotContains(r, "Nothing changes until you press Save.")


class PaycomPositionTests(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.c = Client(HTTP_HX_REQUEST="true")
        self.c.post("/kiosk/do/login", {"pin": "9999"})

    def test_only_the_picked_position_titles_are_imported(self):
        from unittest import mock

        from hmi import engine, integrations
        from plant.models import Person
        emps = [{"code": "100", "name": "A Op", "active": True, "department": "Production", "position": "Machine Operator", "badge": ""},
                {"code": "101", "name": "B Lead", "active": True, "department": "Production", "position": "Machine Operator Lead", "badge": ""},
                {"code": "102", "name": "C Office", "active": True, "department": "Office", "position": "Clerk", "badge": ""},
                {"code": "103", "name": "D Op", "active": True, "department": "Production", "position": "Machine  Operator", "badge": ""}]
        with mock.patch.object(integrations, "paycom_ready", return_value=True), mock.patch.object(integrations, "paycom_employees", return_value=emps):
            r = self.c.get("/kiosk/m/paycomPositions")
            self.assertContains(r, "Machine Operator Lead")
            self.assertContains(r, "2 people")                          # spacing differences count as the same title
            self.c.post("/kiosk/do/paycomPositionsSet", {"positions:l": ["Machine Operator"]})
            r = self.c.post("/kiosk/do/paycomImport")
        names = set(Person.objects.filter(source="paycom", active=True).values_list("name", flat=True))
        self.assertEqual(names, {"A Op", "D Op"})                      # not "Machine Operator Lead", not the clerk
        self.assertTrue(engine.settings_obj().settings["paycom"]["positionExact"])
