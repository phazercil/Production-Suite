"""A second run signal (two machines running together) and the stop pop-up on the operator's screen."""
from datetime import timedelta

from django.test import Client, TestCase
from django.utils import timezone

from hmi import engine
from hmi.models import Downtime, ShiftRun
from plant.demo import seed_demo
from plant.models import Machine


class Run2Tests(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.m = Machine.objects.get(name="Barbara")
        self.sh = ShiftRun.objects.get(line="Barbara", active=True)
        self.t = timezone.now().replace(second=0, microsecond=0) + timedelta(minutes=2)
        engine._planned_cache.clear()
        from unittest import mock
        p = mock.patch.object(engine, "roll_over", side_effect=lambda sh, t: sh)   # whatever the time of day: no shift change mid-test
        p.start()
        self.addCleanup(p.stop)

    def tick(self, i, run=True, run2=True):
        engine.tick(self.m, {"ppm": 190, "run": run, "run2": run2, "run2Name": "Folder"}, self.t + timedelta(minutes=i))

    def test_line_runs_only_while_both_do(self):
        from hmi import machinedata
        m = Machine(name="X", feed_type="db", feed_ppm_field="ppm", feed_src={"run2": {"field": "fold_run", "name": "Folder"}},
                    uptime_src={"type": "feed", "field": "run"})
        out = machinedata.add_extras(m, {}, {"run": 1, "fold_run": 0}.get)
        self.assertEqual((out["run"], out["run2"], out["run2Name"]), (True, False, "Folder"))
        self.tick(0)
        self.tick(1, run2=False)
        sh = ShiftRun.objects.get(pk=self.sh.pk)
        self.assertEqual((sh.state, sh.stop_what), ("stopped", "Folder"))
        for i in range(2, 2 + 8):
            self.tick(i, run2=False)
        self.tick(11)
        sh = ShiftRun.objects.get(pk=self.sh.pk)
        self.assertEqual((sh.state, sh.stop_what), ("running", ""))
        d = Downtime.objects.filter(shift=sh, start_time__gte=self.t).order_by("-start_time").first()
        if d:   # (unless the whole stop fell in planned time at this hour)
            self.assertEqual(d.notes, "Folder stopped")

    def test_stop_pop_up(self):
        c = Client(HTTP_HX_REQUEST="true")
        c.get("/station/Barbara/")
        self.tick(0)
        self.tick(1, run=False)
        ShiftRun.objects.filter(pk=self.sh.pk).update(stop_start=timezone.now() - timedelta(minutes=3))
        with self.settings():
            from unittest import mock
            with mock.patch.object(engine, "planned_for_tick", return_value=[]):
                r = c.get("/kiosk/r/notibar?page=controls")
                self.assertContains(r, "Barbara stopped")
                key = f"{self.sh.pk}:{engine.ms(ShiftRun.objects.get(pk=self.sh.pk).stop_start)}"
                r = c.post("/kiosk/do/ackStop", {"key": key, "page": "controls"})
                self.assertNotContains(r, "stoppop")
                self.assertNotContains(c.get("/kiosk/r/notibar?page=controls"), "stoppop")
