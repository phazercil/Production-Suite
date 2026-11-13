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

    def test_the_log_always_runs_and_splits_at_each_window(self):
        Shift.objects.filter(name="All day").update(start="06:00", end="06:30")
        day = local(self.t).replace(hour=6, minute=0)
        self.tick(day - timedelta(minutes=10))
        a = ShiftRun.objects.get(machine=self.m, active=True)
        self.assertEqual(a.shift_name, engine.NO_SHIFT)                  # before the shift: logged, as "No shift"
        self.tick(day + timedelta(minutes=5))
        a.refresh_from_db()
        self.assertFalse(a.active)
        self.assertEqual(local(a.end).strftime("%H:%M"), "06:00")        # split at the shift's start
        b = ShiftRun.objects.get(machine=self.m, active=True)
        self.assertEqual((b.shift_name, local(b.start).strftime("%H:%M")), ("All day", "06:00"))
        self.tick(day + timedelta(minutes=31))
        b.refresh_from_db()
        self.assertEqual(local(b.end).strftime("%H:%M"), "06:30")
        c = ShiftRun.objects.get(machine=self.m, active=True)
        self.assertEqual((c.shift_name, local(c.start).strftime("%H:%M")), (engine.NO_SHIFT, "06:30"))
        # a stop in "No shift" time is never downtime
        for i in range(32, 45):
            engine.tick(self.m, {"ppm": 0}, day + timedelta(minutes=i))
        engine.tick(self.m, {"ppm": 190}, day + timedelta(minutes=45))
        self.assertFalse(Downtime.objects.filter(shift=c).exists())

    def test_working_into_the_next_shift_and_changing_operator(self):
        from plant.models import ShiftBreak  # noqa: F401
        Shift.objects.filter(name="All day").update(start="06:00", end="14:00")
        Shift.objects.create(name="Late", start="14:00", end="22:00", days=[0, 1, 2, 3, 4, 5, 6])
        self.m.shifts.add(Shift.objects.get(name="Late"))
        day = local(self.t).replace(hour=13, minute=50)
        self.tick(day)
        mo = MO.objects.filter(status="OPEN").first()
        safety = [True] * len(engine.config()["safety"])
        r = engine.execute("startShift", {"line": self.m.name, "mo": mo.number, "safety": safety}, engine.Ctx(user=self.op, t=day + timedelta(minutes=1)))
        self.assertTrue(r["ok"], r)
        self.tick(day + timedelta(minutes=12))                           # 14:02: the next shift's log
        late = ShiftRun.objects.get(machine=self.m, active=True)
        self.assertEqual((late.shift_name, late.mo, late.offer, late.person), ("Late", mo, str(self.op.pk), None))
        # the kiosk asks: "M. Lee, working into Late?"
        from django.test import Client
        c = Client(HTTP_HX_REQUEST="true")
        c.get(f"/station/{self.m.name}/")
        self.assertContains(c.get("/kiosk/v/controls"), "working into Late?")
        r = engine.execute("carryOn", {"id": late.pk, "yes": True}, engine.Ctx())
        self.assertTrue(r["ok"], r)
        late.refresh_from_db()
        self.assertEqual((late.person, late.offer), (self.op, ""))
        # Change Operator: the next person takes over from now, same MO
        other = Person.objects.get(name="Plant Admin")
        other.trained.add(self.m)
        r = engine.execute("changeOperator", {"line": self.m.name}, engine.Ctx(user=other, t=day + timedelta(minutes=20)))
        self.assertTrue(r["ok"], r)
        late.refresh_from_db()
        self.assertFalse(late.active)
        now_run = ShiftRun.objects.get(machine=self.m, active=True)
        self.assertEqual((now_run.person, now_run.mo, now_run.shift_name), (other, mo, "Late"))


class LogWindowTests(TestCase):
    """A log belongs to the shift named on it: clocking in a few minutes early for 2nd shift is not split at 1st
    shift's end, and the demo run (named for the shift going on now) stays one log at any time of day."""
    def setUp(self):
        seed_demo(reset=True)
        self.m = Machine.objects.get(name="Salvatore")
        ShiftRun.objects.filter(machine=self.m).delete()
        self.m.shifts.set(Shift.objects.filter(name__in=["1st shift", "2nd shift"]))
        from datetime import datetime
        self.day = timezone.make_aware(datetime(2026, 11, 4))   # a Wednesday

    def at(self, h, m=0):
        return self.day + timedelta(hours=h, minutes=m)

    def test_early_clock_in_for_the_next_shift_is_not_split(self):
        sh = ShiftRun.objects.create(machine=self.m, line=self.m.name, person=Person.objects.get(name="M. Lee"),
                                     user_name="M. Lee", shift_name="2nd shift", start=self.at(14, 25), state="running")
        self.assertEqual(engine.roll_over(sh, self.at(14, 31)).pk, sh.pk)
        self.assertEqual(engine.log_window(sh, self.at(14, 31))["e"], self.at(23))

    def test_a_log_named_for_its_own_shift_still_splits(self):
        sh = ShiftRun.objects.create(machine=self.m, line=self.m.name, person=Person.objects.get(name="M. Lee"),
                                     user_name="M. Lee", shift_name="1st shift", start=self.at(10), state="running")
        nxt = engine.roll_over(sh, self.at(14, 31))
        self.assertNotEqual(nxt.pk, sh.pk)
        self.assertEqual((nxt.shift_name, nxt.offer), ("2nd shift", str(sh.person_id)))


class ChangeoverPassOverTests(TestCase):
    """Pass over: a changeover handed from one operator and shift to the next, each piece kept (who, shift, time,
    steps), the wait with nobody logged in included."""
    setUp = UnattendedTests.setUp
    tick = UnattendedTests.tick
    def test_pass_over_to_the_next_shift(self):
        from django.test import Client

        from hmi.models import ChangeoverPiece
        Shift.objects.filter(name="All day").update(start="06:00", end="14:00")
        late = Shift.objects.create(name="Late", start="14:00", end="22:00", days=[0, 1, 2, 3, 4, 5, 6])
        self.m.shifts.add(late)
        day = local(self.t).replace(hour=13, minute=20)
        line = {"line": self.m.name}
        lee, other = self.op, Person.objects.get(name="Plant Admin")
        other.trained.add(self.m)
        mos = list(MO.objects.filter(status="OPEN", line__isnull=True)[:2]) or list(MO.objects.filter(status="OPEN")[:2])
        safety = [True] * len(engine.config()["safety"])

        def run(name, p, who, mins):
            r = engine.execute(name, {**line, **p}, engine.Ctx(user=who, t=day + timedelta(minutes=mins)))
            self.assertTrue(r["ok"], (name, r))
            return r

        self.tick(day)
        run("startShift", {"mo": mos[0].number, "safety": safety}, lee, 1)
        run("coOpen", {}, lee, 5)
        run("coEnd", {"checks": [True] * 10, "qty": 10, "scrap": 0}, lee, 10)                     # 13:30 the changeover starts
        run("coPick", {"mo": mos[1].number}, lee, 12)
        run("coStep", {"i": 0, "checked": True}, lee, 15)
        run("coStep", {"i": 1, "checked": True}, lee, 20)
        n = len(engine.co_steps(ShiftRun.objects.get(machine=self.m, active=True).co))
        # near the end of the shift the changeover dialog offers Pass over
        c = Client(HTTP_HX_REQUEST="true")
        c.get(f"/station/{self.m.name}/")
        run("coPass", {"note": "Steps 1-2 done, wheels on the cart"}, lee, 35)   # 13:55
        sh = ShiftRun.objects.get(machine=self.m, active=True)
        self.assertTrue(engine.unattended(sh))
        self.assertEqual((sh.state, sh.co["handoff"]["by"], sh.co["handoff"]["note"]), ("changeover", "M. Lee", "Steps 1-2 done, wheels on the cart"))
        self.assertContains(c.get("/kiosk/v/controls"), "Changeover waiting to be picked up")
        self.tick(day + timedelta(minutes=41))                              # 14:01: the Late shift's log carries it on
        sh = ShiftRun.objects.get(machine=self.m, active=True)
        self.assertEqual((sh.shift_name, sh.state, len(sh.co["done"])), ("Late", "changeover", 2))
        run("startShift", {"safety": safety}, other, 50)                    # 14:10 the next operator picks it up
        for i in range(2, n):
            run("coStep", {"i": i, "checked": True}, other, 55)
        run("coFinish", {}, other, 80)                                       # 14:40
        ps = list(ChangeoverPiece.objects.order_by("start"))
        self.assertEqual([(p.by, p.shift_name, p.minutes, len(p.steps), p.ended) for p in ps], [
            ("M. Lee", "All day", 25, 2, "passed over"),
            ("", "All day", 5, 0, "shift change"),
            ("", "Late", 10, 0, "picked up"),
            ("Plant Admin", "Late", 30, n - 2, "finished"),
        ])
        self.assertEqual(len({p.co_id for p in ps}), 1)
        self.assertEqual(ps[0].note, "Steps 1-2 done, wheels on the cart")
        self.assertEqual(ShiftRun.objects.get(machine=self.m, active=True).mo, mos[1])

    def test_ending_the_shift_mid_changeover_passes_it_over(self):
        day = local(self.t).replace(hour=10, minute=0)
        mo = MO.objects.filter(status="OPEN").first()
        safety = [True] * len(engine.config()["safety"])
        ctx = lambda m: engine.Ctx(user=self.op, t=day + timedelta(minutes=m))  # noqa: E731
        self.tick(day)
        for name, p, m in (("startShift", {"mo": mo.number, "safety": safety}, 1), ("coOpen", {}, 2), ("coEnd", {"checks": [True] * 10, "qty": 5, "scrap": 0}, 3)):
            self.assertTrue(engine.execute(name, {"line": self.m.name, **p}, ctx(m))["ok"])
        r = engine.execute("endShift", {"line": self.m.name, "issues": "Old die off"}, ctx(20))
        self.assertTrue(r["ok"], r)
        sh = ShiftRun.objects.get(machine=self.m, active=True)
        self.assertEqual((engine.unattended(sh), sh.state, sh.co["handoff"]["note"]), (True, "changeover", "Old die off"))
