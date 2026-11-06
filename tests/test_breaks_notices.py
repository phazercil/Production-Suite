"""Extra breaks an Admin or shift lead schedules, and the operator notification bar."""
from datetime import timedelta

from django.core.cache import cache
from django.test import Client, TestCase
from django.utils import timezone

from hmi import engine
from hmi.models import Downtime, Notice, ShiftRun
from plant.demo import seed_demo
from plant.models import ExtraBreak
from plant.util import local

from .test_suite import no_planned_time


class ExtraBreakTests(TestCase):
    def setUp(self):
        no_planned_time(self)
        cache.clear()
        seed_demo(reset=True)
        self.c = Client(HTTP_HX_REQUEST="true")

    def login(self, pin):
        self.c.post("/kiosk/do/login", {"pin": pin})

    def add(self, **kw):
        t = local(timezone.now())
        data = {"label": "Heat break", "start": (t + timedelta(minutes=60)).strftime("%H:%M"), "minutes": "15", "day": t.date().isoformat()}
        data.update(kw)
        return self.c.post("/kiosk/do/extraBreakAdd", data)

    def test_only_leads_schedule_breaks_and_bad_input_is_refused(self):
        self.login("1111")                                         # operator
        self.add()
        self.assertFalse(ExtraBreak.objects.exists())
        self.c.post("/kiosk/do/logout")
        self.login("4444")                                         # shift lead
        r = self.c.get("/kiosk/m/extraBreak")
        self.assertContains(r, "Schedule a break")
        for bad in ({"minutes": "0"}, {"minutes": "500"}, {"start": "25:00"}, {"day": "2001-01-01"}, {"until": "2001-01-01"}):
            r = self.add(**bad)
            self.assertFalse(ExtraBreak.objects.exists(), bad)
        self.add(**{"machines:l": ["Barbara"]})
        self.add(**{"machines:l": ["Barbara"]})                    # the same break twice
        x = ExtraBreak.objects.get()
        self.assertEqual(([m.name for m in x.machines.all()], x.minutes, x.label), (["Barbara"], 15, "Heat break"))
        n = Notice.objects.filter(kind="break").get()
        self.assertEqual(n.lines, ["Barbara"])
        self.assertIn("Heat break", n.text)

    def test_a_stop_during_the_break_is_not_downtime_and_runs_pause(self):
        from unittest import mock
        mock.patch.stopall()                                       # use the real planned time here
        sh = ShiftRun.objects.get(line="Barbara", active=True)
        t = local(timezone.now()).replace(second=0, microsecond=0)
        a = t - timedelta(minutes=40)
        ExtraBreak.objects.create(day=a.date(), start=a.strftime("%H:%M"), minutes=15, label="Heat break")
        engine._planned_cache.clear()
        kinds = [k for _, _, k in engine.planned_windows(sh, kinds=True)]
        from plant.models import Shift
        from plant.util import hm
        shift = Shift.objects.filter(name=sh.shift_name).first()
        inside = shift and ((a.hour * 60 + a.minute - hm(shift.start)) % 1440) + 15 <= (hm(shift.end) - hm(shift.start)) % 1440
        if a.date() == local(sh.start).date() and inside:   # the break falls within the run's shift at this time of day
            self.assertIn("Heat break", kinds)
            before = Downtime.objects.filter(shift=sh).count()
            engine.add_down(sh, a + timedelta(minutes=2), a + timedelta(minutes=12))      # all inside the break
            self.assertEqual(Downtime.objects.filter(shift=sh).count(), before)
        # the scheduler takes the break out of run time: put a 20-minute break inside a stretch of run time that
        # has no break of its own (so the test doesn't depend on the time of day it runs)
        from scheduler.engine import Calendar, load_world
        from plant.util import from_ms, sod_ms
        world = load_world()
        m = next(x for x in world["cfg"]["machines"] if x["name"] == "Barbara")
        for k in range(1, 8):
            day = sod_ms(timezone.now().timestamp() * 1000) + k * 86400000
            iv1 = Calendar(world["cfg"], extra=world["extraBreaks"]).day_iv(m, day)
            gap = next(((s, e) for s, e in iv1 if e - s >= 60 * 60000), None)
            if gap:
                break
        at = local(from_ms(gap[0] + 10 * 60000))
        ExtraBreak.objects.create(day=at.date(), start=at.strftime("%H:%M"), minutes=20)
        w2 = load_world()
        iv2 = Calendar(w2["cfg"], extra=w2["extraBreaks"]).day_iv(m, day)
        self.assertEqual(sum(e - s for s, e in iv1) - sum(e - s for s, e in iv2), 20 * 60000)

    def test_timeline_and_list_and_cancel(self):
        self.login("4444")
        self.add()
        r = self.c.get("/kiosk/v/schedule?view=timeline")
        self.assertContains(r, "Extra breaks")
        self.assertContains(r, "Every line")
        x = ExtraBreak.objects.get()
        self.c.post("/kiosk/do/extraBreakDel", {"id": x.id})
        self.assertFalse(ExtraBreak.objects.exists())
        self.assertTrue(Notice.objects.filter(kind="break", text__contains="cancelled").exists())


class NotificationBarTests(TestCase):
    def setUp(self):
        no_planned_time(self)
        cache.clear()
        seed_demo(reset=True)
        Notice.objects.all().delete()
        self.c = Client(HTTP_HX_REQUEST="true")

    def bar(self, line):
        return self.c.get(f"/station/{line}/").content.decode()

    def test_each_machine_sees_and_acknowledges_its_own(self):
        engine.notify("Barbara only thing", "schedule", lines=["Barbara"], go="schedule")
        engine.notify("For everyone", "info")
        engine.notify("Planner only", "planner")
        b, s = self.bar("Barbara"), self.bar("Salvatore")
        self.assertIn("For everyone", b)                     # newest first
        self.assertIn("+1 more", b)
        self.assertNotIn("Barbara only thing", s)
        self.assertIn("For everyone", s)
        self.assertNotIn("Planner only", b + s)
        # Got it on Barbara's screen doesn't hide it on Salvatore's
        n = Notice.objects.get(text="For everyone")
        self.c.post("/station/Barbara/", {})                 # pick up the station lock in the session
        cl = Client(HTTP_HX_REQUEST="true")
        cl.get("/station/Barbara/")
        cl.post("/kiosk/do/ackNotices", {"id": n.id})
        self.assertNotIn("For everyone", cl.get("/station/Barbara/").content.decode())
        self.assertIn("Barbara only thing", cl.get("/station/Barbara/").content.decode())
        self.assertIn("For everyone", self.bar("Salvatore"))

    def test_events_notify_the_right_lines(self):
        from hmi.models import QCCheck, Ticket
        ctx = engine.Ctx(user=None)
        t = Ticket.objects.get()
        engine.ticket_note({"id": t.id, "text": "ordered a part"}, ctx)
        self.assertEqual(Notice.objects.get(kind="maint").lines, ["Barbara"])
        q = QCCheck.objects.filter(passed=False).first()
        engine.qc_review({"id": q.id, "decision": "approved"}, ctx)
        self.assertEqual(Notice.objects.get(kind="qc").go, "qc-log")
        engine.notify("x", "update")
        self.assertNotIn(">x<", self.bar("Barbara"))
