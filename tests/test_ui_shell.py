"""The app bar: workflow groups for what each person may open, the plant clock, the warning when machine data
stops being read, and the shared status badges."""
import json
from datetime import timedelta

from django.template import Context, Template
from django.test import Client, TestCase
from django.utils import timezone

from plant.context import GROUPS, MODES, groups_for
from plant.demo import seed_demo
from plant.models import PlantSettings


class Groups(TestCase):
    def test_every_mode_is_in_a_group_and_nothing_is_lost(self):
        grouped = {k for _, _, ks in GROUPS for k in ks}
        self.assertEqual(grouped, {m[0] for m in MODES})
        out = groups_for(MODES, "scheduler")
        self.assertEqual(sum(len(g["items"]) for g in out), len(MODES))
        planning = next(g for g in out if g["key"] == "planning")
        self.assertTrue(planning["on"])
        # a mode in no group still gets an entry
        extra = groups_for(MODES + [("new", "/new/", "New thing")])
        self.assertEqual(extra[-1]["items"], [("new", "/new/", "New thing")])
        # a group with nothing this person may open is left out
        self.assertNotIn("admin", [g["key"] for g in groups_for([m for m in MODES if m[0] != "admin"])])


class Bar(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.c = Client()

    def login(self, pin):
        self.c.post("/api/hmi/login", json.dumps({"pin": pin}), content_type="application/json")

    def test_groups_clock_and_links_keep_their_addresses(self):
        r = self.c.get("/scheduler/")
        html = r.content.decode()
        for href in ("/operator/", "/packaging/", "/stations/", "/scheduler/", "/supervisor/", "/quality/", "/maintenance/"):
            self.assertIn(f'href="{href}"', html)
        self.assertIn("suitegroup", html)
        self.assertIn('data-clock', html)
        self.assertIn('<summary aria-current="page">Planning', html)

    def test_warning_only_for_admins_and_supervisors_when_the_ticker_stopped(self):
        PlantSettings.objects.filter(pk=1).update(ticker_seen=timezone.now() - timedelta(minutes=12))
        self.assertNotContains(self.c.get("/scheduler/"), "suitewarn")          # nobody signed in
        self.login("9999")
        r = self.c.get("/scheduler/")
        self.assertContains(r, "the minute ticker last ran 12 minutes ago")
        self.assertContains(r, 'href="/admin/health/"')
        PlantSettings.objects.filter(pk=1).update(ticker_seen=timezone.now())
        self.assertNotContains(self.c.get("/scheduler/"), 'class="suitewarn"')


class Stat(TestCase):
    def test_word_and_icon_not_colour_alone(self):
        out = Template('{% load suite %}{% stat "stale" %}|{% stat "bogus" %}|{% stat "ok" "Connected" %}').render(Context())
        self.assertIn('class="sbadge sbadge-stale"><i aria-hidden="true">◷</i>STALE', out)
        self.assertIn('sbadge-na"><i aria-hidden="true">?</i>NOT AVAILABLE', out)
        self.assertIn("✓</i>Connected", out)
