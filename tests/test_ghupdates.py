"""Updates from GitHub: finding the newest release's update file, downloading it safely, handing it to the update checks."""
import hashlib
import io
import json
import urllib.request
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase, override_settings

from plant import ghupdates, updates
from plant.demo import seed_demo

FILE = b"PK fake update file"
SHA = hashlib.sha256(FILE).hexdigest()


def release(tag, rev, extra=(), digest=SHA, draft=False):
    assets = [{"id": rev, "name": f"ProductionSuite-Update-1.{rev}.0-r{rev}.zip", "size": len(FILE), "digest": f"sha256:{digest}"},
              {"id": 900 + rev, "name": "ProductionSuite-Setup.zip", "size": 9}, *extra]
    return {"tag_name": tag, "name": tag, "draft": draft, "prerelease": False, "body": "notes", "published_at": "2026-11-05T10:00:00Z",
            "html_url": "https://github.com/x/y", "assets": assets}


class FakeGitHub:
    def __init__(self, releases, body=FILE):
        self.releases, self.body, self.calls = releases, body, []

    def __call__(self, path, token, accept="application/vnd.github+json", timeout=20):
        self.calls.append((path, token, accept))
        if path.startswith("/repos/") and "/releases?" in path:
            return io.BytesIO(json.dumps(self.releases).encode())
        if "/releases/assets/" in path:
            return io.BytesIO(self.body)
        raise AssertionError(path)


CFG = {"repo": "phazercil/Production-Suite", "token": "github_pat_" + "x" * 30, "daily": True}


class GitHubUpdateTests(TestCase):
    def setUp(self):
        cache.clear()
        seed_demo(reset=True)
        import shutil
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        for p in (mock.patch.object(updates, "current", return_value=("1.7.1", 10)), mock.patch.object(updates, "base", return_value=tmp)):
            p.start()
            self.addCleanup(p.stop)

    def test_picks_the_newest_update_file_and_prefers_packages(self):
        pk = {"id": 77, "name": "ProductionSuite-Update-1.12.0-r12-with-packages.zip", "size": 5, "digest": ""}
        gh = FakeGitHub([release("v1.11", 11), release("v1.12", 12, [pk]), release("v1.13", 13, draft=True)])
        with mock.patch.object(ghupdates, "_open", gh):
            c = ghupdates.check(CFG)
        self.assertTrue(c["newer"])
        self.assertEqual(c["release"]["tag"], "v1.12")
        self.assertEqual(c["release"]["asset"]["name"], pk["name"])   # drafts are skipped; the packages file always installs
        self.assertEqual(ghupdates.last()["release"]["tag"], "v1.12")

    def test_up_to_date(self):
        with mock.patch.object(ghupdates, "_open", FakeGitHub([release("v1.10", 10)])):
            self.assertFalse(ghupdates.check(CFG)["newer"])
            with self.assertRaisesMessage(ghupdates.GHError, "Nothing newer"):
                ghupdates.install("Admin", cfg=CFG)

    def test_download_goes_through_the_normal_update_checks(self):
        gh = FakeGitHub([release("v1.11", 11)])
        got = {}

        def receive(path, name, by, when, at):
            got.update(data=open(path, "rb").read(), name=name, by=by, when=when)
            return {"version": "1.11.0", "revision": 11}
        with mock.patch.object(ghupdates, "_open", gh), mock.patch.object(updates, "receive", receive):
            m = ghupdates.install("Plant Admin", "idle", cfg=CFG)
        self.assertEqual(m["revision"], 11)
        self.assertEqual(got["data"], FILE)
        self.assertEqual((got["by"], got["when"]), ("Plant Admin", "idle"))
        self.assertIn("v1.11", got["name"])
        self.assertEqual(gh.calls[-1][2], "application/octet-stream")
        self.assertFalse((updates.root() / "run" / "github-download.tmp").exists())

    def test_a_download_that_doesnt_match_githubs_checksum_is_refused(self):
        with mock.patch.object(ghupdates, "_open", FakeGitHub([release("v1.11", 11, digest="0" * 64)])), \
                mock.patch.object(updates, "receive") as receive:
            with self.assertRaisesMessage(ghupdates.GHError, "checksum"):
                ghupdates.install("Admin", cfg=CFG)
        receive.assert_not_called()

    def test_an_unsigned_file_is_refused_by_the_update_checks(self):
        with mock.patch.object(ghupdates, "_open", FakeGitHub([release("v1.11", 11)])):
            with self.assertRaises(ghupdates.GHError):   # not a signed update file: plant.updates refuses it
                ghupdates.install("Admin", cfg=CFG)
        self.assertNotEqual(updates.state().get("status"), "ready")

    def test_redirects_stay_on_github_and_drop_the_token(self):
        h = ghupdates._Redirect()
        req = urllib.request.Request("https://api.github.com/repos/x/y/releases/assets/1", headers={"Authorization": "Bearer t"})
        new = h.redirect_request(req, None, 302, "Found", {}, "https://release-assets.githubusercontent.com/f?sig=1")
        self.assertNotIn("Authorization", new.headers)
        with self.assertRaisesMessage(ghupdates.GHError, "unexpected"):
            h.redirect_request(req, None, 302, "Found", {}, "https://evil.example.com/f")
        with self.assertRaises(ghupdates.GHError):
            h.redirect_request(req, None, 302, "Found", {}, "http://objects.githubusercontent.com/f")

    def test_daily_check_posts_one_notice(self):
        from datetime import timedelta

        from django.utils import timezone

        from hmi.models import Notice
        t = timezone.now()
        with override_settings(SUITE={**__import__("django.conf").conf.settings.SUITE, "GITHUB_UPDATES_TOKEN": CFG["token"], "GITHUB_UPDATES_CHECK": "1"}), \
                mock.patch("plant.liveconfig.refresh"), mock.patch.object(ghupdates, "_open", FakeGitHub([release("v1.11", 11)])):
            ghupdates.daily(t)
            ghupdates.daily(t + timedelta(minutes=5))       # not again within the day
            ghupdates.daily(t + timedelta(hours=25))        # a day later: same release, no second notice
        self.assertEqual(Notice.objects.filter(kind="update").count(), 1)
        self.assertIn("v1.11.0 rev 11", Notice.objects.get(kind="update").text)

    def test_updates_tab_and_admin_only(self):
        c = Client(HTTP_HX_REQUEST="true")
        c.post("/kiosk/do/login", {"pin": "4444"})          # a shift lead can't set it up
        r = c.post("/kiosk/do/ghSave", {"repo": "a/b", "token": "github_pat_" + "y" * 30})
        self.assertContains(r, "<b>Updates</b>")
        c.post("/kiosk/do/logout")
        c.post("/kiosk/do/login", {"pin": "9999"})
        r = c.get("/kiosk/v/admin?tab=updates")
        self.assertContains(r, "Updates from GitHub")
        self.assertContains(r, "NOT SET UP")
        with mock.patch.object(ghupdates, "_open", FakeGitHub([release("v1.11", 11)])), mock.patch("plant.liveconfig.save") as save:
            r = c.post("/kiosk/do/ghSave", {"repo": "phazercil/Production-Suite", "token": "github_pat_" + "y" * 30, "daily": "1"})
        self.assertContains(r, "The token works")
        saved = save.call_args[0][0]
        self.assertEqual(saved["GITHUB_UPDATES_CHECK"], "1")
        self.assertTrue(saved["GITHUB_UPDATES_TOKEN"].startswith("github_pat_"))
        self.assertNotContains(r, "y" * 30)                   # the token never goes back to the screen
        r = c.post("/kiosk/do/ghSave", {"repo": "not a repo", "token": "github_pat_" + "y" * 30})
        self.assertContains(r, "owner/name")
