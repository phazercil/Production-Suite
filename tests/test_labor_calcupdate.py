"""Labor Calculator updates from its own GitHub repository (Admin › Updates › Labor Calculator)."""
import gzip
import io
import json
import shutil
import tempfile
import urllib.parse
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.test import Client, TestCase, override_settings

from labor import calcupdate
from plant import ghupdates
from plant.demo import seed_demo

CALC = gzip.open(Path(__file__).parent / "fixtures" / "labor_calc_offline.html.gz", "rt", encoding="utf-8").read()
SHA1, SHA2 = "a" * 40, "b" * 40
TOKEN = "github_pat_" + "x" * 30


def commit(sha, date, msg):
    return [{"sha": sha, "commit": {"message": msg + "\n\nmore", "committer": {"date": date}}}]


class FakeGitHub:
    """api.github.com for the calculator's repository: files[(path, sha)] = text."""

    def __init__(self, files, commits):
        self.files, self.commits, self.calls = files, commits, []

    def __call__(self, path, token, accept="application/vnd.github+json", timeout=20):
        self.calls.append(path)
        u = urllib.parse.urlparse(path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path == "/repos/phazercil/Labor-Calculator":
            return io.BytesIO(json.dumps({"default_branch": "main"}).encode())
        if u.path.endswith("/commits"):
            return io.BytesIO(json.dumps(self.commits.get(q["path"], [])).encode())
        if "/contents/" in u.path:
            name = urllib.parse.unquote(u.path.split("/contents/", 1)[1])
            assert accept == "application/vnd.github.raw"
            if (name, q["ref"]) not in self.files:
                raise ghupdates.GHError("GitHub answered 404 Not Found.")
            return io.BytesIO(self.files[(name, q["ref"])].encode())
        raise AssertionError(path)


def gh_v2(changed=False):
    files = {("desktop/app/index.html", SHA1): CALC, ("desktop/app/index.html", SHA2): CALC.replace("</body>", "<!-- v2 --></body>")}
    commits = {"desktop/app/index.html": commit(SHA2, "2026-11-01T10:00:00Z", "Calculator v1.2.0"),
               "Labor_Calculator.html": commit(SHA2 if not changed else "c" * 40, "2026-11-03T10:00:00Z" if changed else "2026-11-01T10:00:00Z", "edit")}
    return FakeGitHub(files, commits)


class CalcUpdateTests(TestCase):
    def setUp(self):
        cache.clear()
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        for p in (mock.patch.object(calcupdate, "folder", return_value=tmp / "labor-calc"), mock.patch("plant.liveconfig.refresh"),
                  override_settings(SUITE={**settings.SUITE, "GITHUB_UPDATES_TOKEN": TOKEN})):
            p.start() if hasattr(p, "start") else p.enable()
            self.addCleanup(p.stop if hasattr(p, "stop") else p.disable)
        self.tmp = tmp / "labor-calc"

    def test_check_install_undo_reset(self):
        self.assertEqual(calcupdate.page_path(), calcupdate.SHIPPED)
        gh = gh_v2()
        with mock.patch.object(ghupdates, "_open", gh):
            latest = calcupdate.check()
            self.assertEqual((latest["sha"], latest["message"], latest["behind"]), (SHA2, "Calculator v1.2.0", False))
            m = calcupdate.install("Admin")
        self.assertEqual(m["sha"], SHA2)
        page = calcupdate.page_path()
        self.assertEqual(page, self.tmp / "labor.html")
        html = page.read_text(encoding="utf-8")
        self.assertIn("<!-- v2 -->", html)
        self.assertIn("LaborSync", html)                   # the suite's changes are in
        self.assertNotIn("cdn.tailwindcss", html)
        # undo with nothing from GitHub before it: back to the shipped page
        calcupdate.undo()
        self.assertEqual(calcupdate.page_path(), calcupdate.SHIPPED)
        # two installs, then undo goes back one
        with mock.patch.object(ghupdates, "_open", gh):
            calcupdate.install("Admin", SHA1)
            calcupdate.install("Admin", SHA2)
        self.assertEqual(calcupdate.info()["installed"]["sha"], SHA2)
        self.assertEqual(calcupdate.undo()["sha"], SHA1)
        self.assertNotIn("<!-- v2 -->", calcupdate.page_path().read_text(encoding="utf-8"))
        calcupdate.reset()
        self.assertEqual(calcupdate.page_path(), calcupdate.SHIPPED)
        self.assertIsNone(calcupdate.info()["installed"])

    def test_people_and_pay_are_stripped(self):
        dirty = CALC.replace('"projectName": ""', '"projectName": "Secret plan"', 1)
        self.assertIn("Secret plan", dirty)
        gh = FakeGitHub({("desktop/app/index.html", SHA1): dirty}, {})
        with mock.patch.object(ghupdates, "_open", gh):
            calcupdate.install("Admin", SHA1)
        self.assertNotIn("Secret plan", calcupdate.page_path().read_text(encoding="utf-8"))
        self.assertNotIn("Secret plan", (self.tmp / "calc.html").read_text(encoding="utf-8"))

    def test_refuses_a_calculator_that_does_not_fit(self):
        broken = CALC.replace("localStorage.setItem(budgetKey(activeBudget()), JSON.stringify(state));", "saveEverything();")
        online = CALC.replace('id="offline-assets"', 'id="x"')
        for files in ({("desktop/app/index.html", SHA1): broken}, {("Labor_Calculator.html", SHA1): online}):
            with mock.patch.object(ghupdates, "_open", FakeGitHub(files, {})), self.assertRaises(calcupdate.CalcError):
                calcupdate.install("Admin", SHA1)
            self.assertEqual(calcupdate.page_path(), calcupdate.SHIPPED)
        with self.assertRaises(calcupdate.CalcError):
            calcupdate.install("Admin", "not-a-sha")

    def test_warns_when_the_offline_build_is_behind(self):
        with mock.patch.object(ghupdates, "_open", gh_v2(changed=True)):
            self.assertTrue(calcupdate.check()["behind"])

    def test_needs_the_github_token(self):
        with override_settings(SUITE={**settings.SUITE, "GITHUB_UPDATES_TOKEN": ""}), self.assertRaises(calcupdate.CalcError):
            calcupdate.check()

    def test_rebuilt_after_a_suite_update_or_falls_back(self):
        with mock.patch.object(ghupdates, "_open", gh_v2()):
            calcupdate.install("Admin", SHA2)
        meta = json.loads((self.tmp / "meta.json").read_text())
        meta["revision"] = -1                                # made by an older suite
        (self.tmp / "meta.json").write_text(json.dumps(meta))
        (self.tmp / "labor.html").write_text("old build")
        self.assertIn("LaborSync", calcupdate.page_path().read_text(encoding="utf-8"))
        meta = json.loads((self.tmp / "meta.json").read_text())
        meta["revision"] = -1
        (self.tmp / "meta.json").write_text(json.dumps(meta))
        (self.tmp / "calc.html").write_text("no longer fits")
        self.assertEqual(calcupdate.page_path(), calcupdate.SHIPPED)
        self.assertTrue(calcupdate.info()["installed"]["stale"])

    def test_labor_page_serves_the_installed_calculator(self):
        seed_demo(reset=True)
        c = Client()
        c.post("/api/hmi/login", '{"pin": "9999"}', content_type="application/json")
        if True:
            with mock.patch.object(ghupdates, "_open", gh_v2()):
                calcupdate.install("Admin", SHA2)
            self.assertContains(c.get("/labor/"), "<!-- v2 -->")
            calcupdate.reset()
            self.assertNotContains(c.get("/labor/"), "<!-- v2 -->")

    def test_admin_panel(self):
        seed_demo(reset=True)
        c = Client(HTTP_HX_REQUEST="true")
        c.post("/kiosk/do/login", {"pin": "4444"})          # a shift lead can't
        r = c.post("/kiosk/do/calcCheck")
        self.assertNotContains(r, "laborcalc")
        c.post("/kiosk/do/logout")
        c.post("/kiosk/do/login", {"pin": "9999"})
        r = c.get("/kiosk/v/admin?tab=updates")
        self.assertContains(r, "Labor Calculator")
        self.assertContains(r, "CAME WITH THE SUITE")
        with mock.patch.object(ghupdates, "_open", gh_v2()):
            r = c.post("/kiosk/do/calcCheck")
            self.assertContains(r, "Calculator v1.2.0")
            r = c.post("/kiosk/do/calcInstall", {"sha": SHA2})
        self.assertContains(r, "FROM GITHUB bbbbbbb")
        self.assertContains(r, "Up to date")
        r = c.post("/kiosk/do/calcUndo")
        self.assertContains(r, "CAME WITH THE SUITE")
