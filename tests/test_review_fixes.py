"""Fixes from the code review: each test pins down one of them."""
import datetime as dt
import struct
from types import SimpleNamespace
from unittest import mock

from django.test import Client, SimpleTestCase, TestCase
from django.utils import timezone

from hmi import engine
from hmi.models import Person, Ticket
from maintenance.models import InventoryItem
from plant.demo import seed_demo

from .test_quality import sup_login


class PureFixes(SimpleTestCase):
    def test_mssql_distinct_top(self):
        from plant.dbkinds import top
        self.assertEqual(top("mssql", "DISTINCT line FROM t", 5), "SELECT DISTINCT TOP 5 line FROM t")
        self.assertEqual(top("mssql", "line FROM t", 5), "SELECT TOP 5 line FROM t")
        self.assertEqual(top("mysql", "DISTINCT line FROM t", 5), "SELECT DISTINCT line FROM t LIMIT 5")

    def test_bind_leaves_quoted_text_and_casts_alone(self):
        from hmi.integrations import bind
        sql, names = bind("SELECT DATE_FORMAT(t, '%H:%i'), 'line:A', x::int -- :not\nFROM t WHERE a = :line AND b = :mo", "?")
        self.assertEqual(names, ["line", "mo"])
        self.assertIn("'%H:%i'", sql)
        self.assertIn("'line:A'", sql)
        self.assertIn("x::int", sql)
        self.assertIn("-- :not", sql)
        self.assertTrue(sql.endswith("a = ? AND b = ?"))

    def test_round_places_checked(self):
        from sensors.kinds import _round, number
        self.assertEqual(_round(2.345, 2), 2.35)
        with self.assertRaises(ValueError):
            _round(1.0, 1e9)
        with self.assertRaises(ValueError):
            _round(1.0, 0.5)
        self.assertIsNone(number("NaN"))
        self.assertIsNone(number("inf"))
        self.assertEqual(number("1,250.5"), 1250.5)

    def test_spec_longest_prefix_and_empty_key(self):
        from hmi import inbound
        specs = {"MG36": "short", "MG3672": "long", "": "blank"}
        with mock.patch.object(inbound, "part_key", lambda item: item):
            self.assertEqual(inbound._spec("MG36725", specs), "long")
            self.assertEqual(inbound._spec("MG3601", specs), "short")
            self.assertIsNone(inbound._spec("", specs))
            self.assertIsNone(inbound._spec("XX1", specs))

    def test_week_label_has_no_platform_format(self):
        from reports.engine import _md
        self.assertEqual(_md(dt.date(2026, 3, 2)), "Mon 3/2")

    def test_at_wall_is_local_wall_time_across_dst(self):
        from plant.util import at_wall
        t = at_wall(dt.date(2026, 3, 8), 6 * 60)       # the US spring-forward day: 6:00 is still 6:00 on the clock
        self.assertEqual(timezone.localtime(t).hour, 6)

    def test_modbus_short_answer_is_an_error(self):
        from sensors import kinds

        class Sock:
            def __init__(self, data):
                self.data = data

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def settimeout(self, t):
                pass

            def sendall(self, b):
                pass

            def recv(self, n):
                out, self.data = self.data[:n], self.data[n:]
                return out
        short = struct.pack(">HHHB", 1, 0, 3, 1) + bytes([3, 4])       # says 4 bytes of registers, sends none
        with mock.patch("plant.netsafety.check_host"), mock.patch("socket.create_connection", return_value=Sock(short)), \
                self.assertRaises(kinds.ReadError):
            kinds.modbus_request("192.168.100.9", 502, 1, 3, 0, 2)
        bad_len = struct.pack(">HHHB", 1, 0, 9000, 1)
        with mock.patch("plant.netsafety.check_host"), mock.patch("socket.create_connection", return_value=Sock(bad_len)), \
                self.assertRaises(kinds.ReadError):
            kinds.modbus_request("192.168.100.9", 502, 1, 3, 0, 2)

    def test_camera_redirect_off_the_plant_network_is_refused(self):
        import urllib.request

        from cameras import grab
        h = grab._SafeRedirect()
        req = urllib.request.Request("http://192.168.100.60/snap.jpg")
        with self.assertRaises(grab.CamError):
            h.redirect_request(req, None, 302, "Found", {}, "http://203.0.113.5/steal")
        self.assertIsInstance(grab._http_error(grab.CamError("x")), grab.CamError)

    def test_control_sessions_are_locked(self):
        from plant import control
        s = control.Sessions()
        tok = s.new("K. Patel", "lead")
        self.assertEqual(s.get(tok)["name"], "K. Patel")
        s.drop(tok)
        self.assertIsNone(s.get(tok))
        for _ in range(5):
            s.failed()
        self.assertTrue(s.limited())


class DbFixes(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.mt = engine.Ctx(user=Person.objects.get(pin="3333"))

    def test_item_edit_never_changes_stock(self):
        it = InventoryItem.objects.create(name="Drive belt", part_no="BT-1240", qty=4, min_qty=2)
        r = engine.execute("itemSave", {"id": it.pk, "name": "Drive belt 2", "part_no": "BT-1240", "qty": "99", "min_qty": "2"}, self.mt)
        self.assertTrue(r["ok"], r)
        it.refresh_from_db()
        self.assertEqual((it.name, it.qty), ("Drive belt 2", 4))

    def test_maintenance_redirect_keeps_other_query_and_drops_old_answer(self):
        c = Client()
        self.assertTrue(sup_login(c, "3333", "section.maintenance", "/maintenance/")["ok"])
        t = Ticket.objects.create(t=timezone.now(), line="Barbara", by="J. Ortiz", machine="Barbara", type="Mechanical")
        r = c.post("/maintenance/do/ticketSet", {"id": t.pk, "status": "Reviewed", "anchor": "r1",
                                                 "back": "/maintenance/?tab=done&err=old&page=2&msg=older"})
        loc = r["Location"]
        self.assertTrue(loc.startswith("/maintenance/?"), loc)
        self.assertIn("tab=done", loc)
        self.assertIn("page=2", loc)
        self.assertNotIn("err=old", loc)
        self.assertNotIn("older", loc)
        self.assertEqual(loc.count("msg="), 1)
        self.assertTrue(loc.endswith("#r1"))

    def test_pm_eta_far_away_is_left_out(self):
        from hmi import pm
        task = SimpleNamespace(every=1e12, active=True)
        now = timezone.now()
        with mock.patch.object(pm, "used", return_value=1), mock.patch.object(pm, "rate", return_value=0.001):
            self.assertIsNone(pm.status(task, now)["eta"])          # would be past year 9999: no date, no crash
        with mock.patch.object(pm, "used", return_value=1), mock.patch.object(pm, "rate", return_value=1):
            self.assertIsNotNone(pm.status(SimpleNamespace(every=30, active=True), now)["eta"])

    def test_updater_failed_keeps_recorded_flag(self):
        import importlib.util
        import pathlib
        import sys

        from plant import updatefile
        sys.modules.setdefault("updatefile", updatefile)   # the updater runs beside its own copy
        spec = importlib.util.spec_from_file_location("updater_t", pathlib.Path(__file__).resolve().parent.parent / "tools" / "updater.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        self.assertTrue(m.Failed("x", recorded=True).recorded)
        self.assertFalse(m.Failed("x").recorded)

class SensorNe(SimpleTestCase):
    def test_ne_uses_a_tiny_tolerance(self):
        from sensors import engine as se
        r = SimpleNamespace(op="ne", a=1.0, b=None, deadband=0)
        s = SimpleNamespace(value=1.0 + 1e-12, value_t=timezone.now(), fault=False, state={})
        self.assertFalse(se.condition(r, s, timezone.now()))
        s.value = 1.5
        self.assertTrue(se.condition(r, s, timezone.now()))
