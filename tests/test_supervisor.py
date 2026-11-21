"""Supervisor › History: shift logs (each opens its End of Shift report), QC checks, downtime and changeovers, with
filters and a CSV of each list. Shift leads only."""
import json
from datetime import timedelta

from django.test import Client, TestCase
from django.utils import timezone

from hmi.models import ChangeoverPiece, Downtime, QCCheck, ShiftRun
from plant.demo import seed_demo


def login(c, pin):
    c.get("/supervisor/")
    r = c.post("/api/hmi/login", json.dumps({"pin": pin, "perm": "section.supervisor"}), content_type="application/json",
               HTTP_X_CSRFTOKEN=c.cookies["csrftoken"].value)
    return r.json()


class SupervisorHistoryTests(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.c = Client(enforce_csrf_checks=True)

    def test_operators_cannot_open_it(self):
        self.assertContains(self.c.get("/supervisor/"), "Open Supervisor")
        self.assertFalse(login(self.c, "1111")["ok"])           # an operator's PIN
        self.assertContains(self.c.get("/supervisor/"), "Open Supervisor")
        self.assertEqual(self.c.get("/supervisor/?tab=qc&csv=1")["Content-Type"].split(";")[0], "text/html")

    def test_lists_reports_and_csv(self):
        self.assertTrue(login(self.c, "4444")["ok"])             # S. Lead
        sh = ShiftRun.objects.filter(line="Barbara", active=True).first()
        r = self.c.get("/supervisor/")
        self.assertContains(r, "Shift logs")
        self.assertContains(r, f"/supervisor/log/{sh.pk}/")
        # a past log opens as its full End of Shift report
        old = ShiftRun.objects.filter(active=False).exclude(user_name="").order_by("-start").first()
        r = self.c.get(f"/supervisor/log/{old.pk}/?back=tab%3Dlogs")
        self.assertContains(r, "End of Shift report")
        self.assertContains(r, old.user_name)
        self.assertContains(r, 'href="/supervisor/?tab=logs"')
        # QC checks, filtered to failures, and their CSV
        fails = QCCheck.objects.filter(passed=False).count()
        self.assertGreater(fails, 0)
        r = self.c.get("/supervisor/?tab=qc&result=fail")
        self.assertEqual(r.context["n"], fails)
        csv = self.c.get("/supervisor/?tab=qc&result=fail&csv=1")
        self.assertEqual(csv["Content-Type"].split(";")[0], "text/csv")
        self.assertEqual(len(csv.content.decode().strip().splitlines()), fails + 1)
        # downtime with no reason
        none = Downtime.objects.filter(start_time__gte=timezone.now() - timedelta(days=7), reason__isnull=True).count()
        self.assertEqual(self.c.get("/supervisor/?tab=down&result=none").context["n"], none)
        # changeovers, each person's piece
        ChangeoverPiece.objects.create(line="Barbara", co_id="abc123", shift=sh, by="J. Ortiz", shift_name="1st shift",
                                       start=timezone.now() - timedelta(minutes=40), end=timezone.now() - timedelta(minutes=10), minutes=30,
                                       old_mo="309215", new_mo="306785", steps=["Stop line and lock out"], steps_total=8, ended="passed over",
                                       note="Wheels on the cart")
        r = self.c.get("/supervisor/?tab=co&who=ortiz")
        self.assertContains(r, "Wheels on the cart")
        self.assertContains(r, "1 of 8")
        # a line filter
        self.assertTrue(all(x.line == "Salvatore" for x in self.c.get("/supervisor/?tab=logs&line=Salvatore").context["rows"]))


class LineStatusMachineDataTests(TestCase):
    """A shift left 'running' with no readings from the machine isn't shown as running."""
    def setUp(self):
        seed_demo(reset=True)

    def test_no_readings_says_no_machine_data(self):
        from hmi.models import PpmReading
        from plant.models import Machine
        from supervisor.views import line_status
        bar = Machine.objects.get(name="Barbara")
        ShiftRun.objects.filter(machine=bar, active=True).update(state="running")
        PpmReading.objects.filter(machine=bar).delete()
        cards, tot = line_status()
        c = next(c for c in cards if c["m"] == bar)
        self.assertEqual(c["pill"], ("warn", "No machine data"))
        self.assertGreaterEqual(tot["No machine data"], 1)
        PpmReading.objects.create(machine=bar, t=timezone.now(), ppm=150)
        cards, tot = line_status()
        self.assertEqual(next(c for c in cards if c["m"] == bar)["pill"], ("ok", "Running"))
