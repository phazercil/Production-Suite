"""The thin-client screens: built on the server, re-loaded part by part when the plant changes."""
from datetime import timedelta

from django.core.cache import cache
from django.test import Client, TestCase

from hmi import engine
from plant.demo import seed_demo
from plant.models import Machine, PlantSettings
from plant.util import now
from suite import svg

from .test_suite import no_planned_time


class WatchTests(TestCase):
    def setUp(self):
        cache.clear()
        seed_demo(reset=True)

    def test_data_and_config_change_separately(self):
        c = Client()
        a = c.get("/thin/watch").json()
        PlantSettings.bump()
        b = c.get("/thin/watch").json()
        self.assertEqual(b["data"], a["data"] + 1)
        self.assertEqual(b["config"], a["config"])
        Machine.objects.filter(name="Barbara").update(color="#000000")
        self.assertNotEqual(c.get("/thin/watch").json()["config"], a["config"])
        self.assertEqual(c.get("/thin/watch")["Cache-Control"], "no-store")


class DataKioskTests(TestCase):
    def setUp(self):
        no_planned_time(self)
        cache.clear()
        seed_demo(reset=True)

    def test_page_is_built_on_the_server(self):
        r = Client().get("/data/")
        self.assertContains(r, 'data-watch="/thin/watch?scope=data"')
        self.assertContains(r, "htmx.min.js")
        self.assertNotContains(r, "datakiosk.js")
        self.assertContains(r, 'id="live-barbara"')
        self.assertContains(r, "class=\"chartbox\"")
        self.assertContains(r, "Grafana is not connected")

    def test_parts_follow_the_plant(self):
        bar = Machine.objects.get(name="Barbara")
        engine.tick(bar, {"ppm": 237}, now())
        r = Client().get("/data/r/live?line=Barbara")
        self.assertContains(r, "<b>237</b>", html=False)
        self.assertContains(r, 'hx-trigger="suite:changed from:body"')
        self.assertEqual(Client().get("/data/r/live?line=Nope").status_code, 404)
        self.assertEqual(Client().get("/data/r/nope").status_code, 404)

    def test_rotation_and_grafana_panels_are_decided_by_the_server(self):
        st = PlantSettings.load()
        st.settings["grafana"].update(on=True, url="http://192.168.100.40:3000", uid="pleat")
        st.settings["kiosk"].update(rotate=20)
        st.save()
        r = Client().get("/data/r/board")
        self.assertContains(r, "every 20s")
        self.assertContains(r, "/d-solo/pleat/", count=3)   # one line on screen at a time, three panels
        self.assertNotContains(r, "Grafana is not connected")


class ChartTests(TestCase):
    def test_speed_chart(self):
        t = now()
        html = svg.speed_chart([(t - timedelta(minutes=10 - i), 100 + i) for i in range(10)], 180, label="<b>x</b>")
        self.assertIn('class="chartbox"', html)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", html)
        self.assertIn("vector", open("static/suite/shell.css").read())
        self.assertIn("No readings", svg.speed_chart([], 180))
