"""Machine schedule: days of the week, seasonal variants, and a change counting only from today (the days before
keep the schedule they ran)."""
from datetime import datetime, timedelta

from django.test import TestCase
from django.utils import timezone

from hmi import engine
from plant.demo import seed_demo
from plant.models import Machine, Person, Shift, ShiftVariant, ShiftVersion
from plant.schedule import book
from plant.util import ms, shift_windows


def day_ms(d):
    return ms(timezone.make_aware(datetime(d.year, d.month, d.day)))


class ShiftHistoryTests(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        ShiftVersion.objects.all().delete()
        self.admin = Person.objects.get(name="Plant Admin")
        self.today = timezone.localdate()
        self.s2 = Shift.objects.get(name="2nd shift")
        self.i2 = list(Shift.objects.all()).index(self.s2)
        Shift.objects.filter(pk=self.s2.pk).update(days=[0, 1, 2, 3, 4, 5, 6])
        Shift.objects.filter(name="1st shift").update(days=[0, 1, 2, 3, 4, 5, 6])

    def run_cmd(self, name, **p):
        r = engine.execute(name, p, engine.Ctx(user=self.admin))
        self.assertTrue(r["ok"], (name, r))

    def windows(self, d):
        return {w["name"]: w for w in shift_windows(day_ms(d))}

    def test_a_change_counts_from_today(self):
        y = self.today - timedelta(days=1)
        before = self.windows(y)["2nd shift"]
        self.run_cmd("shiftSet", s=self.i2, f="end", v="23:30")
        self.assertEqual(self.windows(y)["2nd shift"]["e"], before["e"])                       # yesterday as it ran
        self.assertEqual(self.windows(self.today)["2nd shift"]["e"] - day_ms(self.today), (23 * 60 + 30) * 60000)
        self.run_cmd("breakAdd", s=self.i2, kind="shutdown")                                    # a second change today: same version
        self.assertEqual(ShiftVersion.objects.filter(shift=self.s2).count(), 1)
        self.assertNotIn("Shut-down", [x["name"] for x in self.windows(y)["2nd shift"]["brk"]])
        self.assertIn("Shut-down", [x["name"] for x in self.windows(self.today)["2nd shift"]["brk"]])

    def test_days_of_the_week(self):
        sat = self.today + timedelta(days=(5 - self.today.weekday()) % 7 or 7)     # a Saturday ahead
        self.assertIn("2nd shift", self.windows(sat))
        self.run_cmd("shiftSet", s=self.i2, f="day", d=6)                         # untick Saturday
        self.assertNotIn(6, Shift.objects.get(pk=self.s2.pk).days)
        self.assertNotIn("2nd shift", self.windows(sat))

    def test_seasonal_variant(self):
        self.run_cmd("varAdd", s=self.i2)
        v = ShiftVariant.objects.get(shift=self.s2)
        a, b = self.today + timedelta(days=3), self.today + timedelta(days=5)
        self.run_cmd("varSet", v_id=v.pk, f="from", v=a.isoformat())
        self.run_cmd("varSet", v_id=v.pk, f="to", v=b.isoformat())
        self.run_cmd("varSet", v_id=v.pk, f="end", v="00:00")                    # an hour longer
        self.run_cmd("varBreak", v_id=v.pk, kind="shutdown")
        w = self.windows(a)["2nd shift"]
        self.assertEqual(w["e"] - day_ms(a), 24 * 3600000)
        self.assertIn("Shut-down", [x["name"] for x in w["brk"]])
        self.assertNotIn("Shut-down", [x["name"] for x in self.windows(b + timedelta(days=1))["2nd shift"]["brk"]])
        self.assertEqual(w["variant"], v.name if v.name else "Seasonal variant")
        self.assertEqual(self.windows(b + timedelta(days=1))["2nd shift"]["e"] - day_ms(b + timedelta(days=1)), 23 * 3600000)   # after: main schedule
        self.assertEqual(Shift.objects.get(pk=self.s2.pk).end, "23:00")          # the main schedule is untouched
        bar = Machine.objects.get(name="Barbara")
        t = timezone.make_aware(datetime(a.year, a.month, a.day, 23, 30))
        self.assertEqual(engine.machine_window(bar, t)["name"], "2nd shift")
        r = engine.execute("varSet", {"v_id": v.pk, "f": "to", "v": (a - timedelta(days=1)).isoformat()}, engine.Ctx(user=self.admin))
        self.assertFalse(r["ok"])                                                  # the end before the start

    def test_removed_and_new_shifts_keep_the_days_before(self):
        y = self.today - timedelta(days=1)
        self.run_cmd("shiftDel", s=self.i2)
        self.assertNotIn("2nd shift", self.windows(self.today))
        self.assertIn("2nd shift", self.windows(y))                                # it still ran yesterday
        self.assertFalse(Shift.objects.filter(pk=self.s2.pk).exists())
        self.assertTrue(Shift.all_objects.filter(pk=self.s2.pk).exists())
        self.run_cmd("shiftAdd")
        new = Shift.objects.order_by("-id").first()
        Shift.objects.filter(pk=new.pk).update(days=[0, 1, 2, 3, 4, 5, 6])
        self.assertIsNone(book().on(new.pk, y))
        self.assertIsNotNone(book().on(new.pk, self.today))

    def test_admin_page_shows_days_and_variants(self):
        from django.test import Client
        self.run_cmd("varAdd", s=self.i2)
        c = Client(HTTP_HX_REQUEST="true")
        c.get("/station/Barbara/")
        c.post("/kiosk/do/login", {"pin": "9999"}, HTTP_X_CSRFTOKEN=c.cookies["csrftoken"].value)
        r = c.get("/kiosk/v/admin?tab=shifts").content.decode()
        for txt in ("Add seasonal variant", "Seasonal variant ·", ">Mon<", ">Sun<"):
            self.assertIn(txt, r)



class LunchPerShiftTests(TestCase):
    """Running through lunch is set per shift: through on 1st shift (relief cover), stopped for lunch on 2nd."""
    def test_lunch_per_shift(self):
        from hmi.models import ShiftRun
        from plant.models import ShiftBreak
        seed_demo(reset=True)
        ShiftVersion.objects.all().delete()
        s1, s2 = Shift.objects.get(name="1st shift"), Shift.objects.get(name="2nd shift")
        Shift.objects.filter(pk__in=[s1.pk, s2.pk]).update(days=[0, 1, 2, 3, 4, 5, 6])
        ShiftBreak.objects.create(shift=s1, label="Lunch", start="10:00", end="10:30")
        ShiftBreak.objects.create(shift=s2, label="Lunch", start="18:00", end="18:30")
        ShiftVersion.objects.all().delete()
        bar = Machine.objects.get(name="Barbara")
        bar.lunch_shifts = [s1.pk]
        bar.save(update_fields=["lunch_shifts"])
        y = timezone.localdate() - timedelta(days=1)
        a = timezone.make_aware(datetime(y.year, y.month, y.day, 6, 0))
        sh = ShiftRun.objects.create(machine=bar, line=bar.name, shift_name="1st shift", start=a, end=a + timedelta(hours=17), active=False)
        kinds = [(timezone.localtime(s).strftime("%H:%M"), k) for s, e, k in engine.planned_windows(sh, kinds=True) if k == "Lunch"]
        self.assertIn(("18:00", "Lunch"), kinds)        # 2nd shift: the line stops for lunch
        self.assertNotIn(("10:00", "Lunch"), kinds)     # 1st shift: it runs through
