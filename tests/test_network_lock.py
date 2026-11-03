"""Plant network lock: nothing from or to outside the plant network (192.168.100.0/24 unless changed),
except the Paycom import."""
import ipaddress
import io
import json
import os
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase

from hmi import integrations
from plant import netsafety
from plant.demo import seed_demo
from plant.models import PlantSettings


class InboundTests(TestCase):
    def setUp(self):
        cache.clear()
        netsafety.invalidate()
        seed_demo(reset=True)

    def get(self, path, addr):
        return Client(REMOTE_ADDR=addr).get(path)

    def test_public_addresses_are_refused_everywhere(self):
        for addr in ("8.8.8.8", "203.1.2.3", "2001:4860:4860::8888", "100.64.0.1", "", "garbage",
                     "10.4.5.6", "172.20.1.9", "192.168.1.50", "192.168.101.7", "fd12:3456::7", "fe80::1"):
            for path in ("/operator/", "/api/hmi/state", "/scheduler/", "/data/", "/admin/", "/static/hmi/kiosk.css", "/api/feed/Barbara"):
                self.assertEqual(self.get(path, addr).status_code, 403, (addr, path))

    def test_plant_network_and_the_server_itself_are_served(self):
        for addr in ("192.168.100.1", "192.168.100.37", "192.168.100.254", "127.0.0.1", "::1", "::ffff:192.168.100.50"):
            self.assertEqual(self.get("/operator/", addr).status_code, 200, addr)

    def test_changed_setting_applies_right_away(self):
        netsafety.save(netsafety.parse_networks("10.20.0.0/16"), sync=False)
        self.assertEqual(self.get("/operator/", "10.20.4.4").status_code, 200)
        self.assertEqual(self.get("/operator/", "192.168.100.5").status_code, 403)
        self.assertEqual(self.get("/operator/", "127.0.0.1").status_code, 200)  # the server can always fix it

    def test_forwarded_header_cannot_sneak_in(self):
        r = Client(REMOTE_ADDR="8.8.8.8").get("/operator/", HTTP_X_FORWARDED_FOR="192.168.100.5")
        self.assertEqual(r.status_code, 403)

    def test_pages_load_nothing_from_outside(self):
        r = Client().get("/operator/")
        html = r.content.decode()
        self.assertNotIn("googleapis", html)
        self.assertNotIn("https://", html)
        csp = r["Content-Security-Policy"]
        self.assertIn("default-src 'self'", csp)
        self.assertIn("connect-src 'self'", csp)
        self.assertIn("frame-src 'none'", csp)

    def test_grafana_frames_allowed_only_for_a_private_grafana(self):
        st = PlantSettings.load()
        st.settings["grafana"].update({"on": True, "url": "http://192.168.100.20:3000"})
        st.save()
        self.assertIn("frame-src http://192.168.100.20:3000", Client().get("/data/")["Content-Security-Policy"])
        st.settings["grafana"]["url"] = "http://10.9.9.9:3000"  # private, but not the plant network
        st.save()
        self.assertIn("frame-src 'none'", Client().get("/data/")["Content-Security-Policy"])
        st.settings["grafana"]["url"] = "http://8.8.8.8:3000"
        st.save()
        self.assertIn("frame-src 'none'", Client().get("/data/")["Content-Security-Policy"])


class OutboundTests(TestCase):
    def setUp(self):
        cache.clear()
        netsafety.invalidate()
        seed_demo(reset=True)
        self.c = Client()
        self.c.post("/api/hmi/login", json.dumps({"pin": "9999"}), content_type="application/json")

    def cmd(self, name, p):
        return self.c.post("/api/hmi/cmd", json.dumps({"name": name, "p": p, "line": "Barbara"}), content_type="application/json").json()

    def test_public_addresses_cannot_be_saved(self):
        for k in ("s2kUrl", "peopleUrl", "webhook", "grafana.url"):
            for bad in ("http://8.8.8.8/x", "https://example.com/x", "ftp://192.168.100.5/x", "http://user:pw@192.168.100.5/x",
                        "http://10.0.0.5:8080/x"):
                self.assertFalse(self.cmd("settingSet", {"k": k, "v": bad})["ok"], (k, bad))
            self.assertTrue(self.cmd("settingSet", {"k": k, "v": "http://192.168.100.5:8080/x"})["ok"], k)
        self.assertFalse(self.cmd("lineSet", {"id": "Barbara", "f": "feed.url", "v": "http://8.8.8.8/ppm"})["ok"])
        self.assertFalse(self.cmd("lineSet", {"id": "Barbara", "f": "feed.url", "v": "http://192.168.5.9:1880/barb"})["ok"])
        self.assertTrue(self.cmd("lineSet", {"id": "Barbara", "f": "feed.url", "v": "http://192.168.100.9:1880/barb"})["ok"])

    def test_server_never_connects_to_a_public_address(self):
        with mock.patch.object(integrations._PRIVATE_OPENER, "open") as op, mock.patch.object(integrations._PUBLIC_OPENER, "open") as pub:
            with self.assertRaises(integrations.ConnError):
                integrations.get_json("http://8.8.8.8/mos")
            op.assert_not_called()
            pub.assert_not_called()

    def test_private_requests_skip_any_web_proxy(self):
        with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://8.8.8.8:3128", "HTTPS_PROXY": "http://8.8.8.8:3128"}):
            proxies = [h.proxies for h in integrations._PRIVATE_OPENER.handlers if hasattr(h, "proxies")]
            self.assertFalse(any(proxies))   # no handler routes private requests through a proxy
            self.assertFalse(any(hasattr(h, "http_open") and h.__class__.__name__ == "ProxyHandler" for h in integrations._PRIVATE_OPENER.handlers))

    def test_redirect_off_the_private_network_is_refused(self):
        h = integrations._CheckedRedirect()
        req = mock.Mock(full_url="http://192.168.100.5/mos")
        with self.assertRaises(integrations.ConnError):
            h.redirect_request(req, None, 302, "Found", {}, "http://8.8.8.8/steal")

    def test_paycom_is_the_only_internet_connection(self):
        from django.test import override_settings
        from django.conf import settings
        suite = dict(settings.SUITE, PAYCOM_SID="s", PAYCOM_TOKEN="t")
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = b'{"data": []}'
        with override_settings(SUITE=suite), mock.patch.object(integrations._PUBLIC_OPENER, "open", return_value=resp) as pub:
            integrations._paycom_raw()
            pub.assert_called_once()

    def test_only_private_ranges_can_be_entered(self):
        for bad in ("8.8.8.0/24", "0.0.0.0/0", "192.168.0.0/15", "203.0.113.0/24", "192.0.2.0/24", "fd00::/8", "hello", "", []):
            with self.assertRaises(netsafety.BadNetwork, msg=bad):
                netsafety.parse_networks(bad)
        nets = netsafety.parse_networks("192.168.100.0/24, 10.20.0.0/16; 100.64.0.0/10\n192.168.100.25")
        self.assertEqual([str(n) for n in nets], ["192.168.100.0/24", "10.20.0.0/16", "100.64.0.0/10", "192.168.100.25/32"])
        self.assertEqual(str(netsafety.parse_networks("192.168.100.77/24")[0]), "192.168.100.0/24")

    def test_bad_installer_value_falls_back_to_the_default(self):
        with mock.patch.dict(os.environ, {"PLANT_NETWORKS": "8.8.8.0/24"}):
            netsafety.invalidate()
            self.assertEqual([str(n) for n in netsafety.current()], ["192.168.100.0/24"])
        with mock.patch.dict(os.environ, {"PLANT_NETWORKS": "10.30.0.0/16"}):
            netsafety.invalidate()
            self.assertEqual(netsafety.current(with_source=True)[1], "installer")
        netsafety.invalidate()


class NetworkPageTests(TestCase):
    """Operator Kiosk > Admin > Network."""

    def setUp(self):
        cache.clear()
        netsafety.invalidate()
        seed_demo(reset=True)

    def lead(self, addr="192.168.100.40"):
        c = Client(REMOTE_ADDR=addr)
        self.assertTrue(c.post("/api/hmi/login", json.dumps({"pin": "9999"}), content_type="application/json").json()["ok"])
        return c

    def save(self, c, allowed):
        return c.post("/api/hmi/cmd", json.dumps({"name": "networkSet", "p": {"allowed": allowed}}), content_type="application/json").json()

    def test_page_data_for_shift_leads_only(self):
        c = self.lead()
        r = c.get("/api/hmi/state").json()
        self.assertEqual(r["clientIp"], "192.168.100.40")
        self.assertEqual([a["range"] for a in r["snap"]["network"]["allowed"]], ["192.168.100.0/24"])
        self.assertIn("192.168.100.255", r["snap"]["network"]["allowed"][0]["desc"])
        op = Client(REMOTE_ADDR="192.168.100.41")
        op.post("/api/hmi/login", json.dumps({"pin": "1111"}), content_type="application/json")
        self.assertNotIn("network", op.get("/api/hmi/state").json()["snap"])

    def test_shift_lead_changes_the_network(self):
        c = self.lead()
        with self.captureOnCommitCallbacks(execute=True):
            r = self.save(c, ["192.168.100.0/24", "10.20.0.0/16"])
        self.assertTrue(r["ok"], r)
        self.assertEqual(PlantSettings.objects.get(pk=1).network["allowed"], ["192.168.100.0/24", "10.20.0.0/16"])
        self.assertEqual(Client(REMOTE_ADDR="10.20.3.3").get("/operator/").status_code, 200)
        fw = PlantSettings.objects.get(pk=1).network["firewall"]
        self.assertIsNone(fw["ok"])  # not Windows here
        self.assertIn("Plant network changed", c.get("/api/hmi/state").content.decode())

    def test_operators_cannot_change_it(self):
        c = Client(REMOTE_ADDR="192.168.100.41")
        c.post("/api/hmi/login", json.dumps({"pin": "1111"}), content_type="application/json")
        r = self.save(c, ["10.0.0.0/8"])
        self.assertFalse(r["ok"])
        self.assertEqual([str(n) for n in netsafety.current()], ["192.168.100.0/24"])

    def test_bad_ranges_are_refused_with_a_reason(self):
        c = self.lead()
        for bad, words in ((["8.8.8.0/24"], "not a private"), ([], "at least one"), (["192.168.100.0/24", "bogus"], "not a network"),
                           ("192.168.100.0/24", "list"), ([5], "list")):
            r = self.save(c, bad)
            self.assertFalse(r["ok"], bad)
            self.assertIn(words, r["error"], bad)
        self.assertEqual([str(n) for n in netsafety.current()], ["192.168.100.0/24"])

    def test_a_save_cannot_lock_out_the_screen_making_it(self):
        c = self.lead("192.168.100.40")
        r = self.save(c, ["10.20.0.0/16"])
        self.assertFalse(r["ok"])
        self.assertIn("lock it out", r["error"])
        self.assertEqual([str(n) for n in netsafety.current()], ["192.168.100.0/24"])
        # on the server itself it is allowed, because the server can always get back in
        self.assertTrue(self.save(self.lead("127.0.0.1"), ["10.20.0.0/16"])["ok"])

    def test_firewall_sync_on_windows(self):
        nets = netsafety.parse_networks(["192.168.100.0/24", "10.20.0.0/16"])
        done = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(netsafety.os, "name", "nt"), mock.patch.object(netsafety.subprocess, "run", return_value=done) as run:
            ok, msg = netsafety.sync_firewall(nets)
        self.assertTrue(ok)
        script = run.call_args[0][0][-1]
        self.assertIn("-DisplayName 'Production Suite'", script)
        self.assertIn("-RemoteAddress @('192.168.100.0/24','10.20.0.0/16')", script)
        missing = mock.Mock(returncode=1, stdout="", stderr="Get-NetFirewallRule : No MSFT_NetFirewallRule objects found")
        with mock.patch.object(netsafety.os, "name", "nt"), mock.patch.object(netsafety.subprocess, "run", return_value=missing):
            ok, msg = netsafety.sync_firewall(nets)
        self.assertFalse(ok)
        self.assertIn("Re-run the installer", msg)

    def test_recovery_command(self):
        from django.core.management import call_command
        out = io.StringIO()
        call_command("network", "--set", "10.20.0.0/16", "192.168.100.0/24", stdout=out)
        self.assertEqual([str(n) for n in netsafety.current()], ["10.20.0.0/16", "192.168.100.0/24"])
        out = io.StringIO()
        call_command("network", "--plain", stdout=out)
        self.assertEqual(out.getvalue().split(), ["10.20.0.0/16", "192.168.100.0/24"])
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command("network", "--set", "8.8.8.0/24", stdout=io.StringIO())

    def test_broken_saved_value_falls_back_safely(self):
        PlantSettings.load()
        PlantSettings.objects.filter(pk=1).update(network={"allowed": ["0.0.0.0/0"]})
        netsafety.invalidate()
        self.assertEqual([str(n) for n in netsafety.current()], ["192.168.100.0/24"])
        self.assertEqual(Client(REMOTE_ADDR="8.8.8.8").get("/operator/").status_code, 403)


class InstallerLockTests(TestCase):
    def test_public_database_server_is_refused(self):
        from django.core.management.base import CommandError
        from tests.test_installer import InstallerTests, FakeServer
        t = InstallerTests("test_good_server_writes_settings")
        t.setUp()
        buf = io.StringIO()
        t.run_setup(FakeServer(), {"db_host": "8.8.8.8"}, expect_ok=False, out=buf)
        self.assertIn("public internet", buf.getvalue())
        self.assertFalse(t.env.exists())


class ReadOnlyDatabaseTests(TestCase):
    """The asset and S2K databases are only ever read."""

    def test_connections_open_read_only(self):
        from plant.dbconf import read_only_mysql
        env = {"S2K_DB_HOST": "192.168.100.30", "S2K_DB_NAME": "s2k"}.get
        db = read_only_mysql(lambda k, d="": env(k) or d, "S2K_DB")
        self.assertEqual(db["OPTIONS"]["init_command"], "SET SESSION TRANSACTION READ ONLY")
        self.assertIsNone(read_only_mysql(lambda k, d="": d, "S2K_DB"))

    def test_router_never_writes_or_migrates_elsewhere(self):
        from plant.dbconf import SuiteRouter
        r = SuiteRouter()
        self.assertEqual(r.db_for_write(PlantSettings), "default")
        self.assertTrue(r.allow_migrate("default", "plant"))
        self.assertFalse(r.allow_migrate("s2k", "plant"))
        self.assertFalse(r.allow_migrate("assets", "hmi"))

    def test_only_plain_selects_are_sent(self):
        ok = ["SELECT mo, item, qty FROM open_orders WHERE status = 'update pending'", "WITH o AS (SELECT 1 AS mo) SELECT * FROM o",
              "SELECT REPLACE(item, '-', '') AS item FROM t", "SELECT `delete` FROM t"]
        bad = ["DELETE FROM orders", "SELECT 1; DROP TABLE orders", "SELECT * FROM orders FOR UPDATE", "SELECT * FROM t LOCK IN SHARE MODE",
               "WITH a AS (SELECT 1) UPDATE t SET x = 1", "SELECT * INTO OUTFILE '/tmp/x' FROM t", "SELECT 1 /*!50000 FOR UPDATE */",
               "SELECT SLEEP(100)", "SELECT GET_LOCK('x', 10)", "INSERT INTO t VALUES (1)", "CALL p()"]
        for q in ok:
            integrations.check_select(q)
        for q in bad:
            with self.assertRaises(integrations.ConnError, msg=q):
                integrations.check_select(q)

    def test_s2k_pull_from_database(self):
        cache.clear()
        netsafety.invalidate()
        seed_demo(reset=True)
        from hmi.models import SavedQuery
        from plant.models import MO
        st = PlantSettings.load()
        st.settings["s2kSource"] = "db"
        st.save()
        SavedQuery.objects.create(name="s2k_mos", sql="SELECT 1")
        rows = [{"MO": "990001", "Item": "MG4062HP", "QTY": "2400.000", "due": "2026-11-20", "process": "BARB"}]
        with mock.patch.object(integrations, "run_saved", return_value={"columns": list(rows[0]), "rows": rows, "ms": 1, "truncated": False}) as rs:
            c = Client()
            c.post("/api/hmi/login", json.dumps({"pin": "9999"}), content_type="application/json")
            r = c.post("/api/hmi/cmd", json.dumps({"name": "s2kPull", "p": {}}), content_type="application/json").json()
        self.assertTrue(r["ok"], r)
        self.assertEqual(rs.call_args.kwargs["db"], "s2k")
        mo = MO.objects.get(number="990001")
        self.assertEqual((mo.part, mo.qty, str(mo.due)), ("MG4062HP", 2400, "2026-11-20"))
