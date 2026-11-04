"""Labor Budget, the Scheduler's use of the labor plan, machine screen addresses, connections added after
install, incoming data (machine vision QC), downtime reasons and planned time, downtime alerts, and Reports."""
import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.db import connections
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from hmi import alerts, engine, inbound
from hmi.models import AlertContact, AlertLog, AutoQC, DataSource, Downtime, InboundRecord, PlannedTime, PpmReading, ShiftRun
from labor.models import LaborPlan
from plant import liveconfig
from plant.demo import seed_demo
from plant.models import Machine, PartSpec, PlantSettings, Shift

PLAN = {
    "schedules": [{"id": "S1A", "shift": 1, "startTime": "06:00", "endTime": "14:30"}, {"id": "S2A", "shift": 2, "startTime": "14:30", "endTime": "23:00"}],
    "calendar": {"defaultWorkDays": [1, 2, 3, 4, 5], "overrides": {"2030-01-01": "HOLIDAY", "2030-01-05": "WORK"}},
    "positionsDefaults": [{"title": "Operator", "defaultWage": 20}, {"title": "Packager", "defaultWage": 15}],
    "slotAssignments": {"LB_1_Operator_0": {"name": "Ann", "wage": 22}},
    "lines": [{"id": "LB", "name": "Barbara", "activeShifts": {"1": True, "2": False},
               "shiftPositions": {"1": [{"title": "Operator", "reqCount": 1}, {"title": "Packager", "reqCount": 2}], "2": [{"title": "Operator", "reqCount": 1}]}}],
}


def csrf_client(addr="127.0.0.1"):
    return Client(REMOTE_ADDR=addr)


def post(c, path, data):
    return c.post(path, json.dumps(data), content_type="application/json")


def cmd(c, name, p, line="Barbara"):
    return post(c, "/api/hmi/cmd", {"name": name, "p": p, "line": line}).json()


def login(c, pin):
    r = post(c, "/api/hmi/login", {"pin": pin}).json()
    assert r["ok"], r
    return r


def at(y, mo, d, h=0, mi=0):
    return timezone.make_aware(datetime(y, mo, d, h, mi))


class Base(TestCase):
    def setUp(self):
        cache.clear()
        engine._planned_cache.clear()
        seed_demo(reset=True)
        self.bar = Machine.objects.get(name="Barbara")


# ------------------------------------------------------------------ Labor Budget
class LaborBudgetTests(Base):
    def test_shift_leads_only(self):
        r = Client().get("/labor/")
        self.assertContains(r, "Shift lead PIN")
        self.assertEqual(Client().get("/api/labor/state").status_code, 403)
        c = Client()
        login(c, "1111")  # an operator
        self.assertEqual(c.get("/api/labor/state").status_code, 403)
        c = Client()
        login(c, "9999")
        r = c.get("/labor/")
        self.assertContains(r, "MFG Labor &amp; Production Budget")
        self.assertIn("'unsafe-inline'", r["Content-Security-Policy"])
        self.assertIn("connect-src 'self'", r["Content-Security-Policy"])
        self.assertNotIn(b"cdn.tailwindcss.com", r.content)
        self.assertNotIn(b"googleapis", r.content)

    def test_save_conflict_history_restore(self):
        c = Client()
        login(c, "9999")
        r = post(c, "/api/labor/save", {"version": 0, "data": PLAN}).json()
        self.assertEqual(r["version"], 1)
        stale = post(c, "/api/labor/save", {"version": 0, "data": {**PLAN, "projectName": "x"}})
        self.assertEqual(stale.status_code, 409)
        self.assertTrue(stale.json()["conflict"])
        r = post(c, "/api/labor/save", {"version": 1, "data": {**PLAN, "projectName": "second"}}).json()
        self.assertEqual(r["version"], 2)
        hist = c.get("/api/labor/history").json()["items"]
        self.assertEqual([h["version"] for h in hist], [2, 1])
        self.assertTrue(post(c, "/api/labor/restore", {"id": hist[1]["id"]}).json()["ok"])
        self.assertNotIn("projectName", LaborPlan.objects.get(pk=1).data)
        self.assertEqual(post(c, "/api/labor/save", {"version": 3, "data": {"lines": "nope"}}).status_code, 400)

    def test_boot_data_is_safe_inside_the_page(self):
        c = Client()
        login(c, "9999")
        post(c, "/api/labor/save", {"version": 0, "data": {**PLAN, "projectName": "</script><script>alert(1)</script>"}})
        html = c.get("/labor/").content.decode()
        self.assertNotIn("</script><script>alert(1)", html)


class SchedulerLaborTests(Base):
    def setUp(self):
        super().setUp()
        c = Client()
        login(c, "9999")   # saved the way the Labor Budget saves it: 2nd shift turned off for Barbara there
        r = post(c, "/api/labor/save", {"version": 0, "data": json.loads(json.dumps(PLAN))}).json()
        assert r["ok"], r

    def test_calendar_and_crews(self):
        from labor.plan import LaborPlanView
        from scheduler.engine import Calendar, load_world
        w = load_world()
        self.assertTrue(w["labor"])
        m = next(x for x in w["cfg"]["machines"] if x["name"] == "Barbara")
        crews = w["laborLines"][m["id"]]["shifts"]
        s1, s2 = Shift.objects.get(name="1st shift"), Shift.objects.get(name="2nd shift")
        self.assertTrue(crews[s1.id]["staffed"])
        self.assertEqual(crews[s1.id]["crew"], 3)
        self.assertEqual(crews[s1.id]["costPerHour"], 22 + 15 + 15)  # Ann's own wage, then the default wage
        self.assertFalse(s2.id in crews and crews[s2.id]["staffed"])  # 2nd shift turned off in the plan...
        self.assertEqual(list(Machine.objects.get(name="Barbara").shifts.all()), [s1])  # ...is the machine's shifts too
        cal = Calendar(w["cfg"], w["labor"], w["laborLines"])
        from plant.util import ms
        holiday = cal.day_iv(m, ms(at(2030, 1, 1)))
        self.assertEqual(holiday, [])                                # holiday: no shifts
        normal = cal.day_iv(m, ms(at(2030, 1, 2)))                   # a Wednesday: 1st shift only
        self.assertTrue(normal)
        self.assertTrue(all(timezone.localtime(timezone.datetime.fromtimestamp(s / 1000, tz=timezone.get_current_timezone())).hour < 15 for s, e in normal))
        saturday_work = cal.day_iv(m, ms(at(2030, 1, 5)))            # extra work day in the plan
        self.assertTrue(saturday_work)
        sal = next(x for x in w["cfg"]["machines"] if x["name"] == "Salvatore")  # added to the plan from the suite: unchanged
        self.assertEqual(len(cal.day_iv(sal, ms(at(2030, 1, 2)))), len(Calendar(w["cfg"]).day_iv(sal, ms(at(2030, 1, 2)))))
        self.assertEqual(LaborPlanView(PLAN).day_status(at(2030, 1, 6).date()), ("OFF", False))

    def test_wages_only_for_shift_leads(self):
        r = Client().get("/api/scheduler/state").json()
        self.assertTrue(r["labor"]["on"])
        sh = next(iter(r["labor"]["lines"].values()))["shifts"]
        self.assertTrue(all("costPerHour" not in x for x in sh.values()))
        self.assertTrue(all("laborCost" not in x for x in r["res"].values()))
        c = Client()
        login(c, "9999")
        r = c.get("/api/scheduler/state").json()
        self.assertTrue(any("costPerHour" in x for x in next(iter(r["labor"]["lines"].values()))["shifts"].values()))

    def test_can_be_turned_off(self):
        st = PlantSettings.load()
        st.planner = {**st.planner, "useLabor": False}
        st.save()
        from scheduler.engine import load_world
        w = load_world()
        self.assertIsNone(w["labor"])
        self.assertEqual(w["laborLines"], {})


# ------------------------------------------------------------------ machine screen addresses
class StationTests(Base):
    def test_station_address(self):
        r = Client().get("/station/Barbara/")
        self.assertContains(r, '<meta name="station-lock" content="Barbara">')
        self.assertNotContains(r, 'class="suitebar"')
        self.assertContains(Client().get("/station/barbara/"), 'content="Barbara"')
        r = Client().get("/station/Nope/")
        self.assertEqual(r.status_code, 404)
        self.assertContains(r, "Barbara", status_code=404)
        self.assertContains(Client().get("/operator/"), 'class="suitebar"')


# ------------------------------------------------------------------ connections added after install
class FakeDB:
    class Error(Exception):
        pass

    class OperationalError(Error):
        pass

    def __init__(self, ok=True):
        self.ok, self.sql = ok, []

    def connect(self, **kw):
        if not self.ok:
            raise self.OperationalError(1045, "Access denied")
        self.kw = kw
        outer = self

        class Cur:
            def execute(s, q, p=None):
                outer.sql.append(q)

            def fetchone(s):
                return (4,)

            def fetchall(s):
                return [("GRANT SELECT ON s2k.* TO r",)]

        class Conn:
            def cursor(s):
                return Cur()

            def close(s):
                pass
        return Conn()


class LiveConnectionTests(Base):
    def setUp(self):
        super().setUp()
        self.dir = Path(tempfile.mkdtemp())
        self.o = override_settings(BASE_DIR=self.dir)
        self.o.enable()
        self.addCleanup(self.o.disable)
        self.addCleanup(lambda: (connections.settings.pop("s2k", None), connections.settings.pop("assets", None)))
        self.c = Client()
        login(self.c, "9999")

    def test_add_and_remove_a_read_only_database(self):
        fake = FakeDB()
        with mock.patch("plant.dbcheck.driver", return_value=fake):
            r = cmd(self.c, "connSave", {"group": "s2k", "fields": {"host": "192.168.100.30", "port": "3306", "name": "s2k", "user": "r", "password": "pw"}})
        self.assertTrue(r["ok"], r)
        self.assertEqual(fake.kw["init_command"], "SET SESSION TRANSACTION READ ONLY")
        self.assertTrue(all(q.startswith(("SELECT", "SHOW")) for q in fake.sql))
        env = (self.dir / ".env").read_text()
        self.assertIn('S2K_DB_HOST="192.168.100.30"', env)
        self.assertIn("s2k", connections.databases)
        info = cmd(self.c, "connInfo", {})["data"]
        self.assertTrue(info["s2k"]["hasPassword"])
        self.assertNotIn("pw", json.dumps(info))
        # blank password keeps the saved one
        with mock.patch("plant.dbcheck.driver", return_value=fake):
            cmd(self.c, "connSave", {"group": "s2k", "fields": {"host": "192.168.100.30", "port": "3306", "name": "s2k", "user": "r2", "password": ""}})
        self.assertEqual(fake.kw["password"], "pw")
        self.assertTrue(cmd(self.c, "connRemove", {"group": "s2k"})["ok"])
        self.assertNotIn("s2k", connections.databases)
        self.assertNotIn("S2K_DB_HOST", (self.dir / ".env").read_text())

    def test_bad_login_or_public_address_is_not_saved(self):
        with mock.patch("plant.dbcheck.driver", return_value=FakeDB(ok=False)):
            r = cmd(self.c, "connSave", {"group": "assets", "fields": {"host": "192.168.100.31", "name": "a", "user": "u", "password": "x"}})
        self.assertFalse(r["ok"])
        r = cmd(self.c, "connSave", {"group": "assets", "fields": {"host": "8.8.8.8", "name": "a", "user": "u", "password": "x"}})
        self.assertFalse(r["ok"])
        self.assertFalse((self.dir / ".env").exists())

    def test_paycom_feed_key_and_operators(self):
        self.assertTrue(cmd(self.c, "connSave", {"group": "paycom", "fields": {"sid": "abc", "token": "tok"}})["ok"])
        self.assertEqual(settings.SUITE["PAYCOM_SID"], "abc")
        r = cmd(self.c, "connSave", {"group": "feed", "fields": {}})
        self.assertEqual(settings.SUITE["FEED_KEY"], r["key"])
        op = Client()
        login(op, "1111")
        self.assertFalse(cmd(op, "connSave", {"group": "paycom", "fields": {"sid": "x", "token": "y"}})["ok"])
        cmd(self.c, "connRemove", {"group": "paycom"})
        cmd(self.c, "connRemove", {"group": "feed"})


# ------------------------------------------------------------------ incoming data
class InboundTests(Base):
    def setUp(self):
        super().setUp()
        self.c = Client()
        login(self.c, "9999")
        PartSpec.objects.update_or_create(key="MG4062HP", defaults=dict(pleats=25, len=10, wid=8, ph=1, sb=0.75, tol=0.06))

    def add(self, **f):
        r = cmd(self.c, "srcSave", {"src": {"name": "vision-barbara", "kind": "qc", "line": "Barbara", "mode": "push",
                                            "mapping": {"len": "dims.length", "wid": "dims.width", "ph": "ph", "part": "part", "time": "ts"}, **f}})
        self.assertTrue(r["ok"], r)
        return r

    def test_push_qc_measurements(self):
        r = self.add()
        key = r["key"]
        self.assertTrue(key)
        url = "/api/ingest/vision-barbara"
        self.assertEqual(Client().post(url, "{}", content_type="application/json", HTTP_X_SOURCE_KEY="wrong").status_code, 403)
        good = {"part": "MG4062HP", "dims": {"length": 10.02, "width": 7.99}, "ph": 1.0, "ts": timezone.now().isoformat()}
        bad = {"part": "MG4062HP", "dims": {"length": 10.5, "width": 8}, "ph": 1}
        r = Client().post(url, json.dumps([good, bad, "junk"]), content_type="application/json", HTTP_X_SOURCE_KEY=key).json()
        self.assertEqual((r["stored"], r["skipped"]), (2, 1))
        self.assertEqual(sorted(AutoQC.objects.values_list("passed", flat=True)), [False, True])
        self.assertEqual(DataSource.objects.get().received, 2)
        snap = self.c.get("/api/hmi/state?force=1").json()["snap"]
        self.assertEqual(snap["autoqc"]["Barbara"]["fails"], 1)
        # a key never comes back from the server
        self.assertNotIn(key, json.dumps(cmd(self.c, "srcList", {})))

    def test_mm_and_pass_field(self):
        src = DataSource.objects.create(name="v2", kind="qc", line="Barbara", mapping={"len": "L", "passed": "result", "units": "mm"})
        r = inbound.ingest(src, [{"L": 254, "result": "NG"}, {"L": 254, "result": "OK"}])
        self.assertEqual(r["stored"], 2)
        q = AutoQC.objects.order_by("id")
        self.assertAlmostEqual(q[0].len, 10.0, places=3)
        self.assertEqual([x.passed for x in q], [False, True])

    def test_readings_and_poll_reads_each_record_once(self):
        src = DataSource.objects.create(name="ovens", kind="readings", mode="poll", url="http://192.168.100.70/x", line="Barbara",
                                        mapping={"id": "seq", "fields": ["temp"]})
        rows = [{"seq": 1, "temp": 300}, {"seq": 2, "temp": 302}]
        with mock.patch("hmi.integrations.get_json", return_value=rows):
            inbound.poll(src)
            src.refresh_from_db()
            inbound.poll(src)
        self.assertEqual(InboundRecord.objects.count(), 2)
        with mock.patch("hmi.integrations.get_json", return_value=rows + [{"seq": 10, "temp": 1}]):
            src.refresh_from_db()
            inbound.poll(src)
        self.assertEqual(InboundRecord.objects.count(), 3)
        self.assertEqual(InboundRecord.objects.last().data, {"temp": 1.0})

    def test_poll_address_must_be_on_the_plant_network(self):
        r = cmd(self.c, "srcSave", {"src": {"name": "x1", "mode": "poll", "url": "http://8.8.8.8/data"}})
        self.assertFalse(r["ok"])


# ------------------------------------------------------------------ downtime: planned time, reasons
class DowntimeTests(Base):
    def setUp(self):
        super().setUp()
        self.c = Client()
        login(self.c, "1111")
        ShiftRun.objects.filter(machine=self.bar, active=True).update(active=False)  # the demo's running shift
        self.assertTrue(cmd(self.c, "startShift", {"line": "Barbara", "mo": "309215", "safety": [True] * 5})["ok"])
        self.sh = ShiftRun.objects.get(machine=self.bar, active=True)

    def put_in_first_shift(self, day):
        ShiftRun.objects.filter(pk=self.sh.pk).update(shift_name="1st shift", start=at(day.year, day.month, day.day, 6, 2))
        self.sh.refresh_from_db()
        engine._planned_cache.clear()

    def test_lunch_and_startup_are_not_downtime(self):
        d = timezone.localdate()
        self.put_in_first_shift(d)
        win = engine.planned_windows(self.sh)
        kinds = engine.planned_windows(self.sh, kinds=True)
        self.assertIn("Lunch", [k for _, _, k in kinds])
        self.assertIn("Start-up", [k for _, _, k in kinds])
        # a stop from 10:20 to 11:10 crosses lunch (10:30-11:00): only 20 minutes are downtime
        engine.add_down(self.sh, at(d.year, d.month, d.day, 10, 20), at(d.year, d.month, d.day, 11, 10))
        self.assertEqual(sorted(Downtime.objects.filter(shift=self.sh).values_list("minutes", flat=True)), [10, 10])
        # a stop only in the start-up time is not logged at all
        engine.add_down(self.sh, at(d.year, d.month, d.day, 6, 3), at(d.year, d.month, d.day, 6, 14))
        self.assertEqual(Downtime.objects.filter(shift=self.sh).count(), 2)
        self.assertTrue(win)

    def test_ticks_mark_planned_minutes(self):
        d = timezone.localdate()
        self.put_in_first_shift(d)
        engine.tick(self.bar, {"ppm": 0}, at(d.year, d.month, d.day, 10, 45))
        engine.tick(self.bar, {"ppm": 0}, at(d.year, d.month, d.day, 11, 45))
        self.assertEqual(list(PpmReading.objects.filter(shift=self.sh).order_by("t").values_list("planned", flat=True)), [True, False])
        self.assertTrue(PlannedTime.objects.filter(shift=self.sh, kind="Lunch").exists())
        stats = engine.shift_stats(self.sh)
        self.assertEqual(stats["mins"], 1)  # the lunch minute doesn't count

    def test_reason_rules(self):
        d = Downtime.objects.create(shift=self.sh, line="Barbara", start_time=timezone.now() - timedelta(minutes=30),
                                    end_time=timezone.now() - timedelta(minutes=20), minutes=10)
        r = cmd(self.c, "downtimeSet", {"id": d.id, "reason": "My own words"})
        self.assertFalse(r["ok"])                                    # operators pick from the list
        self.assertTrue(cmd(self.c, "downtimeSet", {"id": d.id, "reason": "Jam or misfeed"})["ok"])
        other = Client()
        login(other, "2222")                                         # another operator
        self.assertFalse(cmd(other, "downtimeSet", {"id": d.id, "reason": "Maintenance"})["ok"])
        maint = Client()
        login(maint, "3333")                                         # maintenance
        self.assertTrue(cmd(maint, "downtimeSet", {"id": d.id, "reason": "Bearing seized on infeed"})["ok"])
        d.refresh_from_db()
        self.assertEqual(d.reason, "Bearing seized on infeed")
        self.assertIn("changed the reason from Jam or misfeed", d.notes)

    def test_rules_exist_and_can_be_set(self):
        lead = Client()
        login(lead, "9999")
        self.assertTrue(cmd(lead, "ruleSet", {"k": "reasonAfter", "v": 25})["ok"])
        self.assertTrue(cmd(lead, "ruleSet", {"k": "startGrace", "v": 0})["ok"])
        self.assertEqual(PlantSettings.load().config["rules"]["reasonAfter"], 25)


# ------------------------------------------------------------------ downtime alerts
class AlertTests(Base):
    def setUp(self):
        super().setUp()
        self.c = Client()
        login(self.c, "9999")

    def test_contact_validation(self):
        self.assertFalse(cmd(self.c, "alertContactSave", {"contact": {"name": "A"}})["ok"])
        self.assertFalse(cmd(self.c, "alertContactSave", {"contact": {"name": "A", "phone": "12"}})["ok"])
        r = cmd(self.c, "alertContactSave", {"contact": {"name": "Dana", "phone": "(555) 123-4567", "email": "dana@plant.local", "lines": ["Barbara", "Nope"]}})
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["data"]["phone"], r["data"]["lines"]), ("+15551234567", ["Barbara"]))

    def test_down_and_back_messages_once_each(self):
        AlertContact.objects.create(name="Dana", email="dana@plant.local", lines=["Barbara"], down_after=10)
        op = Client()
        login(op, "1111")
        cmd(op, "startShift", {"line": "Barbara", "mo": "309215", "safety": [True] * 5})
        sh = ShiftRun.objects.get(machine=self.bar, active=True)
        ShiftRun.objects.filter(pk=sh.pk).update(shift_name="", stop_start=timezone.now() - timedelta(minutes=12), state="stopped")
        sent = []

        def fake_thread(target, **kw):
            class T:
                def start(self):
                    target()
            return T()
        with mock.patch("hmi.alerts.send_email", side_effect=lambda to, subj, body: sent.append(subj)), \
                mock.patch("hmi.alerts.threading.Thread", side_effect=fake_thread), mock.patch("hmi.alerts.close_old_connections"):
            alerts.check()
            alerts.check()  # same stop: not again
            self.assertEqual(sent, ["Barbara is down"])
            ShiftRun.objects.filter(pk=sh.pk).update(stop_start=None, state="running")
            alerts.check()
            alerts.check()
        self.assertEqual(sent, ["Barbara is down", "Barbara is running again"])
        self.assertEqual(AlertLog.objects.filter(ok=True, channel="email").count(), 2)

    def test_short_stop_is_quiet(self):
        AlertContact.objects.create(name="Dana", email="dana@plant.local", down_after=30)
        op = Client()
        login(op, "1111")
        cmd(op, "startShift", {"line": "Barbara", "mo": "309215", "safety": [True] * 5})
        ShiftRun.objects.filter(active=True).update(shift_name="", stop_start=timezone.now() - timedelta(minutes=5), state="stopped")
        with mock.patch("hmi.alerts.send_email") as se:
            alerts.check()
        se.assert_not_called()

    def test_mail_server_must_be_on_the_plant_network_unless_allowed(self):
        d = Path(tempfile.mkdtemp())
        with override_settings(BASE_DIR=d):
            r = cmd(self.c, "alertConfigSave", {"group": "smtp", "fields": {"host": "8.8.8.8", "port": "587", "from": "a@b.com"}})
            self.assertFalse(r["ok"])
            r = cmd(self.c, "alertConfigSave", {"group": "smtp", "fields": {"host": "8.8.8.8", "port": "587", "from": "a@b.com", "internet": True}})
            self.assertTrue(r["ok"], r)
            self.assertIn('ALERT_SMTP_INTERNET="1"', (d / ".env").read_text())
            cmd(self.c, "alertConfigSave", {"group": "smtp-remove"})

    def test_phone_numbers(self):
        self.assertEqual(alerts.phone_e164("555.123.4567"), "+15551234567")
        self.assertEqual(alerts.phone_e164("+44 20 7946 0958"), "+442079460958")
        with self.assertRaises(alerts.AlertError):
            alerts.phone_e164("123")


# ------------------------------------------------------------------ Reports
class ReportTests(Base):
    def setUp(self):
        super().setUp()
        PpmReading.objects.all().delete()  # only this test's readings, not the demo's
        Downtime.objects.all().delete()
        sh = ShiftRun.objects.create(machine=self.bar, line="Barbara", shift_name="1st shift", start=at(2026, 3, 4, 6), active=False)
        for i in range(60):
            PpmReading.objects.create(machine=self.bar, line="Barbara", shift=sh, t=at(2026, 3, 4, 7) + timedelta(minutes=i),
                                      ppm=180 if i < 45 else 0, units=7 if i < 45 else 0, run=i < 45)
        Downtime.objects.create(shift=sh, line="Barbara", mo="1", start_time=at(2026, 3, 4, 7, 45), end_time=at(2026, 3, 4, 8, 0), minutes=15, reason="Jam or misfeed")

    def get(self, scope, period, c=None):
        return (c or Client()).get(f"/api/report?scope={scope}&period={period}").json()

    def test_drill_down_and_up(self):
        r = self.get("plant", "month:2026-03")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["total"]["units"], 315)
        self.assertEqual(r["total"]["uptime"], 75.0)
        self.assertEqual(r["total"]["down"], 15)
        row = next(x for x in r["rows"] if x["label"] == "Barbara")
        self.assertEqual(row["key"], "line:Barbara")
        self.assertEqual(len(r["buckets"]), 31)
        day = next(b for b in r["buckets"] if b["key"] == "day:2026-03-04")
        self.assertEqual(day["units"], 315)
        r = self.get("line:Barbara", "day:2026-03-04")
        self.assertEqual([x["label"] for x in r["rows"]], ["1st shift"])
        self.assertEqual(len(r["buckets"]), 24)
        self.assertEqual([c["label"] for c in r["scope"]["crumbs"]], ["All lines", "Barbara"])
        self.assertEqual([c["key"] for c in r["period"]["crumbs"]], ["year:2026", "month:2026-03", "day:2026-03-04"])
        self.assertEqual(self.get("plant", "year:2026")["total"]["units"], 315)
        self.assertEqual(self.get("plant", "week:2026-03-04")["period"]["key"], "week:2026-03-02")

    def test_wages_only_for_shift_leads(self):
        LaborPlan.objects.create(pk=1, data=PLAN, version=1)
        self.assertNotIn("laborCost", self.get("plant", "month:2026-03")["total"])
        c = Client()
        login(c, "9999")
        r = self.get("plant", "month:2026-03", c)
        self.assertEqual(r["total"]["laborCost"], 52.0)  # 60 minutes on the 1st shift crew at $52/h
        self.assertAlmostEqual(r["total"]["laborPerUnit"], round(52 / 315, 4))

    def test_bad_input(self):
        self.assertEqual(Client().get("/api/report?period=month:2026-13").status_code, 400)
        self.assertEqual(Client().get("/api/report?scope=zzz").status_code, 400)

    def test_grafana_panel_data(self):
        from reports import grafana
        panel = {"id": 2, "datasource": {"uid": "ds"}, "targets": [{"refId": "A", "rawSql": "SELECT ... WHERE line = '$line'"}]}
        frames = {"results": {"A": {"frames": [{"schema": {"fields": [{"name": "time", "type": "time"}, {"name": "P/m", "type": "number"}]},
                                                 "data": {"values": [[1, 2, 3], [100, 200, 150]]}}]}}}
        with mock.patch("reports.grafana._call", return_value=frames) as call:
            s = grafana.panel_data(panel, 0, 1000, {"line": "Barbara"})
        sent = call.call_args[0][1]["queries"][0]["rawSql"]
        self.assertIn("'Barbara'", sent)
        self.assertEqual((s[0]["last"], s[0]["max"], s[0]["avg"]), (150, 200, 150.0))
