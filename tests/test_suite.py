"""End-to-end checks of the plant rules: shifts, counting, downtime, changeovers and the scheduler feeding the kiosk."""
import json
from datetime import timedelta

from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase

from hmi import engine
from hmi.models import Downtime, ShiftRun
from plant.demo import seed_demo
from plant.models import MO, Machine, Person
from plant.util import ms, now
from scheduler.engine import compute, load_world, refresh_floor_queue
from scheduler.models import Job


def no_planned_time(test):
    """These checks are about counting and stops, not breaks: run them as if the shift had no planned breaks,
    whatever time of day the tests run (planned time has its own tests in test_platform.py)."""
    p = mock.patch("hmi.engine.planned_windows", return_value=[])
    p.start()
    test.addCleanup(p.stop)
    engine._planned_cache.clear()


class SuiteTests(TestCase):
    def setUp(self):
        no_planned_time(self)
        cache.clear()
        seed_demo(reset=True)
        self.c = Client()
        self.bar = Machine.objects.get(name="Barbara")
        self.sal = Machine.objects.get(name="Salvatore")
        self.lead = Person.objects.get(pin="9999")

    def cmd(self, name, p, line="Salvatore"):
        r = self.c.post("/api/hmi/cmd", json.dumps({"name": name, "p": p, "line": line}), content_type="application/json")
        return r.json()

    def login(self, pin):
        return self.c.post("/api/hmi/login", json.dumps({"pin": pin}), content_type="application/json").json()

    def test_state_and_demo_shift(self):
        j = self.c.get("/api/hmi/state").json()
        self.assertTrue(j["ok"])
        run = j["snap"]["runs"]["Barbara"]["shift"]
        self.assertTrue(run["active"])
        self.assertGreater(run["count"], 0)
        self.assertTrue(Downtime.objects.filter(line="Barbara").exists())

    def test_login_rules(self):
        self.assertFalse(self.login("0000")["ok"])
        r = self.login("1111")
        self.assertEqual(r["me"]["name"], "J. Ortiz")
        r = self.cmd("ruleSet", {"k": "minDown", "v": 3})
        self.assertTrue(r.get("admin"))

    def test_untrained_operator_cannot_start(self):
        self.login("1111")  # trained on Barbara only
        r = self.cmd("startShift", {"line": "Salvatore", "safety": [True] * 5})
        self.assertFalse(r["ok"])
        self.assertIn("not trained", r["error"])

    def test_shift_counts_and_detects_stops(self):
        self.login("2222")
        r = self.cmd("startShift", {"line": "Salvatore", "mo": "306855", "safety": [True] * 5})
        self.assertTrue(r["ok"], r)
        t = now()
        for i in range(10):
            engine.tick(self.sal, {"ppm": 180}, t + timedelta(minutes=i))
        for i in range(10, 15):
            engine.tick(self.sal, {"ppm": 0}, t + timedelta(minutes=i))
        engine.tick(self.sal, {"ppm": 180}, t + timedelta(minutes=15))
        sh = ShiftRun.objects.get(machine=self.sal, active=True)
        self.assertEqual(sh.count, 180 * 11 // 24)  # 24 pleats per MG3686
        self.assertEqual(Downtime.objects.filter(shift=sh).count(), 1)
        self.assertEqual(MO.objects.get(number="306855").produced, sh.count)

    def test_changeover_flow(self):
        self.login("1111")
        self.assertTrue(self.cmd("coOpen", {}, "Barbara")["ok"])
        self.assertTrue(self.cmd("coEnd", {"qty": 100, "scrap": 2}, "Barbara")["ok"])
        r = self.cmd("coPick", {"mo": "306785"}, "Barbara")
        self.assertTrue(r["ok"], r)
        self.assertFalse(self.cmd("coFinish", {}, "Barbara")["ok"])  # steps not done
        for i in range(8):
            self.cmd("coStep", {"i": i, "checked": True}, "Barbara")
        r = self.cmd("coFinish", {}, "Barbara")
        self.assertTrue(r["ok"], r)
        self.assertEqual(ShiftRun.objects.get(machine=self.bar, active=True).mo.number, "306785")

    def test_scheduler_drives_kiosk_queue(self):
        self.login("9999")
        mo = MO.objects.get(number="308627")
        self.assertIsNone(mo.line)
        r = self.c.post("/api/scheduler/job", json.dumps({"moId": mo.id, "op": "s1", "machineId": self.sal.id, "reqStart": ms(now())}),
                        content_type="application/json").json()
        self.assertTrue(r["ok"], r)
        mo.refresh_from_db()
        self.assertEqual(mo.line, self.sal)
        self.assertIsNotNone(mo.plan_start)
        # downstream stage needs stage 1 first
        mo2 = MO.objects.get(number="309099")
        r = self.c.post("/api/scheduler/job", json.dumps({"moId": mo2.id, "op": "eb", "machineId": Machine.objects.get(name="William").id,
                                                          "reqStart": ms(now())}), content_type="application/json").json()
        self.assertFalse(r["ok"])

    def test_downstream_never_outruns_stage1(self):
        world = load_world()
        sch = compute(world)
        run = MO.objects.get(number="309215")
        s1, eb = sch["res"][f"{run.id}__s1"], sch["res"][f"{run.id}__eb"]
        self.assertGreaterEqual(eb["end"], s1["end"] - 60_000)

    def test_unplanned_mo_leaves_queue_but_running_stays(self):  # noqa: D102
        Job.objects.filter(mo__number="306856").delete()
        Job.objects.filter(mo__number="309215").delete()  # running on Barbara
        refresh_floor_queue()
        self.assertIsNone(MO.objects.get(number="306856").line)
        self.assertEqual(MO.objects.get(number="309215").line, self.bar)

    def test_pages_render(self):
        for url in ("/operator/", "/scheduler/", "/data/"):
            self.assertEqual(self.c.get(url).status_code, 200)
        self.assertTrue(self.c.get("/api/data/state").json()["ok"])


class RegressionTests(TestCase):
    """One test per defect found in review, so none can come back unnoticed."""

    def setUp(self):
        no_planned_time(self)
        cache.clear()
        seed_demo(reset=True)
        self.c = Client()
        self.bar = Machine.objects.get(name="Barbara")
        self.sal = Machine.objects.get(name="Salvatore")

    def post(self, url, data):
        return self.c.post(url, json.dumps(data), content_type="application/json")

    def login(self, pin):
        return self.post("/api/hmi/login", {"pin": pin}).json()

    def cmd(self, name, p, line="Barbara"):
        return self.post("/api/hmi/cmd", {"name": name, "p": p, "line": line}).json()

    # --- ticker and machine data
    def test_bad_pushed_reading_is_rejected(self):
        from django.test import override_settings
        from django.conf import settings
        suite = dict(settings.SUITE, FEED_KEY="k")
        with override_settings(SUITE=suite):
            for bad in ({"ppm": "abc"}, {"ppm": None}, {"ppm": "nan"}, {"ppm": 5, "count": "x"}, []):
                r = self.c.post("/api/feed/Barbara", json.dumps(bad), content_type="application/json", HTTP_X_FEED_KEY="k")
                self.assertEqual(r.status_code, 400, bad)
            r = self.c.post("/api/feed/Barbara", json.dumps({"ppm": 190, "count": 10}), content_type="application/json", HTTP_X_FEED_KEY="k")
            self.assertEqual(r.status_code, 200)

    def test_one_bad_machine_does_not_stop_the_others(self):
        from hmi.models import LineState
        from hmi.ticker import tick_machines
        from unittest import mock
        LineState.objects.update(next_tick=now() - timedelta(minutes=2))
        real = engine.tick

        def boom(m, s, t):
            if m.name == "Barbara":
                raise RuntimeError("bad data")
            return real(m, s, t)
        with mock.patch("hmi.engine.tick", side_effect=boom):
            tick_machines(now())
        sal = LineState.objects.get(machine=self.sal)
        bar = LineState.objects.get(machine=self.bar)
        self.assertGreater(sal.next_tick, now())          # Salvatore caught up
        self.assertFalse(bar.feed["ok"])                    # Barbara shows the problem on its screen
        self.assertGreater(bar.next_tick, now() - timedelta(minutes=2))  # and does not retry the same bad minute forever

    def test_tick_ignores_text_and_nan(self):
        sh = ShiftRun.objects.get(machine=self.bar, active=True)
        before = sh.count
        for bad in ({"ppm": "x"}, {"ppm": float("nan")}, {"ppm": float("inf")}, None):
            engine.tick(self.bar, bad, now())
        sh.refresh_from_db()
        self.assertEqual(sh.count, before)

    def test_only_one_ticker_runs(self):
        from hmi.ticker import claim_lease
        t = now()
        self.assertTrue(claim_lease("a", t))
        self.assertFalse(claim_lease("b", t + timedelta(seconds=5)))
        self.assertTrue(claim_lease("a", t + timedelta(seconds=10)))
        self.assertTrue(claim_lease("b", t + timedelta(seconds=60)))  # "a" went quiet, "b" takes over

    def test_removing_a_machine_keeps_its_history(self):
        from hmi.models import PpmReading
        self.login("9999")
        ShiftRun.objects.filter(machine=self.bar).update(active=False)
        n = PpmReading.objects.filter(line="Barbara").count()
        self.assertTrue(self.cmd("lineDel", {"id": "Barbara"})["ok"])
        self.assertEqual(PpmReading.objects.filter(line="Barbara").count(), n)
        self.assertTrue(ShiftRun.objects.filter(line="Barbara").exists())

    # --- scheduling
    def test_closed_mo_frees_stage1_but_keeps_later_stages(self):
        run = MO.objects.get(number="309215")
        run.status = "CLOSE"
        run.save()
        ShiftRun.objects.filter(machine=self.bar).update(mo=None)
        res = compute(load_world())["res"]
        self.assertNotIn(f"{run.id}__s1", res)
        self.assertIn(f"{run.id}__eb", res)            # End Band still planned
        self.assertFalse(res[f"{run.id}__eb"]["waiting"])

    def test_running_mo_is_planned_from_now_with_what_is_left(self):
        run = MO.objects.get(number="309215")
        r = compute(load_world())["res"][f"{run.id}__s1"]
        self.assertEqual(r["qty"], run.qty - run.produced)
        self.assertGreaterEqual(r["start"], ms(now()) - 60_000)
        self.assertEqual(r["coMin"], 0)

    def test_planner_cannot_move_running_mo_off_its_machine(self):
        self.login("9999")
        run = MO.objects.get(number="309215")
        r = self.post("/api/scheduler/job", {"moId": run.id, "op": "s1", "machineId": self.sal.id, "reqStart": ms(now())}).json()
        self.assertFalse(r["ok"])
        Job.objects.filter(mo=run, op="s1").update(machine=self.sal)   # even if the board says so
        refresh_floor_queue()
        self.assertEqual(MO.objects.get(pk=run.pk).line, self.bar)

    def test_scheduler_changes_need_a_shift_lead(self):
        mo = MO.objects.get(number="308627")
        r = self.post("/api/scheduler/mo/delete", {"id": mo.id})
        self.assertEqual(r.status_code, 403)
        self.assertTrue(MO.objects.filter(pk=mo.pk).exists())
        self.login("1111")  # an operator is not enough
        self.assertEqual(self.post("/api/scheduler/mo/delete", {"id": mo.id}).status_code, 403)
        self.login("9999")
        self.assertTrue(self.post("/api/scheduler/mo/delete", {"id": mo.id}).json()["ok"])

    def test_running_or_started_mo_cannot_be_deleted(self):
        self.login("9999")
        run = MO.objects.get(number="309215")
        self.assertFalse(self.post("/api/scheduler/mo/delete", {"id": run.id}).json()["ok"])
        self.assertTrue(MO.objects.filter(pk=run.pk).exists())

    def test_changing_cost_sheet_match_removes_dropped_stage_job(self):
        self.login("9999")
        mo = MO.objects.get(number="309099")  # needs End Band
        self.post("/api/scheduler/job", {"moId": mo.id, "op": "s1", "machineId": self.bar.id, "reqStart": ms(now())})
        self.post("/api/scheduler/job", {"moId": mo.id, "op": "eb", "machineId": Machine.objects.get(name="William").id, "reqStart": ms(now())})
        self.assertTrue(Job.objects.filter(mo=mo, op="eb").exists())
        r = self.post("/api/scheduler/mo", {"id": mo.id, "ops": {"s1": True, "eb": False}}).json()
        self.assertTrue(r["ok"])
        self.assertFalse(Job.objects.filter(mo=mo, op="eb").exists())

    def test_lines_save_rejects_bad_input_and_changes_nothing(self):
        self.login("9999")
        n = Machine.objects.count()
        st = self.c.get("/api/scheduler/state").json()
        cfg = {**st["cfg"], "cfgSig": st["cfgSig"]}
        for bad in ({**cfg, "machines": []},
                    {**cfg, "shifts": [{**cfg["shifts"][0], "start": "25:99"}] + cfg["shifts"][1:]},
                    {**cfg, "machines": [{**cfg["machines"][0], "scrap": "lots"}] + cfg["machines"][1:]},
                    {**cfg, "machines": [{**cfg["machines"][0], "ppm": float("inf")}] + cfg["machines"][1:]} if False else {**cfg, "machines": [{**cfg["machines"][0], "ppm": 1e9}] + cfg["machines"][1:]},
                    {**cfg, "machines": cfg["machines"] + [{**cfg["machines"][0], "id": "new-x"}]}):  # duplicate name
            r = self.post("/api/scheduler/config", bad)
            self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(Machine.objects.count(), n)
        self.assertTrue(self.post("/api/scheduler/config", cfg).json()["ok"])  # the unchanged plan still saves

    def test_bad_requests_get_a_message_not_a_crash(self):
        self.login("9999")
        for url, data in (("/api/scheduler/job", {"moId": "x"}), ("/api/scheduler/preview", {"op": "s1"}),
                          ("/api/scheduler/mo", {"id": 1, "due": "not a date"}), ("/api/hmi/login", {"pin": "1", "userId": "abc"})):
            r = self.post(url, data)
            self.assertIn(r.status_code, (400, 403), url)
            self.assertFalse(r.json()["ok"])
        r = self.cmd("listDel", {"k": "reasons", "i": 999})
        self.assertFalse(r["ok"])
        self.assertIn("error", r)

    # --- operator inputs
    def test_qc_rejects_nonsense_measurements(self):
        self.login("1111")
        for bad in ("nan", "inf", "", "abc", "-1"):
            r = self.cmd("qcSave", {"len": bad, "wid": 8.8, "ph": 1.2, "sbl": 1.2, "sbr": 1.2})
            self.assertFalse(r["ok"], bad)

    def test_changeover_rejects_bad_counts(self):
        self.login("1111")
        self.cmd("coOpen", {})
        for bad in ("abc", "nan", -5):
            self.assertFalse(self.cmd("coEnd", {"qty": bad})["ok"], bad)
        self.assertTrue(self.cmd("coEnd", {"qty": 100})["ok"])

    def test_s2k_import_skips_bad_rows_and_keeps_good_ones(self):
        ctx = engine.Ctx()
        n = engine.import_mos(ctx, [{"mo": "900001", "item": "MG3012", "qty": 100, "due": "2026-13-45"},
                                    {"mo": "900002", "item": "MG3012", "qty": "lots"},
                                    {"mo": "", "item": "MG3012", "qty": 5}, "garbage",
                                    {"mo": "900003", "item": "MG3012", "qty": 50, "due": "2026-11-20"}])
        self.assertEqual(n, 2)
        self.assertIsNone(MO.objects.get(number="900001").due)

    def test_pin_guessing_is_slowed_down(self):
        for _ in range(5):
            self.assertFalse(self.login("0000")["ok"])
        r = self.login("9999")  # right PIN, but still locked out for a minute
        self.assertFalse(r["ok"])
        self.assertIn("Too many", r["error"])

    def test_people_list_never_creates_duplicate_pins(self):
        engine.apply_people(engine.Ctx(), [{"id": "x1", "name": "New Person", "pin": "9999"}])
        self.assertEqual(Person.objects.filter(pin="9999").count(), 1)


class ReviewerFindingTests(TestCase):
    """Defects reported by the independent review."""

    def setUp(self):
        no_planned_time(self)
        cache.clear()
        seed_demo(reset=True)
        self.c = Client()
        self.bar = Machine.objects.get(name="Barbara")
        self.sh = ShiftRun.objects.get(machine=self.bar, active=True)
        Downtime.objects.all().delete()
        ShiftRun.objects.filter(pk=self.sh.pk).update(stop_start=None, state="running")
        self.sh.refresh_from_db()

    def post(self, url, data):
        return self.c.post(url, json.dumps(data), content_type="application/json")

    def lead(self):
        self.post("/api/hmi/login", {"pin": "9999"})

    def test_dead_feed_is_not_downtime(self):
        t = now().replace(second=0, microsecond=0)
        engine.tick(self.bar, {"ppm": 190}, t)
        for i in range(1, 4):
            engine.tick(self.bar, {"ppm": 0}, t + timedelta(minutes=i))       # 3 stopped minutes
        for i in range(4, 63):
            engine.tick(self.bar, None, t + timedelta(minutes=i))             # feed offline ~1 hour
        engine.tick(self.bar, {"ppm": 190}, t + timedelta(minutes=63))
        rows = list(Downtime.objects.filter(shift=self.sh))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].minutes, 3)

    def test_min_downtime_rule_is_exact(self):
        t = now().replace(second=0, microsecond=0)
        engine.tick(self.bar, {"ppm": 190}, t)
        engine.tick(self.bar, {"ppm": 0}, t + timedelta(minutes=1))           # one stopped minute, minDown=2
        engine.tick(self.bar, {"ppm": 190}, t + timedelta(minutes=2))
        self.assertEqual(Downtime.objects.filter(shift=self.sh).count(), 0)
        for i in range(3, 5):
            engine.tick(self.bar, {"ppm": 0}, t + timedelta(minutes=i))       # two stopped minutes
        engine.tick(self.bar, {"ppm": 190}, t + timedelta(minutes=5))
        self.assertEqual([d.minutes for d in Downtime.objects.filter(shift=self.sh)], [2])

    def test_ending_shift_during_changeover_records_it(self):
        self.post("/api/hmi/login", {"pin": "1111"})
        self.post("/api/hmi/cmd", {"name": "coOpen", "p": {}, "line": "Barbara"})
        self.post("/api/hmi/cmd", {"name": "coEnd", "p": {"qty": 10}, "line": "Barbara"})
        ShiftRun.objects.filter(pk=self.sh.pk).update(co={**ShiftRun.objects.get(pk=self.sh.pk).co, "start": ms(now() - timedelta(minutes=40))})
        r = self.post("/api/hmi/cmd", {"name": "endShift", "p": {"issues": "glue pot slow"}, "line": "Barbara"}).json()
        self.assertTrue(r["ok"], r)
        d = Downtime.objects.get(shift=self.sh, reason="Changeover")
        self.assertGreaterEqual(d.minutes, 39)
        self.sh.refresh_from_db()
        self.assertEqual(self.sh.end_issues, "glue pot slow")

    def test_zero_length_job_stays_on_board_and_queue(self):
        from plant.models import PlantSettings
        st = PlantSettings.load(); st.planner = {"coFirst": False, "coSamePart": False}; st.save()
        sal = Machine.objects.get(name="Salvatore")
        Job.objects.filter(machine=sal).delete()
        mo = MO.objects.create(number="ZERO1", part="NOT-ON-SHEET", qty=10, ops={"s1": True})
        Job.objects.create(mo=mo, op="s1", machine=sal, req_start=now())
        refresh_floor_queue()
        res = compute(load_world())["res"]
        self.assertIn(f"{mo.id}__s1", res)
        self.assertTrue(res[f"{mo.id}__s1"]["warn"])
        self.assertEqual(MO.objects.get(pk=mo.pk).line, sal)

    def test_empty_paycom_answer_changes_nobody(self):
        Person.objects.filter(pin="1111").update(source="paycom", eecode="E1")
        for bad in ([], None, {"oops": 1}):
            with self.assertRaises(engine.Need):
                engine.apply_paycom(engine.Ctx(), bad)
        self.assertTrue(Person.objects.get(pin="1111").active)

    def test_rename_from_scheduler_carries_kiosk_records(self):
        self.lead()
        st = self.c.get("/api/scheduler/state").json()
        cfg = {**st["cfg"], "cfgSig": st["cfgSig"]}
        for m in cfg["machines"]:
            if m["name"] == "Barbara":
                m["name"] = "Barb 2"
        r = self.post("/api/scheduler/config", cfg).json()
        self.assertTrue(r["ok"], r)
        self.assertEqual(ShiftRun.objects.get(pk=self.sh.pk).line, "Barb 2")
        from hmi.models import PpmReading
        self.assertFalse(PpmReading.objects.filter(line="Barbara").exists())

    def test_stale_lines_save_keeps_machines_added_elsewhere(self):
        self.lead()
        st = self.c.get("/api/scheduler/state").json()                              # planner's screen
        self.post("/api/hmi/cmd", {"name": "lineAdd", "p": {}, "line": "Barbara"})  # kiosk adds a machine meanwhile
        n = Machine.objects.count()
        r = self.post("/api/scheduler/config", {**st["cfg"], "cfgSig": st["cfgSig"]})  # planner saves the old list
        self.assertEqual(r.status_code, 409)                                         # refused, nothing lost
        self.assertEqual(Machine.objects.count(), n)

    def test_removed_line_is_deleted_only_when_asked(self):
        self.lead()
        st = self.c.get("/api/scheduler/state").json()
        cfg = {**st["cfg"], "cfgSig": st["cfgSig"]}
        gb = next(m for m in cfg["machines"] if m["name"] == "Glue Bead")
        cfg["machines"] = [m for m in cfg["machines"] if m is not gb]
        self.assertTrue(self.post("/api/scheduler/config", {**cfg, "removedMachines": [gb["id"]]}).json()["ok"])
        self.assertFalse(Machine.objects.filter(name="Glue Bead").exists())

    def test_import_does_not_touch_closed_orders(self):
        self.lead()
        MO.objects.filter(number="308147").update(status="CLOSE", qty=1260)
        imp = {"header": ["MO", "Part", "Qty"], "rows": [["308147", "MG4017", 5], ["NEW9", "MG3012", 7]],
               "map": {"mo": 0, "part": 1, "qty": 2, "due": -1, "pleats": -1, "eb": -1, "ac": -1, "gb": -1}}
        r = self.post("/api/scheduler/import/commit", imp).json()
        self.assertEqual((r["added"], r["skipped"]), (1, 1))
        self.assertEqual(MO.objects.get(number="308147").qty, 1260)

    def test_pin_match_is_exact(self):
        Person.objects.filter(pin="1111").update(pin="ab12")
        self.assertFalse(self.post("/api/hmi/login", {"pin": "AB12"}).json()["ok"])
        self.assertTrue(self.post("/api/hmi/login", {"pin": "ab12"}).json()["ok"])

    def test_long_text_is_fit_not_fatal(self):
        from hmi.models import Ticket
        Ticket.objects.create(t=now(), by="x", machine="Barbara", type="t" * 500, notes="")
        self.assertEqual(len(Ticket.objects.latest("id").type), 80)

    def test_shift_with_no_days_is_not_worked(self):
        from plant.models import Shift
        from plant.util import shift_windows, sod_ms
        Shift.objects.update(days=[])
        self.assertEqual(shift_windows(sod_ms(ms(now()))), [])

    def test_shift_lead_approval_login(self):
        r = self.post("/api/hmi/login", {"pin": "1111", "adminOnly": True}).json()
        self.assertFalse(r["ok"])
        self.assertIn("can't do this", r["error"])
        r = self.post("/api/hmi/login", {"pin": "9999", "adminOnly": True}).json()
        self.assertTrue(r["ok"], r)
        lead = Person.objects.get(pin="9999")
        r = self.post("/api/hmi/login", {"pin": "9999", "userId": lead.id, "adminOnly": True}).json()
        self.assertTrue(r["ok"], r)


class SecondReviewTests(TestCase):
    """Issues from the independent re-check of the fixes."""

    def setUp(self):
        no_planned_time(self)
        cache.clear()
        seed_demo(reset=True)
        self.c = Client()

    def post(self, url, data, c=None):
        return (c or self.c).post(url, json.dumps(data), content_type="application/json")

    def test_long_machine_name_is_refused(self):
        self.post("/api/hmi/login", {"pin": "9999"})
        r = self.post("/api/hmi/cmd", {"name": "lineSet", "p": {"id": "Barbara", "f": "id", "v": "B" * 70}, "line": "Barbara"}).json()
        self.assertFalse(r["ok"])
        self.assertTrue(Machine.objects.filter(name="Barbara").exists())

    def test_stale_scheduler_screen_cannot_undo_kiosk_changes(self):
        self.post("/api/hmi/login", {"pin": "9999"})
        cfg = self.c.get("/api/scheduler/state").json()
        self.post("/api/hmi/cmd", {"name": "lineSet", "p": {"id": "Barbara", "f": "rated", "v": 999}, "line": "Barbara"})
        r = self.post("/api/scheduler/config", {**cfg["cfg"], "cfgSig": cfg["cfgSig"]})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(Machine.objects.get(name="Barbara").ppm, 999)
        fresh = self.c.get("/api/scheduler/state").json()
        self.assertTrue(self.post("/api/scheduler/config", {**fresh["cfg"], "cfgSig": fresh["cfgSig"]}).json()["ok"])

    def test_non_object_bodies_get_a_message(self):
        self.post("/api/hmi/login", {"pin": "9999"})
        for url in ("/api/hmi/cmd", "/api/scheduler/job", "/api/scheduler/config", "/api/hmi/login"):
            r = self.c.post(url, "[1,2,3]", content_type="application/json")
            self.assertNotEqual(r.status_code, 500, url)
            self.assertFalse(r.json()["ok"], url)

    def test_fresh_cookies_do_not_reset_the_pin_limit(self):
        for i in range(20):
            self.post("/api/hmi/login", {"pin": "0000"}, Client())   # new browser session every time
        r = self.post("/api/hmi/login", {"pin": "9999"}, Client()).json()
        self.assertFalse(r["ok"])
        self.assertIn("Too many", r["error"])
