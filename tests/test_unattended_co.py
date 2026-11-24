"""A changeover done with nobody logged in: Start of shift asks when (a stop in this shift), or leaves it for a supervisor."""
from datetime import timedelta

from django.test import Client, TestCase
from django.utils import timezone

from hmi import engine
from hmi.models import ChangeoverPiece, Downtime, MoRun, Notice, ShiftRun
from plant.demo import seed_demo
from plant.models import MO, Machine, Person, Shift


class UnattendedChangeover(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.m = Machine.objects.get(name="Salvatore")
        ShiftRun.objects.filter(machine=self.m).delete()
        sh = Shift.objects.create(name="All day", start="00:00", end="23:59", days=[0, 1, 2, 3, 4, 5, 6])
        self.m.shifts.set([sh])
        self.op = Person.objects.get(name="M. Lee")
        self.op.trained.add(self.m)
        mos = list(MO.objects.filter(status="OPEN").order_by("id")[:2])
        self.old, self.new = mos
        MO.objects.filter(pk__in=[self.old.pk, self.new.pk]).update(line=self.m, produced=0)
        self.t = timezone.now().replace(second=0, microsecond=0) - timedelta(minutes=40)
        engine.tick(self.m, {"ppm": 180, "count": 1000}, self.t)
        self.sh = ShiftRun.objects.get(machine=self.m, active=True)
        ShiftRun.objects.filter(pk=self.sh.pk).update(mo=self.old, mo_start=self.sh.start)
        counts = [1000 + 10 * i for i in range(1, 11)] + [1100] * 10 + [1100 + 20 * i for i in range(1, 6)]
        for i, c in enumerate(counts, start=1):
            engine.tick(self.m, {"ppm": 0 if 11 <= i <= 20 else 180, "count": c}, self.t + timedelta(minutes=i))

    def start(self, **kw):
        p = {"line": self.m.name, "mo": self.new.number, "safety": [True] * len(engine.config()["safety"]), **kw}
        return engine.execute("startShift", p, engine.Ctx(user=self.op, line=self.m.name))

    def test_asks_and_splits_at_the_stop(self):
        sh = ShiftRun.objects.get(pk=self.sh.pk)
        self.assertEqual(sh.count, 200)
        r = self.start()
        self.assertFalse(r["ok"])
        self.assertIn("Pick when it changed over", r["error"])
        stop = Downtime.objects.get(shift=sh)
        c = Client(HTTP_HX_REQUEST="true")
        c.post("/kiosk/do/station", {"line": self.m.name})
        c.post("/kiosk/do/login", {"pin": self.op.pin})
        self.assertContains(c.get(f"/kiosk/r/coPick?line={self.m.name}&mo={self.new.number}"), f'value="stop:{stop.pk}"')
        self.assertNotContains(c.get(f"/kiosk/r/coPick?line={self.m.name}&mo={self.old.number}"), "coAt")
        r = self.start(coAt=f"stop:{stop.pk}")
        self.assertTrue(r["ok"], r)
        sh.refresh_from_db()
        self.assertEqual((sh.mo_id, sh.mo_count, sh.mo_start), (self.new.pk, 100, stop.end_time))
        self.assertEqual(MO.objects.get(pk=self.old.pk).produced, 100)
        self.assertEqual(MO.objects.get(pk=self.new.pk).produced, 100)
        self.assertEqual(MoRun.objects.get(shift=sh).qty, 100)
        stop.refresh_from_db()
        self.assertEqual(stop.reason, "Changeover")
        self.assertEqual(ChangeoverPiece.objects.get(shift=sh).ended, "nobody logged in")

    def test_before_this_shift_is_left_for_a_supervisor(self):
        r = self.start(coAt="before")
        self.assertTrue(r["ok"], r)
        sh = ShiftRun.objects.get(pk=self.sh.pk)
        self.assertEqual((sh.mo_id, sh.mo_count), (self.new.pk, 200))
        self.assertTrue(Notice.objects.filter(text__contains="Supervisor: check the earlier logs").exists())
