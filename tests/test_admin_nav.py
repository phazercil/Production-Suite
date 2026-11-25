"""Admin's sidebar: every page in a group, every address still works, the open page is marked with its breadcrumb and
line, and shift leads see the pages they may change marked as such."""
import json

from django.test import Client, TestCase

from hmi import kadmin
from plant.demo import seed_demo


class Groups(TestCase):
    def test_every_tab_is_in_exactly_one_group_with_a_line(self):
        grouped = [k for _, _, ks in kadmin.GROUPS for k in ks]
        self.assertEqual(sorted(grouped), sorted(k for k, _ in kadmin.TABS))
        self.assertEqual(len(grouped), len(set(grouped)))
        for k, _ in kadmin.TABS:
            self.assertTrue(kadmin.DESC.get(k), k)
        nav, here = kadmin.admin_nav("backup")
        self.assertEqual(here, "System")
        self.assertEqual([i["k"] for g in nav for i in g["items"] if i["on"]], ["backup"])


class Page(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.c = Client(HTTP_HX_REQUEST="true")
        self.c.post("/api/hmi/login", json.dumps({"pin": "9999"}), content_type="application/json")

    def test_every_address_still_opens_with_its_sidebar_breadcrumb_and_line(self):
        for k, label in kadmin.TABS:
            r = self.c.get(f"/kiosk/v/admin?tab={k}")
            self.assertEqual(r.status_code, 200, k)
            html = r.content.decode()
            self.assertIn(f'data-admin-tab="{k}"', html)
            self.assertIn(f'class="adm-i on" aria-current="page" data-adm-item="{k}"', html)
            self.assertIn(f'<b aria-current="page">{label.replace("&", "&amp;")}</b>', html)
            self.assertIn(kadmin.DESC[k].replace("'", "&#x27;")[:30], html)
            self.assertEqual(Client().get(f"/admin/{k}/").status_code, 200, k)       # bookmarks keep working
        html = self.c.get("/kiosk/v/admin?tab=rules").content.decode()
        self.assertIn('hx-push-url="/admin/backup/"', html)
        self.assertIn('data-adm-find', html)
        self.assertIn('href="/operator/team/"', html)                                # people & PINs
        self.assertIn("<span>Plant setup</span>", html)

    def test_not_signed_in_sees_no_sidebar(self):
        r = Client(HTTP_HX_REQUEST="true").get("/kiosk/v/admin?tab=rules").content.decode()
        self.assertNotIn("adm-side", r)
        self.assertIn("Log in to open Admin", r)
