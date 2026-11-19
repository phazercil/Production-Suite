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

    def test_full_screen_button(self):
        self.assertContains(Client().get("/data/"), 'class="suite-fs" data-fullscreen')                 # in the top bar
        self.assertContains(Client().get("/data/?view=1"), 'class="suite-fs float" data-fullscreen')    # the corner, no bar
        self.assertContains(Client().get("/data/"), "suite/fullscreen.js")

    def test_one_machine_and_view_only_link(self):
        r = Client().get("/data/salvatore/")                    # any letter case
        self.assertContains(r, 'id="live-salvatore"')
        self.assertNotContains(r, 'id="live-barbara"')
        self.assertContains(r, 'hx-get="/data/r/board?line=Salvatore"')
        self.assertContains(r, "http://testserver/data/Salvatore/?view=1")   # the link to copy
        self.assertContains(r, 'class="suitebar"')
        self.assertEqual(Client().get("/data/Nope/").status_code, 404)
        v = Client().get("/data/Salvatore/?view=1")
        self.assertNotContains(v, 'class="suitebar"')            # no menu
        self.assertNotContains(v, "dk-pick")                      # no line picker or link box
        self.assertNotContains(v, "Operator Kiosk › Admin")       # no links into the suite
        self.assertContains(v, "view only")
        self.assertContains(v, 'hx-get="/data/r/board?line=Salvatore&amp;view=1"')
        b = Client().get("/data/r/board?line=Salvatore&view=1")
        self.assertContains(b, 'id="live-salvatore"')
        self.assertNotContains(b, 'id="live-barbara"')
        self.assertNotContains(b, "Grafana is not connected")
        self.assertContains(Client().get("/data/?view=1"), 'id="live-barbara"')   # every line, view only

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


class ChartWindowTests(TestCase):
    """The run screen's speed chart: an operator sees the whole shift plus the last half of the shift before it; a
    shift lead or admin sees the whole day. A gap with no readings is not joined across."""
    def test_fixed_window_and_gaps(self):
        t = now()
        pts = [(t - timedelta(minutes=60 - i), 100) for i in range(10)] + [(t - timedelta(minutes=20 - i), 120) for i in range(10)]
        html = svg.speed_chart(pts, 180, start=t - timedelta(hours=4), end=t + timedelta(hours=4), now=t,
                               marks=[(t - timedelta(hours=1), "1st shift")], shade=(t - timedelta(hours=4), t - timedelta(hours=1)))
        self.assertEqual(html.count('class="c-line"'), 2)    # two runs: the 30-minute gap is left empty
        for cls in ("c-now", "c-mark", "c-shade", "1st shift"):
            self.assertIn(cls, html)
        self.assertIn("No readings", svg.speed_chart([], 180, empty="No readings yet", start=t, end=t + timedelta(hours=8)))

    def test_hour_lines_and_dotted_half_hours(self):
        from django.utils import timezone
        start = timezone.localtime(now()).replace(hour=7, minute=0, second=0, microsecond=0)
        html = svg.speed_chart([(start + timedelta(minutes=5), 100)], 180, start=start, end=start + timedelta(hours=8))
        self.assertEqual(html.count('class="c-hour"'), 7)    # 8am … 2pm (the chart's two edges have none)
        self.assertEqual(html.count('class="c-half"'), 8)    # 7:30 … 2:30
        self.assertIn("stroke-dasharray", open("static/suite/shell.css").read())

    def test_operator_and_lead_windows(self):
        from django.test import Client

        from plant.demo import seed_demo
        seed_demo(reset=True)
        c = Client(HTTP_HX_REQUEST="true")
        c.get("/station/Barbara/")
        c.post("/kiosk/do/login", {"pin": "1111"}, HTTP_X_CSRFTOKEN=c.cookies["csrftoken"].value)
        r = c.get("/kiosk/v/controls").content.decode()
        self.assertIn("Line rate · this shift", r)
        self.assertIn("c-shade", r)
        c.post("/kiosk/do/logout", HTTP_X_CSRFTOKEN=c.cookies["csrftoken"].value)
        c.post("/kiosk/do/login", {"pin": "9999"}, HTTP_X_CSRFTOKEN=c.cookies["csrftoken"].value)
        r = c.get("/kiosk/v/controls").content.decode()
        self.assertIn("Line rate · today", r)
        self.assertNotIn("c-shade", r)
        self.assertIn("1st shift", r)


class DowntimeScopeTests(TestCase):
    """An operator's Downtime page, alert and badge cover their own shift's stops; earlier shifts' stops without a
    reason are for shift leads and admins."""
    def test_earlier_shifts_are_for_leads(self):
        from hmi.models import Downtime, ShiftRun
        seed_demo(reset=True)
        cur = ShiftRun.objects.get(line="Barbara", active=True)
        old = ShiftRun.objects.create(line="Barbara", machine=cur.machine, user_name="M. Lee", shift_name="2nd shift", active=False,
                                      start=cur.start - timedelta(hours=12), end=cur.start - timedelta(hours=4))
        t = old.start + timedelta(hours=2)
        Downtime.objects.create(line="Barbara", shift=old, start_time=t, end_time=t + timedelta(minutes=33), minutes=33)
        c = Client(HTTP_HX_REQUEST="true")
        c.get("/station/Barbara/")
        tok = c.cookies["csrftoken"].value
        c.post("/kiosk/do/login", {"pin": "1111"}, HTTP_X_CSRFTOKEN=tok)
        r = c.get("/kiosk/v/downtime").content.decode()
        self.assertNotIn("Earlier shifts", r)
        self.assertNotIn(">33<", r)
        c.post("/kiosk/do/logout", HTTP_X_CSRFTOKEN=tok)
        c.post("/kiosk/do/login", {"pin": "9999"}, HTTP_X_CSRFTOKEN=tok)
        r = c.get("/kiosk/v/downtime").content.decode()
        self.assertNotIn("Earlier shifts", r)                     # an admin on the operator screens: this shift too
        r = c.get("/kiosk/v/admin?tab=reasons").content.decode()
        self.assertIn("Earlier shifts", r)                        # earlier shifts' are on the Admin page
        self.assertIn(">33<", r)
