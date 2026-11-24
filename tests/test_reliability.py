"""Reliability fixes from the code audit: database-enforced uniqueness, the ticker's lease fence, webhook ids,
inventory policy, inbound times and replays, feed limits, the PIN limiter's fallback, camera origins and logins,
and traceability reconciliation."""
import json
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from hmi import engine, inbound
from hmi.models import AutoQC, DataSource, Event, MoRun, PMTask, PpmReading, ShiftRun, Ticket
from maintenance.models import InventoryItem, InventoryMove
from plant.demo import seed_demo
from plant.models import Machine, Person, PlantSettings


class Reliability(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.bar = Machine.objects.get(name="Barbara")
        self.mt = engine.Ctx(user=Person.objects.get(pin="3333"))

    def test_one_active_log_per_machine(self):
        ShiftRun.objects.filter(machine=self.bar).update(active=False)
        a = ShiftRun.objects.create(machine=self.bar, line="Barbara", start=timezone.now(), active=True)
        self.assertEqual(a.active_machine, self.bar.pk)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ShiftRun.objects.create(machine=self.bar, line="Barbara", start=timezone.now(), active=True)
        a.active = False
        a.save(update_fields=["active"])
        a.refresh_from_db()
        self.assertIsNone(a.active_machine)
        ShiftRun.objects.create(machine=self.bar, line="Barbara", start=timezone.now(), active=True)   # free again

    def test_one_open_request_per_pm_task(self):
        task = PMTask.objects.create(machine="Barbara", title="Grease", every=1, unit="days", active=True, created=timezone.now())
        now = timezone.now()
        Ticket.objects.create(t=now, machine="Barbara", by="x", type="PM", pm=task)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Ticket.objects.create(t=now, machine="Barbara", by="y", type="PM", pm=task)
        from hmi import pm
        with mock.patch.object(pm, "used", return_value=99):
            self.assertEqual([x for x in pm.check(now) if x.pm_id == task.pk], [])   # the open one blocks a second

    def test_lease_fence_stops_a_stalled_ticker(self):
        from hmi import ticker
        from hmi.models import LineState
        t = timezone.now()
        self.assertTrue(ticker.claim_lease("a", t))
        LineState.objects.update(next_tick=t.replace(second=0, microsecond=0))
        PlantSettings.objects.filter(pk=1).update(ticker_owner="b")      # b took over while a was reading
        with mock.patch.object(engine, "tick") as tick:
            ticker.tick_machines(t, owner="a")
            tick.assert_not_called()

    def test_webhook_events_carry_a_stable_id(self):
        Event.objects.all().delete()
        e = Event.objects.create(t=timezone.now(), kind="x", data={"a": 1})
        st = PlantSettings.load()
        st.settings = {**st.settings, "webhook": "http://192.168.100.9/hook"}
        st.save()
        from hmi import integrations, ticker
        sent = []
        with mock.patch.object(integrations, "post_webhook", side_effect=integrations.ConnError("lost")):
            ticker.timed_pulls({}, timezone.now())
        with mock.patch.object(integrations, "post_webhook", side_effect=lambda url, ev: sent.append(ev)):
            ticker.timed_pulls({}, timezone.now())
        e.refresh_from_db()
        self.assertTrue(e.sent)
        self.assertEqual(e.attempts, 2)
        self.assertEqual(sent[0][0]["id"], str(e.uid))

    def test_inventory_policy_and_history(self):
        it = InventoryItem.objects.create(name="Belt", part_no="B1", qty=1, min_qty=0)
        r = engine.execute("itemUse", {"item": it.pk, "qty": "3", "machine": "Barbara"}, self.mt)
        self.assertTrue(r["ok"], r)                      # allowed by default (emergency repairs)
        self.assertTrue(engine.execute("itemPolicy", {"below": "0"}, self.mt)["ok"])
        r = engine.execute("itemUse", {"item": it.pk, "qty": "1", "machine": "Barbara"}, self.mt)
        self.assertFalse(r["ok"])
        mv = InventoryMove.objects.get(kind="used")
        self.assertTrue(engine.execute("itemUseUndo", {"id": mv.pk}, self.mt)["ok"])
        self.assertEqual(sorted(InventoryMove.objects.values_list("kind", flat=True)), ["back", "undone"])   # nothing deleted
        it.refresh_from_db()
        self.assertEqual(it.qty, 1)

    def test_inbound_keeps_the_sent_time_and_skips_replays(self):
        src = DataSource.objects.create(name="v", kind="qc", line="Barbara", mapping={"len": "L", "passed": "ok", "time": "ts", "id": "n"})
        far = (timezone.now() + timedelta(days=3)).isoformat()
        r = inbound.ingest(src, [{"n": 1, "L": 10, "ok": 1, "ts": far}, {"n": 2, "L": 10, "ok": 1, "ts": "garbage"}])
        self.assertEqual((r["stored"], r["timeFixed"]), (2, 2))
        q = AutoQC.objects.order_by("id")
        self.assertEqual([x.t_fixed for x in q], ["ahead", "unreadable"])
        self.assertEqual(q[0].sent_t, far[:40])
        r = inbound.ingest(src, [{"n": 1, "L": 10, "ok": 1}, {"n": 3, "L": 10, "ok": 1}])
        self.assertEqual((r["stored"], r["repeats"]), (1, 1))

    def test_feed_limits(self):
        with override_settings(SUITE=dict(settings.SUITE, FEED_KEY="k")):
            c = Client()
            r = c.post("/api/feed/Barbara", json.dumps({"ppm": 1, "pad": "x" * 70000}), content_type="application/json", HTTP_X_FEED_KEY="k")
            self.assertEqual(r.status_code, 413)
            self.assertEqual(c.post("/api/feed/Barbara", "{}", content_type="application/json", HTTP_X_FEED_KEY="kk").status_code, 403)
            from hmi import views
            with mock.patch.object(views, "FEED_PER_MIN", 1):
                ok = json.dumps({"ppm": 1})
                self.assertEqual(c.post("/api/feed/Barbara", ok, content_type="application/json", HTTP_X_FEED_KEY="k").status_code, 200)
                self.assertEqual(c.post("/api/feed/Barbara", ok, content_type="application/json", HTTP_X_FEED_KEY="k").status_code, 429)

    def test_pin_limit_without_the_shared_cache(self):
        from hmi import views
        views._LOCAL_FAILS.clear()
        rq = mock.Mock(META={"REMOTE_ADDR": "192.168.100.77"}, session={})
        with mock.patch.object(views.cache, "get", side_effect=RuntimeError("down")), \
                mock.patch.object(views.cache, "set", side_effect=RuntimeError("down")):
            for _ in range(20):
                rq.session = {}        # a new screen session each time: only the address limit applies
                views.check_pin(rq, "nope")
            rq.session = {}
            u, err = views.check_pin(rq, "3333")
        self.assertIsNone(u)
        self.assertIn("Too many", err)

    def test_trace_overlap_is_reported(self):
        from hmi import trace
        t0 = timezone.now().replace(second=0, microsecond=0) - timedelta(hours=3)
        PpmReading.objects.filter(line="Barbara").delete()
        ShiftRun.objects.filter(line="Barbara").update(active=False)
        sh = ShiftRun.objects.create(machine=self.bar, line="Barbara", start=t0, end=t0 + timedelta(hours=2), active=False)
        for i in range(120):
            PpmReading.objects.create(shift=sh, line="Barbara", t=t0 + timedelta(minutes=i), ppm=10, units=10)
        MoRun.objects.filter(line="Barbara").delete()
        MoRun.objects.create(shift=sh, line="Barbara", mo="A1", item="P", start_time=t0, end_time=t0 + timedelta(minutes=70), qty=0)
        MoRun.objects.create(shift=sh, line="Barbara", mo="B2", item="P", start_time=t0 + timedelta(minutes=60), end_time=t0 + timedelta(hours=2), qty=0)
        rec = trace.reconcile(timezone.now(), t0, t0 + timedelta(hours=2), "Barbara")[0]
        self.assertEqual(rec["made"], 1200)
        self.assertEqual(rec["onMo"], 1300)
        self.assertEqual(rec["mo"]["over"], 100)
        self.assertEqual(rec["overlapUnits"], 100)
        self.assertEqual(rec["overlaps"][0][:2], ("A1", "B2"))
        self.assertEqual(trace.mo_rolls("A1", timezone.now())["overlaps"][0][0], "B2")


class CameraSafety(TestCase):
    def test_go2rtc_has_no_wildcard_origin_and_errors_hide_logins(self):
        from cameras import grab, live
        self.assertNotIn("origin", live.config_text({}))
        self.assertEqual(grab.scrub("rtsp://admin:s3cr%40t@192.168.100.60/x failed s3cr@t", "s3cr@t"),
                         "rtsp://•••@192.168.100.60/x failed •••")


class BoundaryBugs(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.bar = Machine.objects.get(name="Barbara")

    def test_new_log_carries_the_counter_across_a_boundary(self):
        ShiftRun.objects.filter(machine=self.bar).update(active=False)
        t = timezone.now()
        ShiftRun.objects.create(machine=self.bar, line="Barbara", start=t - timedelta(hours=8), end=t, active=False, last_count=48211, acc=3.5)
        new = ShiftRun.objects.create(machine=self.bar, line="Barbara", start=t, active=True)
        self.assertEqual((new.last_count, new.acc), (48211, 3.5))
        new.active = False
        new.end = t
        new.save()
        later = ShiftRun.objects.create(machine=self.bar, line="Barbara", start=t + timedelta(minutes=30), active=True)
        self.assertIsNone(later.last_count)          # after a gap the counter may have moved with no log open: not carried

    def test_counter_counts_the_first_minute_of_the_next_log(self):
        from hmi.models import PpmReading
        ShiftRun.objects.filter(machine=self.bar).update(active=False)
        t = timezone.now().replace(second=0, microsecond=0)
        ShiftRun.objects.create(machine=self.bar, line="Barbara", start=t - timedelta(hours=1), end=t, active=False, last_count=1000)
        sh = ShiftRun.objects.create(machine=self.bar, line="Barbara", start=t, active=True, state="running")
        with mock.patch("hmi.engine.has_counter", return_value=True):
            engine.tick(self.bar, {"ppm": 200, "count": 1100}, t + timedelta(minutes=1))
        sh.refresh_from_db()
        self.assertEqual(sh.count, 100)
        self.assertEqual(PpmReading.objects.filter(shift=sh).first().units, 100)

    def test_station_key_guards_the_suite_admin_not_the_table_editor(self):
        with override_settings(SUITE=dict(settings.SUITE, STATION_KEY="k1")):
            c = Client()
            self.assertEqual(c.get("/admin/").status_code, 401)
            self.assertNotEqual(c.get("/db-admin/").status_code, 401)     # its own login
            self.assertNotEqual(c.get("/admin/?key=k1").status_code, 401)

    def test_updater_imports_on_its_own(self):
        import subprocess
        import sys
        from pathlib import Path
        src = Path(__file__).resolve().parent.parent / "tools" / "updater.py"
        r = subprocess.run([sys.executable, "-c", f"import importlib.util as u; s=u.spec_from_file_location('x', r'{src}'); "
                            "m=u.module_from_spec(s); s.loader.exec_module(m); print('ok')"], capture_output=True, text=True, cwd="/")
        self.assertIn("ok", r.stdout, r.stderr)


class Recovery(TestCase):
    """Outages: after the ticker (or the database) was down for more than 5 minutes, counting starts again from now
    and the missed minutes are not invented; a machine feed that fails is flagged, and counting goes on when it's back."""
    def setUp(self):
        seed_demo(reset=True)

    def test_gap_is_not_filled_in(self):
        from hmi import ticker
        from hmi.models import LineState
        t = timezone.now().replace(second=0, microsecond=0)
        LineState.objects.update(next_tick=t - timedelta(hours=2))
        before = PpmReading.objects.count()
        ticker.tick_machines(t + timedelta(seconds=5))
        added = PpmReading.objects.count() - before
        self.assertLessEqual(added, Machine.objects.filter(stage="s1", active=True).count())   # one minute per machine, not 120
        self.assertTrue(all(ls.next_tick >= t for ls in LineState.objects.all()))

    def test_failed_feed_is_flagged_then_recovers(self):
        from hmi import ticker
        from hmi.models import LineState
        bar = Machine.objects.get(name="Barbara")
        t = timezone.now().replace(second=0, microsecond=0)
        LineState.objects.filter(machine=bar).update(next_tick=t)
        with mock.patch.object(ticker, "sample_for", return_value=(None, {"type": "db", "ok": False, "err": "down", "ppm": None, "t": 0})):
            ticker.tick_machines(t + timedelta(seconds=5))
        self.assertFalse(LineState.objects.get(machine=bar).feed["ok"])
        LineState.objects.filter(machine=bar).update(next_tick=t + timedelta(minutes=1))
        ticker.tick_machines(t + timedelta(minutes=1, seconds=5))
        self.assertTrue(LineState.objects.get(machine=bar).next_tick > t + timedelta(minutes=1))
