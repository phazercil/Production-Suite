"""Sensors & custom alarms: connection types, scaling, alarm rules and what they do, the page and the push API."""
import json
import socket
import struct
import threading
from datetime import timedelta
from unittest import mock

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from hmi.models import AlertContact, LineState, Notice, ShiftRun, Ticket
from plant.demo import seed_demo
from plant.models import Machine
from sensors import engine, kinds
from sensors.models import Sensor, SensorAlarm, SensorRule, SensorValue

from .test_quality import sup_login


def roll(**kw):
    base = dict(name="barb_roll", label="Media roll", machine="Barbara", kind="manual", scale=True, raw_lo=0, raw_hi=5, eng_lo=0, eng_hi=500,
                raw_unit="V", unit="mm", decimals=0)
    base.update(kw)
    return Sensor.objects.create(**base)


class ModbusServer:
    """A tiny Modbus TCP device on localhost: holding registers 0..9, input registers, coils."""
    def __init__(self, regs):
        self.regs = regs
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            with c:
                head = c.recv(7)
                tid, pid, ln, unit = struct.unpack(">HHHB", head)
                fn, addr, count = struct.unpack(">BHH", c.recv(ln - 1))
                if addr >= len(self.regs):
                    pdu = struct.pack(">BB", fn | 0x80, 2)
                elif fn in (3, 4):
                    vals = self.regs[addr:addr + count]
                    pdu = struct.pack(f">BB{len(vals)}H", fn, len(vals) * 2, *vals)
                else:
                    pdu = struct.pack(">BBB", fn, 1, 1 if self.regs[addr] else 0)
                c.sendall(struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu)

    def close(self):
        self.sock.close()


class ScalingTests(TestCase):
    def test_two_point_scaling_reversed_clamped_offset_and_on_off(self):
        s = Sensor(raw_lo=0, raw_hi=5, eng_lo=0, eng_hi=500, scale=True, clamp=True)
        self.assertEqual(engine.scaled(s, 2.5), 250)
        self.assertEqual(engine.scaled(s, 6), 500)                     # kept inside the range
        s.clamp = False
        self.assertEqual(engine.scaled(s, 6), 600)
        r = Sensor(raw_lo=4, raw_hi=20, eng_lo=500, eng_hi=0, scale=True, clamp=True, offset=-2)   # 4–20 mA, reversed
        self.assertEqual(engine.scaled(r, 12), 248)
        o = Sensor(dtype="onoff", invert=True)
        self.assertEqual((engine.scaled(o, 1), engine.scaled(o, 0)), (0.0, 1.0))
        self.assertEqual(kinds.number("high"), 1.0)
        self.assertEqual(kinds.number("1,234.5"), 1234.5)

    def test_formulas_are_safe(self):
        self.assertEqual(kinds.evaluate("avg(a, b) - 1", {"a": 4, "b": 6}), 4)
        for bad in ("__import__('os')", "a.real", "[1][0]", "9 ** 99"):
            with self.assertRaises(kinds.ReadError):
                kinds.evaluate(bad, {"a": 1})

    def test_modbus_tcp_registers_floats_bits_and_refusals(self):
        f = struct.unpack(">HH", struct.pack(">f", 12.5))
        dev = ModbusServer([1234, 65535, f[0], f[1], 0b1010, 0, 0, 0, 0, 0])
        self.addCleanup(dev.close)
        c = {"host": "127.0.0.1", "port": str(dev.port), "unit": "1", "table": "holding", "style": "zero", "format": "uint16", "words": "big", "address": "0"}
        with mock.patch("plant.netsafety.check_host"):
            self.assertEqual(kinds.read_modbus(c), 1234)
            self.assertEqual(kinds.read_modbus({**c, "address": "1", "format": "int16"}), -1)
            self.assertEqual(kinds.read_modbus({**c, "address": "40003", "style": "one", "format": "float32"}), 12.5)
            self.assertEqual(kinds.read_modbus({**c, "address": "4", "bit": "1"}), 1.0)
            self.assertEqual(kinds.read_modbus({**c, "address": "4", "bit": "2"}), 0.0)
            self.assertEqual(kinds.read_modbus({**c, "table": "coil", "address": "0"}), 1.0)
            with self.assertRaisesRegex(kinds.ReadError, "illegal data address"):
                kinds.read_modbus({**c, "address": "50"})

    def test_modbus_device_must_be_on_the_plant_network(self):
        self.assertIn("public internet", kinds.check_config("modbus", {"host": "8.8.8.8", "port": "502", "unit": "1", "address": "0"}))


class RuleTests(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.t = timezone.now()
        self.s = roll()

    def at(self, sec):
        return self.t + timedelta(seconds=sec)

    def test_below_limit_after_delay_with_deadband_and_clear(self):
        r = SensorRule.objects.create(sensor=self.s, name="Roll almost empty", op="lt", a=100, delay=10, deadband=20,
                                      message="{machine}: change the roll — {value} left (limit {limit} {unit})")
        engine.accept(self.s, 0.9, self.at(0))           # 90 mm
        self.assertFalse(SensorAlarm.objects.exists())   # not for 10 s yet
        engine.accept(self.s, 0.9, self.at(11))
        al = SensorAlarm.objects.get()
        self.assertEqual(al.text, "Barbara: change the roll — 90 mm left (limit 100 mm)")
        engine.accept(self.s, 0.5, self.at(20))
        engine.accept(self.s, 1.1, self.at(30))          # 110 mm: back over 100 but inside the 20 mm deadband
        self.assertIsNone(SensorAlarm.objects.get().end)
        engine.accept(self.s, 1.3, self.at(40))          # 130 mm: clear
        al.refresh_from_db()
        self.assertEqual((al.end, al.worst), (self.at(40), 50))
        r.refresh_from_db()
        self.assertIsNone(r.alarm_id)

    def test_shows_on_the_machines_kiosk_and_acknowledge_removes_a_latched_one(self):
        SensorRule.objects.create(sensor=self.s, op="lt", a=100, latch=True, severity="bad")
        engine.accept(self.s, 0.5, self.at(0))
        cl = Client(HTTP_HX_REQUEST="true")
        cl.get("/station/Barbara/")
        html = cl.get("/kiosk/m/alarms").content.decode()
        self.assertIn("Media roll", html)
        self.assertIn("/kiosk/do/ackSensor", html)
        status = cl.get("/kiosk/v/status").content.decode()
        self.assertIn("Media roll", status)                  # live value on the Status page
        self.assertIn("50 mm", status)
        engine.accept(self.s, 4, self.at(5))             # clears, but latched: still on screen
        self.assertIn("(cleared)", cl.get("/kiosk/m/alarms").content.decode())
        al = SensorAlarm.objects.get()
        r = cl.post("/kiosk/do/ackSensor", {"id": al.pk})
        self.assertNotIn("Media roll", r.content.decode())
        c2 = Client(HTTP_HX_REQUEST="true")
        c2.get("/station/Salvatore/")
        self.assertNotIn("Media roll", c2.get("/kiosk/m/alarms").content.decode())   # another machine's alarm

    def test_ticket_messages_repeat_and_clear(self):
        AlertContact.objects.create(name="Lead", email="lead@example.com", lines=["Barbara"])
        AlertContact.objects.create(name="Other line", email="x@example.com", lines=["Salvatore"])
        SensorRule.objects.create(sensor=self.s, op="le", a=100, ticket=True, ticket_priority="High", notify=True, notify_after=1, repeat=5,
                                  notify_clear=True)
        sent = []
        with mock.patch("hmi.alerts.deliver", side_effect=lambda *a: sent.append(a)), mock.patch("threading.Thread") as th:
            th.return_value.start.side_effect = lambda: None
            th.side_effect = lambda target, **kw: mock.Mock(start=target)
            engine.accept(self.s, 1, self.at(0))
            tk = Ticket.objects.get(by="Sensor alarm")
            self.assertEqual((tk.machine, tk.priority), ("Barbara", "High"))
            engine.notify_due(self.at(30))
            self.assertEqual(sent, [])                    # not lasted a minute yet
            engine.notify_due(self.at(61))
            engine.notify_due(self.at(120))
            self.assertEqual([x[0] for x in sent], ["Lead"])
            engine.notify_due(self.at(61 + 300))
            self.assertEqual(len(sent), 2)
            engine.accept(self.s, 4, self.at(400))
            self.assertTrue(sent[-1][3].startswith("Cleared"))

    def test_only_while_running_stale_stuck_rate_and_fault(self):
        r = SensorRule.objects.create(sensor=self.s, op="lt", a=100, when="running")
        sh = ShiftRun.objects.get(line="Barbara", active=True)
        sh.state = "stopped"
        sh.save()
        engine.accept(self.s, 0.5, self.at(0))
        self.assertFalse(SensorAlarm.objects.exists())
        sh.state = "running"
        sh.save()
        engine.accept(self.s, 0.5, self.at(5))
        self.assertTrue(SensorAlarm.objects.filter(end__isnull=True).exists())
        r.delete()
        stale = SensorRule.objects.create(sensor=self.s, op="stale", a=2)
        engine.check_all(self.at(60))
        self.assertFalse(SensorAlarm.objects.filter(rule=stale).exists())
        engine.check_all(self.at(200))
        self.assertTrue(SensorAlarm.objects.filter(rule=stale, end__isnull=True).exists())
        fall = SensorRule.objects.create(sensor=self.s, op="fall", a=100)
        engine.accept(self.s, 4, self.at(300))
        engine.accept(self.s, 2, self.at(330))            # 200 mm in 30 s = 400 mm/min down
        self.assertTrue(SensorAlarm.objects.filter(rule=fall).exists())
        self.s.fault_lo = 0.2
        self.s.save()
        fault = SensorRule.objects.create(sensor=self.s, op="fault")
        engine.accept(self.s, 0.1, self.at(340))
        self.assertTrue(SensorAlarm.objects.filter(rule=fault).exists())

    def test_minute_values_for_grafana(self):
        m = timezone.now().replace(second=0, microsecond=0) - timedelta(minutes=1)
        for i, v in enumerate((1, 2, 3)):
            engine.accept(self.s, v, m + timedelta(seconds=10 * i))
        engine._last_minute["t"] = None
        engine.write_minute(m + timedelta(minutes=1, seconds=3))
        row = SensorValue.objects.get()
        self.assertEqual((row.value, row.vmin, row.vmax, row.n, row.machine), (200, 100, 300, 3, "Barbara"))


class SourceTests(TestCase):
    def setUp(self):
        seed_demo(reset=True)

    @override_settings(SUITE={**__import__("django.conf").conf.settings.SUITE, "FEED_KEY": "k123"})
    def test_push_api(self):
        s = roll(kind="push")
        c = Client()
        self.assertEqual(c.post("/api/sensors", '{"barb_roll": 1}', content_type="application/json").status_code, 403)
        r = c.post("/api/sensors", json.dumps({"barb_roll": 2.5, "nope": 1}), content_type="application/json", HTTP_X_FEED_KEY="k123").json()
        self.assertEqual((r["stored"], r["unknown"]), (1, ["nope"]))
        s.refresh_from_db()
        self.assertEqual(s.value, 250)
        c.post("/api/sensors", json.dumps([{"sensor": "barb_roll", "value": "1"}]), content_type="application/json", HTTP_X_FEED_KEY="k123")
        s.refresh_from_db()
        self.assertEqual(s.value, 100)

    def test_suite_value_calc_and_polling(self):
        up = Sensor.objects.create(name="barb_count", label="Count", machine="Barbara", kind="suite", config={"what": "count"}, every=1)
        Sensor.objects.create(name="twice", label="Twice", kind="calc", config={"formula": "barb_count * 2"}, every=1)
        sh = ShiftRun.objects.get(line="Barbara", active=True)
        sh.count = 21
        sh.save()
        engine.poll(timezone.now())
        self.assertEqual(Sensor.objects.get(name="barb_count").value, 21)
        self.assertEqual(Sensor.objects.get(name="twice").value, 42)
        up.config = {"what": "mo_left"}
        up.save()
        self.assertIsNotNone(engine.test_read(up))

    def test_extra_field_of_the_machines_own_reading(self):
        s = roll(kind="feed", config={"field": "roll.v"})
        m = Machine.objects.get(name="Barbara")
        from hmi import machinedata
        out = machinedata.add_extras(m, {"ppm": 100}, lambda f: {"roll.v": 3}.get(f))
        self.assertEqual(out["sensors"], {"roll.v": 3})
        engine.from_feed(m, out, timezone.now())
        s.refresh_from_db()
        self.assertEqual(s.value, 300)

    def test_ticker_strips_sensor_fields_before_counting(self):
        roll(kind="feed", config={"field": "roll_v"})
        from hmi import ticker
        m = Machine.objects.get(name="Barbara")
        sample = {"ppm": 100, "sensors": {"roll_v": 1}}
        ticker._sensors_from_feed(m, sample, timezone.now())
        self.assertNotIn("sensors", sample)
        self.assertEqual(Sensor.objects.get().value, 100)


class PageTests(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.c = Client(enforce_csrf_checks=True)
        self.assertTrue(sup_login(self.c, "9999", perm="sensors.edit", page="/sensors/")["ok"])
        self.tok = self.c.cookies["csrftoken"].value

    def post(self, url, data):
        return self.c.post(url, data, HTTP_X_CSRFTOKEN=self.tok)

    def test_add_sensor_then_rule_from_the_page(self):
        r = self.c.get("/sensors/?edit=new")
        for s in ("Modbus TCP", "Web address", "Database table", "InfluxDB query", "Sent to the server", "Incoming data source",
                  "A value the suite already has", "Calculated from other sensors", "Typed in", "4–20 mA", "Test the connection"):
            self.assertContains(r, s)
        r = self.post("/sensors/", {"action": "save", "label": "Media roll", "name": "barb_roll", "machine": "Barbara", "dtype": "number", "kind": "manual",
                                    "every": "10", "scale": "1", "raw_lo": "0", "raw_hi": "5", "raw_unit": "V", "eng_lo": "0", "eng_hi": "500", "unit": "mm",
                                    "clamp": "1", "decimals": "0", "smooth": "1", "offset": "0", "show_kiosk": "1", "enabled": "1"})
        self.assertEqual(r.status_code, 302, r.content[:400])
        s = Sensor.objects.get()
        self.assertEqual((s.machine, s.eng_hi, s.unit), ("Barbara", 500, "mm"))
        r = self.post(f"/sensors/?rules={s.pk}", {"action": "rule", "id": s.pk, "op": "lt", "a": "100", "delay": "0", "when": "always", "severity": "bad",
                                                  "name": "Roll almost empty", "show_kiosk": "1", "enabled": "1", "webhook": "1"})
        self.assertEqual(r.status_code, 302, r.content[:400])
        self.post("/sensors/do/manual", {"id": s.pk, "value": "0.5"})
        self.assertEqual(SensorAlarm.objects.get().severity, "bad")
        page = self.c.get(f"/sensors/?rules={s.pk}")
        self.assertContains(page, "falls below 100 mm")
        self.assertContains(self.c.get("/sensors/?tab=log"), "Roll almost empty")
        self.assertContains(self.c.get("/sensors/?tab=types"), "MQTT")

    def test_refuses_bad_settings_and_keeps_what_was_typed(self):
        r = self.post("/sensors/", {"action": "save", "label": "Roll", "name": "r1", "dtype": "number", "kind": "url", "c_url_url": "http://8.8.8.8/x",
                                    "c_url_field": "v", "enabled": "1"})
        self.assertContains(r, "public internet")
        self.assertContains(r, 'value="http://8.8.8.8/x"')
        r = self.post("/sensors/", {"action": "save", "label": "Roll", "name": "r1", "dtype": "number", "kind": "manual", "scale": "1", "raw_lo": "1",
                                    "raw_hi": "1", "eng_lo": "0", "eng_hi": "5"})
        self.assertContains(r, "two raw points must be different")

    def test_operators_cant_open_it(self):
        c = Client(enforce_csrf_checks=True)
        r = sup_login(c, "1111", perm="sensors.edit", page="/sensors/")
        self.assertFalse(r["ok"])
