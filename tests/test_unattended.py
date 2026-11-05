"""Machines are recorded during their shifts with nobody logged in; the operator who starts their shift takes it over."""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from hmi import engine
from hmi.models import Downtime, PpmReading, ShiftRun
from plant.demo import seed_demo
from plant.models import MO, Machine, Person, Shift
from plant.util import local


class UnattendedTests(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.m = Machine.objects.get(name="Salvatore")
        ShiftRun.objects.filter(machine=self.m).delete()
        # a shift that runs all day, every day, so the test doesn't depend on the time it runs
        sh = Shift.objects.create(name="All day", start="00:00", end="23:59", days=[0, 1, 2, 3, 4, 5, 6])
        self.m.shifts.set([sh])
        self.t = timezone.now().replace(second=0, microsecond=0)
        if local(self.t).hour == 23 and local(self.t).minute >= 50:
            self.t -= timedelta(hours=1)
        self.op = Person.objects.get(name="M. Lee")

    def tick(self, t, ppm=180, count=None):
        s = {"ppm": ppm}
        if count is not None:
            s["count"] = count
        engine.tick(self.m, s, t)

    def test_records_then_the_operator_takes_over(self):
        t = self.t
        self.tick(t, count=1000)
        sh = ShiftRun.objects.get(machine=self.m, active=True)
        self.assertTrue(engine.unattended(sh))
        self.assertEqual(sh.shift_name, "All day")
        for i, c in enumerate((1010, 1030, 1030, 1030, 1030, 1050)):
            self.tick(t + timedelta(minutes=i + 1), ppm=0 if c == 1030 else 180, count=c)
        sh.refresh_from_db()
        self.assertEqual(sh.count, 50)
        self.assertEqual(PpmReading.objects.filter(shift=sh).count(), 7)
        self.assertTrue(Downtime.objects.filter(shift=sh).exists())      # the stop was logged with nobody there
        from django.test import Client
        c = Client(HTTP_HX_REQUEST="true")
        c.get("/station/Salvatore/")
        r = c.get("/kiosk/v/controls")
        self.assertContains(r, "No one logged in")
        self.assertContains(r, "Start shift")
        self.assertNotContains(r, "End shift")
        # the operator starts their shift with an MO: the run and its count are theirs
        mo = MO.objects.filter(status="OPEN", line=self.m).first() or MO.objects.filter(status="OPEN", line__isnull=True).first()
        before = mo.produced
        r = engine.execute("startShift", {"line": self.m.name, "mo": mo.number, "safety": [True] * len(engine.config()["safety"])},
                           engine.Ctx(user=self.op, t=t + timedelta(minutes=8)))
        self.assertTrue(r["ok"], r)
        self.assertIn("is yours", r["msg"])
        self.assertEqual(ShiftRun.objects.filter(machine=self.m).count(), 1)
        sh.refresh_from_db()
        self.assertEqual((sh.person, sh.user_name, sh.mo, sh.mo_count, sh.start), (self.op, self.op.name, mo, 50, t - timedelta(minutes=1)))
        self.assertEqual(MO.objects.get(pk=mo.pk).produced, before + 50)
        self.assertFalse(engine.unattended(sh))
        # a second operator can't start over them
        r = engine.execute("startShift", {"line": self.m.name, "safety": [True] * len(engine.config()["safety"])},
                           engine.Ctx(user=Person.objects.get(name="Plant Admin"), t=t + timedelta(minutes=9)))
        self.assertFalse(r["ok"])

    def test_not_outside_the_machines_shifts_and_closes_at_the_end(self):
        Shift.objects.filter(name="All day").update(start="06:00", end="06:30")
        day = local(self.t).replace(hour=6, minute=0)
        self.tick(day - timedelta(minutes=10))
        self.assertFalse(ShiftRun.objects.filter(machine=self.m).exists())   # before the shift
        self.tick(day + timedelta(minutes=5))
        sh = ShiftRun.objects.get(machine=self.m, active=True)
        self.tick(day + timedelta(minutes=31))                             # after the shift: closed at its end
        sh.refresh_from_db()
        self.assertFalse(sh.active)
        self.assertEqual(local(sh.end).strftime("%H:%M"), "06:30")
        self.assertFalse(ShiftRun.objects.filter(machine=self.m, active=True).exists())
        engine.tick(self.m, None, day + timedelta(minutes=10))             # no data: nothing opened
        self.assertFalse(ShiftRun.objects.filter(machine=self.m, active=True).exists())
