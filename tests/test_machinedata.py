"""Machine readings from a database table (pleats per minute, filters cut, recipe), and the second place for the
run signal (High/Low) that uptime and downtime follow."""
import json
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test import Client, TestCase

from hmi import engine, integrations, machinedata, ticker
from hmi.models import Downtime, LineState, PpmReading, ShiftRun
from plant.demo import seed_demo
from plant.models import Machine, Person
from plant.util import now

from .test_suite import no_planned_time


class SignalTests(TestCase):
    def test_high_and_low(self):
        for v in (1, "1", True, "HIGH", "On", "run", "RUNNING", 2.5, b"\x01"):
            self.assertIs(machinedata.signal(v), True, v)
        for v in (0, "0", False, "low", "OFF", "stop", "Stopped", 0.0, b"\x00", "-1"):
            self.assertIs(machinedata.signal(v), False, v)
        for v in (None, "", "maybe", float("nan")):
            self.assertIsNone(machinedata.signal(v), v)
        self.assertIs(machinedata.is_running("0", "low"), True)   # active-low signal
        self.assertIs(machinedata.is_running("1", "low"), False)


def fake_table(test):
    """A machine log table in the test database, read through the same code as the real machine database."""
    with connection.cursor() as c:
        c.execute("CREATE TABLE plc_log (id INTEGER PRIMARY KEY, stamp TEXT, line TEXT, speed REAL, cut INTEGER, rcp TEXT, motor TEXT)")
        c.execute("CREATE TABLE plc_run (id INTEGER PRIMARY KEY, line TEXT, run_bit INTEGER)")
    test.addCleanup(lambda: connection.cursor().execute("DROP TABLE IF EXISTS plc_log"))
    test.addCleanup(lambda: connection.cursor().execute("DROP TABLE IF EXISTS plc_run"))

    def cols(db, table):
        with connection.cursor() as c:
            c.execute(f"PRAGMA table_info({table})")
            names = [r[1] for r in c.fetchall()]
        if not names:
            raise integrations.ConnError(f'There is no table "{table}"')
        return names
    for p in (mock.patch.object(machinedata, "columns", side_effect=cols),
              mock.patch.object(integrations, "_db", return_value=connection)):
        p.start()
        test.addCleanup(p.stop)


def log_row(i, line, speed, cut, rcp="MG-24", motor="ON"):
    with connection.cursor() as c:
        c.execute("INSERT INTO plc_log (id, stamp, line, speed, cut, rcp, motor) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                  [i, f"2026-11-02 10:{i:02d}:00", line, speed, cut, rcp, motor])


class TableFeedTests(TestCase):
    def setUp(self):
        no_planned_time(self)
        cache.clear()
        seed_demo(reset=True)
        fake_table(self)
        self.bar = Machine.objects.get(name="Barbara")
        self.bar.feed_type = "db"
        self.bar.feed_src = {"db": "machines", "table": "plc_log", "orderCol": "stamp", "keyCol": "line", "keyVal": "BARB", "maxAge": 3}
        self.bar.feed_ppm_field, self.bar.feed_count_field, self.bar.feed_recipe_field = "speed", "cut", "rcp"
        self.bar.save()

    def test_newest_row_for_this_machine(self):
        log_row(1, "BARB", 150, 1000)
        log_row(2, "SAL", 999, 5, "OTHER")
        log_row(3, "BARB", 182, 1012, "MG-36")
        out, state = machinedata.fetch_feed(self.bar, {}, 0)
        self.assertEqual(out, {"ppm": 182.0, "count": 1012, "recipe": "MG-36"})
        self.assertEqual(state["row"], "2026-11-02 10:03:00")

    def test_a_table_with_no_new_rows_counts_as_no_data(self):
        log_row(1, "BARB", 150, 1000)
        _, st = machinedata.fetch_feed(self.bar, {}, 0)
        _, st = machinedata.fetch_feed(self.bar, st, 2 * 60000)       # same row for 2 min: still fine
        with self.assertRaises(machinedata.FeedError) as e:
            machinedata.fetch_feed(self.bar, st, 4 * 60000)            # 4 min: no data
        self.assertIn("No new row for 4 min", str(e.exception))
        log_row(2, "BARB", 151, 1003)
        out, _ = machinedata.fetch_feed(self.bar, e.exception.state, 5 * 60000)
        self.assertEqual(out["ppm"], 151.0)

    def test_names_must_be_the_tables_own(self):
        log_row(1, "BARB", 150, 1000)
        self.bar.feed_ppm_field = "speed`; DROP TABLE plc_log; --"
        with self.assertRaises(integrations.ConnError) as e:
            machinedata.fetch_feed(self.bar, {}, 0)
        self.assertIn("has no column", str(e.exception))
        self.bar.feed_src = {**self.bar.feed_src, "table": "nope"}
        with self.assertRaises(integrations.ConnError):
            machinedata.fetch_feed(self.bar, {}, 0)
        with connection.cursor() as c:
            c.execute("SELECT COUNT(*) FROM plc_log")
            self.assertEqual(c.fetchone()[0], 1)

    def test_ticker_counts_filters_and_keeps_the_recipe(self):
        sh = ShiftRun.objects.get(machine=self.bar, active=True)
        LineState.objects.filter(machine=self.bar).update(feed={})
        t = now().replace(second=0, microsecond=0)
        log_row(1, "BARB", 180, 5000)
        s, feed = ticker.sample_for(self.bar, LineState.objects.get(machine=self.bar), t)
        engine.tick(self.bar, s, t)
        log_row(2, "BARB", 180, 5009, "MG-36")
        ls = LineState.objects.get(machine=self.bar)
        ls.feed = feed
        s, feed = ticker.sample_for(self.bar, ls, t + timedelta(minutes=1))
        self.assertTrue(feed["ok"])
        self.assertEqual(feed["recipe"], "MG-36")
        before = ShiftRun.objects.get(pk=sh.pk).count
        engine.tick(self.bar, s, t + timedelta(minutes=1))
        self.assertEqual(ShiftRun.objects.get(pk=sh.pk).count - before, 9)  # the counter went up by 9 filters
        r = PpmReading.objects.filter(shift=sh).latest("t")
        self.assertEqual((r.ppm, r.units, r.recipe, r.run), (180, 9, "MG-36", True))


class RunSignalTests(TestCase):
    def setUp(self):
        no_planned_time(self)
        cache.clear()
        seed_demo(reset=True)
        fake_table(self)
        self.bar = Machine.objects.get(name="Barbara")
        self.sh = ShiftRun.objects.get(machine=self.bar, active=True)
        PpmReading.objects.filter(shift=self.sh).delete()
        Downtime.objects.filter(shift=self.sh).delete()

    def test_the_run_signal_decides_downtime_not_the_speed(self):
        t = now().replace(second=0, microsecond=0)
        # machine says it is stopped although the speed sensor still shows motion (coasting), then running at low speed
        for i in range(4):
            engine.tick(self.bar, {"ppm": 120, "run": False}, t + timedelta(minutes=i))
        for i in range(4, 6):
            engine.tick(self.bar, {"ppm": 3, "run": True}, t + timedelta(minutes=i))
        st = engine.shift_stats(ShiftRun.objects.get(pk=self.sh.pk))
        self.assertEqual((st["mins"], st["run"], st["down"]), (6, 2, 4))
        self.assertEqual(Downtime.objects.filter(shift=self.sh).count(), 1)
        self.assertEqual(list(PpmReading.objects.filter(shift=self.sh).order_by("t").values_list("run", flat=True)),
                         [False] * 4 + [True] * 2)

    def test_without_a_signal_the_speed_still_decides(self):
        t = now().replace(second=0, microsecond=0)
        engine.tick(self.bar, {"ppm": 0}, t)
        engine.tick(self.bar, {"ppm": 150}, t + timedelta(minutes=1))
        self.assertEqual(list(PpmReading.objects.filter(shift=self.sh).order_by("t").values_list("run", flat=True)), [False, True])

    def test_signal_from_a_separate_table(self):
        self.bar.feed_type = "demo"
        self.bar.uptime_src = {"type": "db", "db": "machines", "table": "plc_run", "orderCol": "id", "keyCol": "line", "keyVal": "BARB",
                               "field": "run_bit", "runWhen": "low"}
        self.bar.save()
        with connection.cursor() as c:
            c.execute("INSERT INTO plc_run (id, line, run_bit) VALUES (1, 'BARB', 1), (2, 'SAL', 0)")
        ls = LineState.objects.get(machine=self.bar)
        s, feed = ticker.sample_for(self.bar, ls, now())
        self.assertIs(s["run"], False)          # active-low: 1 = stopped
        self.assertIs(feed["run"], False)
        with connection.cursor() as c:
            c.execute("INSERT INTO plc_run (id, line, run_bit) VALUES (3, 'BARB', 0)")
        s, feed = ticker.sample_for(self.bar, ls, now())
        self.assertIs(s["run"], True)
        # the signal table goes away: the minute still counts, using the speed
        self.bar.uptime_src = {**self.bar.uptime_src, "table": "gone"}
        s, feed = ticker.sample_for(self.bar, ls, now())
        self.assertNotIn("run", s)
        self.assertIn("gone", feed["runErr"])

    def test_signal_from_a_field_of_the_reading(self):
        self.bar.feed_type = "push"
        self.bar.feed_recipe_field = "recipe"
        self.bar.uptime_src = {"type": "feed", "field": "state.run"}
        self.bar.save()
        ls = LineState.objects.get(machine=self.bar)
        ls.pushed, ls.pushed_at = {"ppm": 0, "recipe": "AQ1", "state": {"run": "RUN"}}, now()
        s, feed = ticker.sample_for(self.bar, ls, now())
        self.assertEqual((s["ppm"], s["recipe"], s["run"]), (0, "AQ1", True))


class AdminTests(TestCase):
    def setUp(self):
        cache.clear()
        seed_demo(reset=True)
        self.c = Client()
        lead = Person.objects.get(pin="9999")
        r = self.c.post("/api/hmi/login", json.dumps({"pin": lead.pin}), content_type="application/json")
        self.assertTrue(r.json()["ok"], r.json())

    def cmd(self, name, p):
        return self.c.post("/api/hmi/cmd", json.dumps({"name": name, "p": p, "line": "Barbara"}), content_type="application/json").json()

    def test_set_up_a_table_and_a_run_signal(self):
        for f, v in (("feed.type", "db"), ("feed.src.table", "plc_log"), ("feed.src.orderCol", "stamp"), ("feed.src.keyCol", "line"),
                     ("feed.src.keyVal", "BARB"), ("feed.ppmField", "speed"), ("feed.countField", "cut"), ("feed.recipeField", "rcp"),
                     ("uptime.type", "db"), ("uptime.table", "plc_run"), ("uptime.orderCol", "id"), ("uptime.field", "run_bit"),
                     ("uptime.runWhen", "low")):
            r = self.cmd("lineSet", {"id": "Barbara", "f": f, "v": v})
            self.assertTrue(r["ok"], (f, r))
        m = Machine.objects.get(name="Barbara")
        self.assertEqual(m.feed_src, {"db": "machines", "maxAge": 3, "table": "plc_log", "orderCol": "stamp", "keyCol": "line", "keyVal": "BARB"})
        self.assertEqual(m.uptime_src, {"type": "db", "db": "machines", "table": "plc_run", "orderCol": "id", "keyCol": "", "field": "run_bit",
                                        "runWhen": "low"})
        self.assertEqual(m.feed_recipe_field, "rcp")
        # picking another table clears the columns picked for the old one
        self.cmd("lineSet", {"id": "Barbara", "f": "uptime.table", "v": "other"})
        self.assertEqual(Machine.objects.get(name="Barbara").uptime_src["field"], "")
        for f, v in (("feed.src.db", "s2k"), ("uptime.runWhen", "sideways"), ("uptime.type", "x"), ("feed.src.bogus", 1)):
            self.assertFalse(self.cmd("lineSet", {"id": "Barbara", "f": f, "v": v})["ok"], f)
        line = next(x for x in self.c.get("/api/hmi/state").json()["snap"]["lines"] if x["id"] == "Barbara")
        self.assertEqual((line["feed"]["type"], line["feed"]["recipeField"], line["feed"]["src"]["table"]), ("db", "rcp", "plc_log"))

    def test_tables_need_the_database_set_up(self):
        r = self.cmd("machineTables", {"db": "machines"})
        self.assertFalse(r["ok"])
        self.assertIn("not set up", r["error"])
        self.assertFalse(self.cmd("machineTables", {"db": "s2k"})["ok"])
