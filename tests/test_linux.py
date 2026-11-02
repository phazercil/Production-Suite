"""Linux install behind Apache: the visitor's address as Apache reports it (and only Apache), the root helper
that writes Apache's allow list and firewalld rules, and handing plant-network changes to it."""
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import Client, SimpleTestCase, override_settings

from plant import middleware, netsafety

from .test_platform import Base

sys.path.insert(0, str(Path(settings.BASE_DIR) / "tools"))
import linux_apply  # noqa: E402


def suite_with(**kw):
    return {**settings.SUITE, **kw}


class ForwardedAddressTests(SimpleTestCase):
    def test_only_from_apache_on_this_server(self):
        with override_settings(SUITE=suite_with(BEHIND_PROXY=False)):
            self.assertIsNone(middleware.forwarded_client({"REMOTE_ADDR": "127.0.0.1", "HTTP_X_FORWARDED_FOR": "192.168.100.5"}))
        with override_settings(SUITE=suite_with(BEHIND_PROXY=True)):
            f = middleware.forwarded_client
            self.assertEqual(f({"REMOTE_ADDR": "127.0.0.1", "HTTP_X_FORWARDED_FOR": "192.168.100.5"}), "192.168.100.5")
            # Apache appends the real address: a value a client typed in front of it is never used
            self.assertEqual(f({"REMOTE_ADDR": "127.0.0.1", "HTTP_X_FORWARDED_FOR": "192.168.100.9, 8.8.8.8"}), "8.8.8.8")
            # not from this server (can't happen: the suite listens on 127.0.0.1 only), so not trusted
            self.assertIsNone(f({"REMOTE_ADDR": "192.168.100.7", "HTTP_X_FORWARDED_FOR": "192.168.100.5"}))
            self.assertIsNone(f({"REMOTE_ADDR": "127.0.0.1"}))  # a program on the server itself
            self.assertEqual(f({"REMOTE_ADDR": "127.0.0.1", "HTTP_X_FORWARDED_FOR": "not-an-address"}), "invalid")


class BehindApacheRequestTests(Base):
    def test_plant_network_check_uses_apaches_address(self):
        with override_settings(SUITE=suite_with(BEHIND_PROXY=True)):
            ok = Client(REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR="192.168.100.40").get("/api/version")
            self.assertEqual(ok.status_code, 200)
            refused = Client(REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR="10.77.0.5").get("/api/version")
            self.assertEqual(refused.status_code, 403)
            self.assertIn("10.77.0.5", refused.content.decode())
            self.assertEqual(Client(REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR="junk").get("/api/version").status_code, 403)
            self.assertEqual(Client(REMOTE_ADDR="127.0.0.1").get("/api/version").status_code, 200)  # the server itself
        # without the Apache setting, a forwarded address is ignored (Windows, or someone faking it)
        self.assertEqual(Client(REMOTE_ADDR="10.77.0.5", HTTP_X_FORWARDED_FOR="192.168.100.40").get("/api/version").status_code, 403)

    def test_network_changes_are_handed_to_the_root_helper(self):
        d = Path(tempfile.mkdtemp())
        unit = d / "production-suite-network.path"
        unit.write_text("x")
        with override_settings(BASE_DIR=d), mock.patch.object(netsafety, "LINUX_NETWORK_UNIT", unit), mock.patch.object(netsafety.os, "name", "posix"):
            def helper():  # what systemd + linux_apply.py do
                req = d / "updates" / "network-request.json"
                for _ in range(40):
                    if req.exists():
                        nets = json.loads(req.read_text())["networks"]
                        req.unlink()
                        (d / "updates" / "network-result.json").write_text(json.dumps({"ok": True, "msg": "Apache allows " + ", ".join(nets)}))
                        return
                    time.sleep(0.1)
            t = threading.Thread(target=helper)
            t.start()
            ok, msg = netsafety.sync_firewall(netsafety.parse_networks("192.168.100.0/24, 10.5.0.0/16"))
            t.join()
            self.assertEqual((ok, msg), (True, "Apache allows 192.168.100.0/24, 10.5.0.0/16"))


class RootHelperTests(SimpleTestCase):
    def test_only_private_ranges_reach_apache_and_firewalld(self):
        self.assertEqual(linux_apply.private_nets(["192.168.100.0/24", "8.8.8.0/24", "10.1.2.3", "bad", "0.0.0.0/0", "172.16.5.0/24"]),
                         ["192.168.100.0/24", "10.1.2.3/32", "172.16.5.0/24"])
        self.assertEqual(linux_apply.private_nets("192.168.100.0/24"), [])  # not a list: nothing

    def test_allow_list_file(self):
        d = Path(tempfile.mkdtemp())
        inc = d / "allow.inc"
        with mock.patch.object(linux_apply, "ALLOW_INC", inc), mock.patch.object(linux_apply, "run", return_value=(0, "")):
            ok, msg = linux_apply.apache_include(["192.168.100.0/24", "10.5.0.0/16"])
        self.assertTrue(ok)
        self.assertEqual(inc.read_text().splitlines()[1:], ["Require local", "Require ip 192.168.100.0/24", "Require ip 10.5.0.0/16"])

    def test_writes_never_follow_a_planted_link(self):
        d = Path(tempfile.mkdtemp())
        victim = d / "victim"
        victim.write_text("keep")
        (d / "result.json").symlink_to(victim)
        linux_apply.safe_write(d / "result.json", "{}")
        self.assertEqual(victim.read_text(), "keep")
        self.assertEqual((d / "result.json").read_text(), "{}")
        self.assertFalse((d / "result.json").is_symlink())
