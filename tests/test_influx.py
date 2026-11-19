"""InfluxDB: settings in Admin > Connections, machine data from a query, and the copy of the suite's records."""
import json
import tempfile
import threading
import urllib.parse
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from django.conf import settings
from django.test import Client, override_settings
from django.utils import timezone

from hmi import influx, integrations, ticker
from hmi.models import Downtime, LineState, PlannedTime, PpmReading, ShiftRun
from plant.models import Machine, PlantSettings

from .test_platform import Base, cmd, login


class FakeInflux:
    """Just enough of InfluxDB 2.x and 1.x to test against."""

    def __init__(self, version="2"):
        self.version, self.writes, self.queries, self.auth = version, [], [], []
        self.ppm_time = timezone.now()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, body=b"", ctype="application/json", headers=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                qs = urllib.parse.parse_qs(u.query)
                fake.auth.append(self.headers.get("Authorization"))
                if u.path == "/health":
                    return self.reply(200, b'{"status":"pass","version":"v2.7.11"}')
                if u.path == "/ping":
                    return self.reply(204, headers={"X-Influxdb-Version": "1.8.10"})
                if u.path == "/query":
                    q = qs["q"][0]
                    fake.queries.append(q)
                    if q == "SHOW DATABASES":
                        return self.reply(200, json.dumps({"results": [{"series": [{"columns": ["name"], "values": [["_internal"], ["machines"]]}]}]}).encode())
                    t = int(fake.ppm_time.timestamp() * 1000)
                    return self.reply(200, json.dumps({"results": [{"series": [{"name": "pleater", "columns": ["time", "ppm", "count"],
                                                                                  "values": [[t, 177.0, 5000]]}]}]}).encode())
                self.reply(404, b'{"message":"not found"}')

            def do_POST(self):
                u = urllib.parse.urlparse(self.path)
                qs = urllib.parse.parse_qs(u.query)
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                fake.auth.append(self.headers.get("Authorization"))
                if u.path in ("/api/v2/write", "/write"):
                    fake.writes.append((qs, body))
                    return self.reply(204)
                if u.path == "/api/v2/query":
                    q = json.loads(body)["query"]
                    fake.queries.append(q)
                    if q.startswith("buckets()"):
                        return self.reply(200, b",result,table,name\r\n,_result,0,_monitoring\r\n,_result,0,machines\r\n,_result,0,suite\r\n\r\n", "text/csv")
                    t = fake.ppm_time.strftime("%Y-%m-%dT%H:%M:%S.123456789Z")
                    old = (fake.ppm_time - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    csv = (",result,table,_time,_value,_field,_measurement,line\r\n"
                           f",_result,0,{old},150,ppm,pleater,Barbara\r\n,_result,0,{t},183.5,ppm,pleater,Barbara\r\n\r\n"
                           ",result,table,_time,_value,_field,_measurement,line\r\n"
                           f",_result,1,{t},48211,count,pleater,Barbara\r\n\r\n")
                    return self.reply(200, csv.encode(), "text/csv")
                self.reply(404, b'{"message":"not found"}')

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class InfluxTests(Base):
    def setUp(self):
        super().setUp()
        self.dir = Path(tempfile.mkdtemp())
        o = override_settings(BASE_DIR=self.dir)
        o.enable()
        self.addCleanup(o.disable)
        for k in list(settings.SUITE):
            if k.startswith("INFLUX_"):
                settings.SUITE[k] = ""
        self.c = Client()
        login(self.c, "9999")
        self.fake = FakeInflux()
        self.addCleanup(self.fake.close)

    def save(self, **f):
        fields = {"url": self.fake.url, "version": "2", "org": "plant", "token": "tok-123", "out": ""}
        fields.update(f)
        return cmd(self.c, "connSave", {"group": "influx", "fields": fields})

    def test_only_reading_queries(self):
        for bad in ['from(bucket:"a") |> range(start:-1m) |> to(bucket:"b")', 'import "http"\nhttp.post(url:"http://x")',
                    'import "sql"\nsql.from(driverName:"x")']:
            with self.assertRaises(influx.InfluxError, msg=bad):
                influx.check_query(bad, "2")
        influx.check_query('import "strings"\nfrom(bucket:"machines") |> range(start:-1m) |> last()', "2")
        for bad in ['SELECT * INTO other FROM pleater', 'DROP DATABASE machines', 'SELECT 1; DROP MEASUREMENT x', 'DELETE FROM x']:
            with self.assertRaises(influx.InfluxError, msg=bad):
                influx.check_query(bad, "1")
        influx.check_query("SELECT last(\"ppm\") FROM \"pleater\" WHERE \"note\" = 'drop into'", "1")

    def test_save_test_and_remove(self):
        r = self.save()
        self.assertTrue(r["ok"], r)
        self.assertIn("2 buckets", r["msg"])  # _monitoring is left out
        env = (self.dir / ".env").read_text()
        self.assertIn('INFLUX_TOKEN="tok-123"', env)
        info = cmd(self.c, "connInfo", {})["data"]["influx"]
        self.assertTrue(info["on"] and info["hasToken"])
        self.assertNotIn("tok-123", json.dumps(info))
        self.assertIn("Token tok-123", self.fake.auth)
        # a blank token keeps the saved one
        self.assertTrue(self.save(token="")["ok"])
        self.assertEqual(settings.SUITE["INFLUX_TOKEN"], "tok-123")
        self.assertTrue(cmd(self.c, "connRemove", {"group": "influx"})["ok"])
        self.assertNotIn("INFLUX_URL", (self.dir / ".env").read_text())

    def test_public_address_and_bad_login_not_saved(self):
        self.assertFalse(self.save(url="http://8.8.8.8:8086")["ok"])
        self.assertFalse(self.save(org="")["ok"])
        op = Client()
        login(op, "1111")
        self.assertFalse(cmd(op, "connSave", {"group": "influx", "fields": {"url": self.fake.url}})["ok"])
        self.assertFalse((self.dir / ".env").exists())

    def test_saved_even_when_it_does_not_answer(self):
        from unittest import mock
        with mock.patch.object(influx, "test", side_effect=influx.InfluxError("InfluxDB refused the token")):
            r = self.save()
        self.assertTrue(r["ok"], r)
        self.assertIn("isn't connecting yet: InfluxDB refused the token", r["msg"])
        self.assertIn('INFLUX_TOKEN="tok-123"', (self.dir / ".env").read_text())

    def test_machine_reads_speed_from_a_flux_query(self):
        self.assertTrue(self.save()["ok"])
        r = cmd(self.c, "lineSet", {"id": "Barbara", "f": "feed.type", "v": "influx"})
        self.assertTrue(r["ok"], r)
        m = Machine.objects.get(name="Barbara")
        self.assertIn('r.line == "Barbara"', m.feed_query)  # starts from a sample query
        self.assertFalse(cmd(self.c, "lineSet", {"id": "Barbara", "f": "feed.query", "v": 'from(bucket:"a") |> to(bucket:"b")'})["ok"])
        self.assertTrue(cmd(self.c, "lineSet", {"id": "Barbara", "f": "feed.countField", "v": "count"})["ok"])
        m.refresh_from_db()
        s = influx.fetch_feed(m)
        self.assertEqual(s, {"ppm": 183.5, "count": 48211})  # the newest ppm, folded by _field
        self.assertEqual(integrations.test("feed:Barbara")[0], True)
        ls = LineState.objects.get(machine=m)
        sample, feed = ticker.sample_for(m, ls, timezone.now())
        self.assertEqual((sample["ppm"], feed["type"], feed["ok"]), (183.5, "influx", True))
        # a reading that stopped updating is no data, not downtime
        self.fake.ppm_time = timezone.now() - timedelta(minutes=10)
        with self.assertRaises(influx.InfluxError):
            influx.fetch_feed(m)

    def test_influxdb_1_with_influxql(self):
        self.fake.version = "1"
        r = self.save(version="1", org="", user="reader", token="pw", db="machines")
        self.assertTrue(r["ok"], r)
        self.assertIn("1.8.10", r["msg"])
        m = Machine.objects.get(name="Barbara")
        m.feed_type, m.feed_query, m.feed_count_field = "influx", influx.default_query("Barbara", "1"), "count"
        m.save()
        self.assertEqual(influx.fetch_feed(m), {"ppm": 177.0, "count": 5000})
        self.assertTrue(any(a and a.startswith("Basic ") for a in self.fake.auth))

    def test_copies_suite_records_with_planned_time(self):
        self.assertTrue(self.save(out="suite")["ok"])
        sh = ShiftRun.objects.filter(active=True).first()
        ShiftRun.objects.filter(pk=sh.pk).update(shift_name="2nd shift")
        now = timezone.now()
        PpmReading.objects.all().delete()
        PpmReading.objects.create(machine=sh.machine, line=sh.line, shift=sh, t=now - timedelta(minutes=3), ppm=190, units=4)
        PpmReading.objects.create(machine=sh.machine, line=sh.line, shift=sh, t=now - timedelta(minutes=2), ppm=0, planned=True, run=False)
        PpmReading.objects.create(machine=sh.machine, line=sh.line, shift=sh, t=now - timedelta(minutes=1), ppm=0, run=False)
        Downtime.objects.all().delete()
        Downtime.objects.create(shift=sh, line=sh.line, mo="309215", start_time=now - timedelta(minutes=1), end_time=now, minutes=1, reason='Jam, "bad" roll')
        PlannedTime.objects.all().delete()  # the demo's own breaks depend on the time of day
        PlannedTime.objects.create(shift=sh, line=sh.line, start=now - timedelta(minutes=2), end=now - timedelta(minutes=1), kind="Lunch")
        PlantSettings.objects.update(conn={})
        n = influx.push(now)
        self.assertEqual(n, 5)
        qs, body = self.fake.writes[0]
        self.assertEqual((qs["bucket"], qs["precision"]), (["suite"], ["ms"]))
        lines = "\n".join(b for _, b in self.fake.writes).splitlines()
        minute = [x for x in lines if x.startswith("suite_minute")]
        self.assertEqual(len(minute), 3)
        self.assertIn("ppm=190i,units=4i,run=1i,planned=0i,down=0i,counted=1i", minute[0])
        self.assertIn("run=0i,planned=1i,down=0i,counted=0i", minute[1])  # lunch: not downtime
        self.assertIn("run=0i,planned=0i,down=1i,counted=1i", minute[2])
        self.assertTrue(minute[0].startswith("suite_minute,line=Barbara,shift=2nd\\ shift "), minute[0])
        self.assertTrue(any(x.startswith("suite_downtime,line=Barbara ") and 'reason="Jam, \\"bad\\" roll"' in x for x in lines))
        self.assertTrue(any(x.startswith("suite_planned,kind=Lunch,line=Barbara ") for x in lines))
        # the next copy only resends downtime and breaks (so a reason filled in later is updated), not old minutes
        self.fake.writes.clear()
        self.assertEqual(influx.push(now), 2)
        self.assertEqual(PlantSettings.load().conn["influxOut"]["ok"], True)
        # InfluxDB down: an error is recorded and nothing is lost
        self.fake.close()
        PpmReading.objects.create(machine=sh.machine, line=sh.line, shift=sh, t=now, ppm=180)
        with self.assertRaises(influx.InfluxError):
            influx.push(now)
        self.assertFalse(PlantSettings.load().conn["influxOut"]["ok"])

    def test_flux_csv_and_line_protocol(self):
        rows = influx.parse_flux_csv(",result,table,a,b\r\n,_result,0,1,x y\r\n\r\n,result,table,c\r\n,_result,1,2.5\r\n")
        self.assertEqual(rows, [{"a": 1, "b": "x y"}, {"c": 2.5}])
        with self.assertRaises(influx.InfluxError):
            influx.parse_flux_csv(",error,reference\r\n,bucket not found,\r\n")
        t = timezone.now()
        p = influx.point("m x", {"line": "Line 2,A", "empty": ""}, {"s": 'a"b', "i": 3, "f": 1.5, "b": True}, t)
        self.assertEqual(p, f'm\\ x,line=Line\\ 2\\,A s="a\\"b",i=3i,f=1.5,b=true {int(t.timestamp() * 1000)}')
