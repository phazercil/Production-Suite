"""Run screen extras: tooling from the asset database, how a part has run before, and the last day's shift reports."""
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase
from django.utils import timezone

from hmi import runinfo
from hmi.models import MoRun, SavedQuery, ShiftNote, ToolingCache
from plant.demo import seed_demo
from plant.models import MO

from .test_suite import no_planned_time

ROWS = [{"kind": "Screw set", "name": "SC-42", "detail": "set of 4"}, {"kind": "Helix", "name": "HX-9.1", "detail": None},
        {"kind": "Media", "name": "CC-2P-305", "detail": ""}, {"kind": "Side band", "name": "SB-125", "detail": ""},
        {"kind": "Side band", "name": "SB-125", "detail": ""}, {"kind": "Die", "name": "D-7", "detail": ""}]


class ToolingTests(TestCase):
    def setUp(self):
        runinfo.clear_cache()

    def test_one_row_per_tool(self):
        g = runinfo.norm_tooling(ROWS)
        self.assertEqual([x["label"] for x in g], ["Screws", "Helix", "Media", "Sidebands", "Die"])
        self.assertEqual(g[0]["items"], [{"name": "SC-42", "detail": "set of 4"}])
        self.assertEqual(len(g[3]["items"]), 1)           # duplicates collapse

    def test_one_row_with_a_column_per_kind(self):
        g = runinfo.norm_tooling([{"SCREWS": "SC-1", "helix": "HX-2", "media": None, "sideband": "SB-3"}])
        self.assertEqual({x["key"]: [i["name"] for i in x["items"]] for x in g}, {"screws": ["SC-1"], "helix": ["HX-2"], "media": [], "sideband": ["SB-3"]})

    def test_sources(self):
        t = timezone.now()
        with mock.patch("hmi.runinfo.integrations.assets_ready", return_value=False):
            self.assertEqual(runinfo.tooling("MG4062HP", "Barbara", t=t)["src"], "off")
        with mock.patch("hmi.runinfo.integrations.assets_ready", return_value=True):
            self.assertEqual(runinfo.tooling("MG4062HP", "Barbara", t=t)["src"], "noquery")
            SavedQuery.objects.create(name="part_tooling", sql="SELECT kind, name FROM tools WHERE part = :part", saved_by="x", saved_at=t)
            with mock.patch("hmi.runinfo.integrations.run_saved", return_value={"rows": ROWS}) as run:
                r = runinfo.tooling("2MG4062HP", "Barbara", t=t)
                self.assertEqual(r["src"], "db")
                self.assertEqual(run.call_args[0][1]["part"], "MG4062HP")     # 2MG shares the MG part
                runinfo.tooling("MG4062HP", "Barbara", t=t + timedelta(minutes=5))
                self.assertEqual(run.call_count, 1)                           # fresh for 15 minutes
            with mock.patch("hmi.runinfo.integrations.run_saved", side_effect=OSError("timed out")) as run:
                r = runinfo.tooling("MG4062HP", "Barbara", t=t + timedelta(minutes=20))
                self.assertEqual(r["src"], "stale")
                self.assertEqual(r["groups"][0]["items"][0]["name"], "SC-42")
                runinfo.tooling("MG4062HP", "Barbara", t=t + timedelta(minutes=21))
                self.assertEqual(run.call_count, 1)                           # backs off after a failure
                self.assertEqual(runinfo.tooling("MG9999", "Barbara", t=t)["src"], "error")
        with mock.patch("hmi.runinfo.integrations.assets_ready", return_value=False):
            r = runinfo.tooling("MG4062HP", "Barbara", t=t)
            self.assertEqual((r["src"], r["why"]), ("stale", "off"))


class ToolingQuerySaveTests(TestCase):
    def test_the_tooling_query_can_be_saved(self):
        from hmi import engine
        seed_demo(reset=True)
        from plant.models import Person
        ctx = engine.Ctx(user=Person.objects.get(role="admin"))
        with mock.patch("hmi.engine.integrations.run_saved", return_value={"columns": ["kind", "name"], "rows": []}), \
                mock.patch("hmi.engine._which_db", return_value="assets"):
            r = engine.execute("dbSave", {"name": "part_tooling", "sql": "SELECT a AS kind, b AS name FROM t WHERE p = :part"}, ctx)
        self.assertTrue(r["ok"], r)
        self.assertTrue(SavedQuery.objects.filter(name="part_tooling").exists())


class RunScreenTests(TestCase):
    def setUp(self):
        no_planned_time(self)
        cache.clear()
        runinfo.clear_cache()
        seed_demo(reset=True)
        self.c = Client(HTTP_HX_REQUEST="true")

    def test_averages_for_this_machine_and_any_machine(self):
        a = runinfo.averages("MG4062HP", "Barbara")
        self.assertEqual(a["here"]["runs"], 3)
        self.assertEqual(a["all"]["runs"], 5)
        for k in ("scrapRate", "perHour", "speed", "downPerHour"):
            self.assertGreater(a["all"][k], 0, k)
        # 1MG and 2MG count as the same part; other parts don't
        r = MoRun.objects.filter(line="Salvatore").first()
        MoRun.objects.filter(pk=r.pk).update(item="2MG4062HP")
        MoRun.objects.create(line="Barbara", mo="1", item="MG3045", qty=100, scrap=50, start_time=r.start_time, end_time=r.end_time)
        runinfo.clear_cache()
        self.assertEqual(runinfo.averages("MG4062HP", "Barbara")["all"]["runs"], 5)
        self.assertIsNone(runinfo.averages("MG7777", "Barbara")["here"])

    def test_report_history_is_the_last_day_on_this_line(self):
        t = timezone.now()
        ShiftNote.objects.create(t=t - timedelta(hours=2), line="Barbara", by="M. Lee", text="Running low on SB-125 side band")
        ShiftNote.objects.create(t=t - timedelta(hours=30), line="Barbara", by="M. Lee", text="old note")
        ShiftNote.objects.create(t=t - timedelta(hours=1), line="Salvatore", by="M. Lee", text="other line")
        rep = runinfo.report_history("Barbara", t)
        kinds = [r["kind"] for r in rep]
        texts = " ".join(r["text"] for r in rep)
        self.assertIn("Shift start", kinds)
        self.assertIn("End of shift", kinds)
        self.assertIn("Maintenance", kinds)
        self.assertIn("SB-125", texts)
        self.assertNotIn("old note", texts)
        self.assertNotIn("other line", texts)
        self.assertEqual(rep, sorted(rep, key=lambda r: r["t"], reverse=True))

    def test_run_screen_shows_next_changeover_tooling_averages_and_reports(self):
        t = timezone.now()
        groups = runinfo.norm_tooling(ROWS)
        ToolingCache.objects.create(part="MG3045", machine="Barbara", groups=groups, t=t)
        r = self.c.get("/kiosk/v/controls")
        self.assertContains(r, "next changeover")
        self.assertContains(r, "SC-42")
        self.assertContains(r, "Tooling comes from the asset database")
        self.assertContains(r, "How MG4062HP has run")
        self.assertContains(r, "On Barbara")
        self.assertContains(r, "Any machine")
        self.assertContains(r, "Shift reports")
        self.assertContains(r, "Side band guide on the left")
        # the next changeover is more than a day out: say so instead
        MO.objects.filter(number="306785").update(plan_start=t + timedelta(hours=30))
        r = self.c.get("/kiosk/v/controls")
        self.assertNotContains(r, "next changeover")


class ScheduleTimelineTests(TestCase):
    def setUp(self):
        no_planned_time(self)
        cache.clear()
        seed_demo(reset=True)
        self.c = Client(HTTP_HX_REQUEST="true")

    def test_runs_through_lunch_keeps_the_scheduler_running(self):
        from plant.models import Machine
        from plant.util import sod_ms
        from scheduler.engine import Calendar, load_world
        day = sod_ms(timezone.now().timestamp() * 1000)
        for i in range(7):   # a weekday the 2nd shift works
            d = day + i * 86400000
            world = load_world()
            m = next(x for x in world["cfg"]["machines"] if x["name"] == "Salvatore")
            stops = Calendar(world["cfg"]).day_iv(m, d)
            if stops:
                break
        sal = Machine.objects.get(name="Salvatore")
        sal.lunch_shifts = list(sal.shifts.values_list("id", flat=True))
        sal.save(update_fields=["lunch_shifts"])
        world = load_world()
        m = next(x for x in world["cfg"]["machines"] if x["name"] == "Salvatore")
        runs = Calendar(world["cfg"]).day_iv(m, d)
        mins = lambda iv: sum(e - s for s, e in iv) / 60000
        self.assertEqual(mins(runs) - mins(stops), 60)       # two 30-minute lunches (1st and 2nd shift) now run
        self.assertEqual(len(runs), len(stops) - 2)

    def test_lunch_and_finish_flag_on_the_timeline(self):
        r = self.c.get("/kiosk/v/schedule?view=timeline")
        body = r.content.decode()
        self.assertIn('class="fin"', body)                                # checkered flag at the MO's end
        self.assertIn("Barbara runs through lunch", body)                 # the demo's Barbara has relief cover
        self.assertNotIn("Salvatore", body)                               # only this screen's line
        self.c.post("/kiosk/do/station", {"line": "Salvatore"})
        self.assertIn("Salvatore stops for lunch", self.c.get("/kiosk/v/schedule?view=timeline").content.decode())

    def test_changeover_in_progress_turns_red_when_over_time(self):
        from hmi.models import ShiftRun
        from plant.util import ms
        sh = ShiftRun.objects.get(line="Barbara", active=True)
        nm = MO.objects.get(number="306785")
        sh.state, sh.mo = "changeover", None
        sh.co = {"start": ms(timezone.now() - timedelta(minutes=10)), "step": 3, "newMo": nm.pk}
        sh.save()
        r = self.c.get("/kiosk/v/schedule?view=timeline")
        self.assertContains(r, "Currently Changing Over")
        self.assertNotContains(r, "blk co live over")
        sh.co = {**sh.co, "start": ms(timezone.now() - timedelta(minutes=45))}
        sh.save()
        r = self.c.get("/kiosk/v/schedule?view=timeline")
        self.assertContains(r, "blk co live over")
        self.assertContains(r, "15 min over")                             # Barbara is allowed 30 minutes


class ScheduleWeekTests(TestCase):
    """The kiosk's Schedule page shows only this screen's line, and only this week's (or next week's) MOs."""

    def setUp(self):
        no_planned_time(self)
        cache.clear()
        seed_demo(reset=True)
        self.c = Client(HTTP_HX_REQUEST="true")
        self.c.post("/kiosk/do/station", {"line": "Barbara"})

    def test_week_and_list_are_this_lines_only(self):
        from datetime import timedelta as td
        from plant.models import Machine
        bar, sal = Machine.objects.get(name="Barbara"), Machine.objects.get(name="Salvatore")
        now = timezone.now()
        here = MO.objects.create(number="950001", part="MG4062HP", qty=100, status="OPEN", line=bar, seq=50,
                                 plan_start=now + td(hours=2), plan_end=now + td(hours=4))
        later = MO.objects.create(number="950002", part="MG4062HP", qty=100, status="OPEN", line=bar, seq=51,
                                  plan_start=now + td(days=15), plan_end=now + td(days=15, hours=2))
        other = MO.objects.create(number="950003", part="MG4062HP", qty=100, status="OPEN", line=sal, seq=50,
                                  plan_start=now + td(hours=2), plan_end=now + td(hours=4))
        r = self.c.get("/kiosk/v/schedule?view=week&week=0").content.decode()
        self.assertIn("Schedule · Barbara", r)
        self.assertIn("950001", r)
        self.assertNotIn("950002", r)                  # weeks away
        self.assertNotIn("950003", r)                  # another line
        self.assertIn('class="wk"', r)
        r = self.c.get("/kiosk/v/schedule?view=timeline").content.decode()
        self.assertNotIn("950003", r)
        self.assertNotIn(">Salvatore<", r)
        locked = Client(HTTP_HX_REQUEST="true")
        locked.get("/station/Barbara/schedule/")
        r = locked.get("/kiosk/v/schedule").content.decode()
        self.assertIn("Schedule · Barbara", r)
        self.assertNotIn("Plan in the Scheduler", r)           # nothing on a machine's own screen leads off it
