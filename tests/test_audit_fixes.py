"""The fixes from the two November audits: data integrity, reporting accuracy and robustness."""
import json
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from hmi import engine
from hmi.models import Downtime, LineState, PpmReading, ShiftRun, Ticket
from plant.demo import seed_demo
from plant.models import MO, Machine, Person, PlantSettings
from tests.test_suite import all_day_shift, no_planned_time


class Engine(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.bar = Machine.objects.get(name="Barbara")

    def test_push_feed_without_a_counter_counts_from_speed(self):
        from hmi import ticker
        Machine.objects.filter(pk=self.bar.pk).update(feed_type="push", feed_count_field="")
        m = Machine.objects.get(pk=self.bar.pk)
        ls, _ = LineState.objects.get_or_create(machine=m)
        ls.pushed, ls.pushed_at = {"ppm": 190}, timezone.now()
        ls.save()
        s, _ = ticker._feed_sample(m, ls, timezone.now())
        self.assertNotIn("count", s)
        self.assertFalse(engine.has_counter(m, s))
        self.assertTrue(engine.has_counter(m, {"ppm": 1, "count": 5}))
        self.assertFalse(engine.has_counter(m, {"ppm": 1, "count": None}))
        # a reading pushed after a missed minute isn't stamped on that minute
        s, feed = ticker._feed_sample(m, ls, timezone.now() - timedelta(minutes=4))
        self.assertIsNone(s)

    def test_no_inverted_downtime(self):
        sh = ShiftRun.objects.filter(machine=self.bar, active=True).first()
        t = timezone.now()
        self.assertIsNone(engine.add_down(sh, t, t - timedelta(seconds=37), "Changeover"))
        self.assertFalse(Downtime.objects.filter(shift=sh, end_time__lt=t - timedelta(seconds=30), start_time__gte=t).exists())

    def test_co_end_needs_a_whole_count(self):
        op = engine.Ctx(user=Person.objects.get(pin="9999"), line="Barbara")
        engine.execute("coOpen", {}, op)
        all_ = [True] * 20
        r = engine.execute("coEnd", {"checks": all_}, op)
        self.assertFalse(r["ok"])
        self.assertIn("final count", r["error"])
        r = engine.execute("coEnd", {"checks": all_, "qty": "12.9"}, op)
        self.assertIn("whole number", r["error"])

    def test_planned_windows_of_a_closed_run_stop_at_its_end(self):
        sh = ShiftRun.objects.filter(machine=self.bar).first()
        sh.start, sh.end = timezone.now() - timedelta(days=30), timezone.now() - timedelta(days=29, hours=16)
        wins = engine.planned_windows(sh)
        self.assertTrue(all(b <= sh.end for a, b in wins))

    def test_carried_into_the_next_log(self):
        no_planned_time(self)
        sh = ShiftRun.objects.filter(machine=self.bar, active=True).first()
        sh.qc_after_co, sh.qc_due_at, sh.override = True, timezone.now(), {"pleats": 40}
        sh._stopped_at, sh._stopped_what = timezone.now() - timedelta(minutes=3), ""
        c = engine._carried(sh, timezone.now())
        self.assertEqual((c["qc_after_co"], c["override"]), (True, {"pleats": 40}))
        self.assertIn("stop_start", c)


class Ticker(TestCase):
    def setUp(self):
        seed_demo(reset=True)

    def test_a_lost_lease_skips_the_rest_of_the_pass(self):
        from hmi import ticker
        t = timezone.now()
        LineState.objects.update(next_tick=t.replace(second=0, microsecond=0))
        PlantSettings.objects.filter(pk=1).update(ticker_owner="b")
        with mock.patch.object(engine, "tick"):
            self.assertFalse(ticker.tick_machines(t, owner="a"))
        PlantSettings.objects.filter(pk=1).update(ticker_owner="a")
        with mock.patch.object(engine, "tick"):
            self.assertTrue(ticker.tick_machines(t, owner="a"))


class Updates(TestCase):
    def test_state_writes_and_damaged_backup_meta(self):
        from plant import updates
        d = Path(tempfile.mkdtemp())
        with override_settings(BASE_DIR=d):
            updates.set_state(status="idle", message="a")
            with updates.locked():
                with updates.locked():   # re-entrant in one thread
                    updates.set_state(message="b")
            self.assertEqual(updates.state()["message"], "b")
            self.assertEqual(list((d / "updates").glob("*.tmp")), [])
            self.assertFalse((d / "updates" / "updates.lock").exists())
            (d / "backups" / "x").mkdir(parents=True)
            (d / "backups" / "x" / "meta.json").write_text('["not a dict"]')
            (d / "backups" / "y").mkdir()
            (d / "backups" / "y" / "meta.json").write_text('{"revision": "abc"}')
            self.assertEqual(updates.backups(), [])


class Views(TestCase):
    def setUp(self):
        seed_demo(reset=True)

    def test_ping_doesnt_keep_an_idle_lead_signed_in(self):
        c = Client()
        c.get("/supervisor/")
        c.post("/api/hmi/login", json.dumps({"pin": "9999"}), content_type="application/json")
        s = c.session
        s["last_act"] = timezone.now().timestamp() - 3600
        s["sup"] = 1
        s.save()
        self.assertTrue(c.post("/api/hmi/ping", "{}", content_type="application/json").json()["note"])
        self.assertNotIn("pid", c.session)
        self.assertNotIn("sup", c.session)

    def test_pin_limit_counts_by_address_without_the_cookie(self):
        for _ in range(20):
            Client().post("/api/hmi/login", json.dumps({"pin": "0000x"}), content_type="application/json")
        r = Client().post("/api/hmi/login", json.dumps({"pin": "9999"}), content_type="application/json").json()
        self.assertFalse(r["ok"])
        self.assertIn("Too many", r["error"])


class Scheduler(TestCase):
    def test_pacing_weighs_by_output(self):
        from scheduler.engine import frac_at, keeps_up
        fast_short = [(0, 10, 9.0)]    # 90 made in 10 minutes
        slow_long = [(0, 100, 0.1)]    # 10 made in 100 minutes
        up = sorted(fast_short + slow_long)
        self.assertAlmostEqual(frac_at(up, 10), (90 + 1) / 100)
        self.assertTrue(keeps_up([(5, 120)], up))
        self.assertFalse(keeps_up([(5, 50)], up))     # done by 50 while the slow line still has 5% to go
        self.assertEqual(frac_at([(0, 10)], 5), 0.5)


class Reporting(TestCase):
    def setUp(self):
        seed_demo(reset=True)

    def test_spc_within_and_overall(self):
        from quality import spc
        st = spc.stats([10.0, 10.02, 9.98, 10.01, 9.99] * 6, (10.0, 9.9, 10.1))
        self.assertAlmostEqual(st["cp"], 0.2 / (6 * st["mr"] / 1.128))
        self.assertAlmostEqual(st["pp"], 0.2 / (6 * st["sd"]))
        self.assertFalse(st["preliminary"])

    def test_mttr_uses_when_it_was_completed(self):
        from maintenance import reliability
        now = timezone.now()
        t = Ticket.objects.create(t=now - timedelta(hours=5), machine="Barbara", by="x", type="Jam", status="Open")
        ctx = engine.Ctx(user=Person.objects.get(pin="9999"))
        engine.execute("ticketStatus", {"id": t.pk, "close": True}, ctx)
        t.refresh_from_db()
        closed = t.closed_at
        self.assertIsNotNone(closed)
        Ticket.objects.filter(pk=t.pk).update(updated=now + timedelta(days=3))   # a note added later
        t.refresh_from_db()
        self.assertEqual(reliability.done(t), closed)

    def test_availability_against_planned_time(self):
        from reports import engine as rep
        a = timezone.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)
        b = a + timedelta(hours=2)
        PpmReading.objects.all().delete()
        for i in range(30):   # 30 running minutes with readings; the rest of the planned 120 has none
            PpmReading.objects.create(line="Barbara", t=a + timedelta(minutes=i), ppm=180, units=3, run=True)
        with mock.patch.object(rep, "planned_minutes", return_value=120.0):
            tot, rows = rep.summarize({"level": "line", "line": "Barbara"}, a, b, 10, with_rows=False)
        self.assertEqual(tot["uptime"], 100.0)      # of what was observed
        self.assertEqual(tot["avail"], 25.0)        # of the planned time
        self.assertTrue(tot["lowCoverage"])
        self.assertEqual(tot["availBasis"], "planned")

    def test_planned_minutes_subtracts_breaks(self):
        from reports import engine as rep
        a = timezone.now()
        with mock.patch("hmi.engine.planned_windows", return_value=[(a, a + timedelta(minutes=30)), (a + timedelta(minutes=20), a + timedelta(minutes=40))]):
            self.assertEqual(rep.planned_minutes("Barbara", a, a + timedelta(minutes=100)), 60.0)

    def test_timeline_says_when_it_was_cut(self):
        from supervisor import timeline
        sh = ShiftRun.objects.first()
        t = timezone.now() - timedelta(hours=1)
        for i in range(6):
            Downtime.objects.create(shift=sh, line="Barbara", start_time=t + timedelta(minutes=i), end_time=t + timedelta(minutes=i, seconds=30), minutes=1)
        info = {}
        with mock.patch.object(timeline, "LIMIT", 3):
            evs = timeline.events({"A": t - timedelta(hours=1), "B": t + timedelta(hours=1), "line": "", "who": ""}, "down", "", info=info)
        self.assertEqual(info["sources"], ["Downtime"])
        self.assertTrue(all(e["t"] >= info["cutoff"] for e in evs))

    def test_health_counts_every_problem(self):
        from plant import health
        from hmi.models import DataSource
        DataSource.objects.create(name="Vision", mode="push", enabled=True, last_ok=False)
        h = health.build() if hasattr(health, "build") else health.status()
        self.assertIn("A data source is failing", h["problems"])
        self.assertEqual(h["summary"], "warn")


class CountFromSpeed(TestCase):
    def setUp(self):
        no_planned_time(self)
        seed_demo(reset=True)
        self.m = Machine.objects.get(name="Barbara")
        Machine.objects.filter(pk=self.m.pk).update(feed_count_field="")
        all_day_shift(self.m)

    def run_minutes(self):
        sh = ShiftRun.objects.get(machine=self.m, active=True)
        before = sh.count
        t = timezone.now().replace(second=0, microsecond=0)
        m = Machine.objects.get(pk=self.m.pk)
        for i in range(1, 4):
            engine.tick(m, {"ppm": 400}, t + timedelta(minutes=i))
        return ShiftRun.objects.get(pk=sh.pk).count - before

    def test_toggle(self):
        self.assertGreater(self.run_minutes(), 0)               # on (the default): counted from the speed
        ctx = engine.Ctx(user=Person.objects.get(pin="9999"))
        self.assertTrue(engine.execute("lineSet", {"id": "Barbara", "f": "countFromSpeed", "v": False}, ctx)["ok"])
        self.assertFalse(Machine.objects.get(pk=self.m.pk).count_from_speed)
        self.assertEqual(self.run_minutes(), 0)                 # off: nothing until a counter is hooked up
        Machine.objects.filter(pk=self.m.pk).update(feed_count_field="count")
        sh = ShiftRun.objects.get(machine=self.m, active=True)
        t = timezone.now().replace(second=0, microsecond=0) + timedelta(minutes=10)
        m = Machine.objects.get(pk=self.m.pk)
        engine.tick(m, {"ppm": 400, "count": 100}, t)
        engine.tick(m, {"ppm": 400, "count": 130}, t + timedelta(minutes=1))
        self.assertEqual(ShiftRun.objects.get(pk=sh.pk).count - sh.count, 30)   # a counter counts either way

    def test_counter_that_gives_filters_per_minute(self):
        Machine.objects.filter(pk=self.m.pk).update(feed_count_field="count")
        ctx = engine.Ctx(user=Person.objects.get(pin="9999"))
        self.assertTrue(engine.execute("lineSet", {"id": "Barbara", "f": "countKind", "v": "minute"}, ctx)["ok"])
        sh = ShiftRun.objects.get(machine=self.m, active=True)
        t = timezone.now().replace(second=0, microsecond=0) + timedelta(minutes=20)
        m = Machine.objects.get(pk=self.m.pk)
        for i, v in enumerate((7, 7, 7.5, 7.5, 0)):
            engine.tick(m, {"ppm": 170, "count": v}, t + timedelta(minutes=i))
        self.assertEqual(ShiftRun.objects.get(pk=sh.pk).count - sh.count, 29)   # 7 + 7 + 7.5 + 7.5 (halves carry)


class RecipeChange(TestCase):
    def setUp(self):
        no_planned_time(self)
        seed_demo(reset=True)
        self.m = Machine.objects.get(name="Barbara")
        Machine.objects.filter(pk=self.m.pk).update(feed_count_field="count", count_kind="minute")
        all_day_shift(self.m)
        ShiftRun.objects.filter(machine=self.m, active=True).update(acc=0.0)
        self.sh = ShiftRun.objects.get(machine=self.m, active=True)
        self.old = self.sh.mo
        self.t = timezone.now().replace(second=0, microsecond=0) + timedelta(minutes=30)

    def minutes(self, recipes, start=0):
        m = Machine.objects.get(pk=self.m.pk)
        for i, r in enumerate(recipes, start=start):
            engine.tick(m, {"ppm": 170, "count": 7, "recipe": r}, self.t + timedelta(minutes=i))

    def test_a_new_recipe_ends_the_mo_and_holds_what_follows(self):
        before = MO.objects.get(pk=self.old.pk).produced
        self.minutes(["MG4062HP"] * 3 + ["AQ1234"] * 4)
        sh = ShiftRun.objects.get(pk=self.sh.pk)
        self.assertTrue(sh.rx["off"])
        self.assertEqual(MO.objects.get(pk=self.old.pk).produced - before, 21)    # only the 3 minutes on its recipe
        self.assertEqual(sh.rx["held"], 28)
        self.assertEqual(engine.recipe_cut(sh)[0], self.t + timedelta(minutes=2))
        # End order, then the next MO: it gets the held filters and starts at the change
        op = engine.Ctx(user=Person.objects.get(pin="9999"), line="Barbara")
        engine.execute("coOpen", {}, op)
        r = engine.execute("coEnd", {"checks": [True] * 20, "qty": sh.mo_count}, op)
        self.assertTrue(r["ok"], r)
        from hmi.models import MoRun
        self.assertEqual(MoRun.objects.filter(shift=sh, mo=self.old.number).latest("id").end_time, self.t + timedelta(minutes=2))
        self.assertEqual(ShiftRun.objects.get(pk=sh.pk).co["pending"], 28)

    def test_a_glance_at_another_recipe_doesnt_count(self):
        before = MO.objects.get(pk=self.old.pk).produced
        self.minutes(["MG4062HP", "MG4062HP", "AQ1234", "MG4062HP"])
        sh = ShiftRun.objects.get(pk=self.sh.pk)
        self.assertFalse(sh.rx.get("off"))
        self.assertEqual(MO.objects.get(pk=self.old.pk).produced - before, 28)
