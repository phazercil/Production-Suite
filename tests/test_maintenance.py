"""Maintenance tickets with pictures of what is wrong, and the errors machines report during downtime."""
import io
import json
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client, TestCase
from django.utils import timezone

from hmi import machineerrors, photos, ticker
from hmi.models import Downtime, LineState, MachineError, ShiftRun, Ticket, TicketPhoto
from plant.demo import seed_demo
from plant.models import Machine
from plant.util import now

from .test_machinedata import fake_table
from .test_suite import no_planned_time

def picture(w=2000, h=1500, fmt="JPEG", orientation=None, color=(200, 120, 40)):
    """A real picture, as a tablet camera would send it."""
    from PIL import Image
    im = Image.new("RGB", (w, h), color)
    im.paste((20, 20, 20), (0, 0, w // 4, h // 4))   # dark corner, top-left as taken
    out = io.BytesIO()
    kw = {}
    if orientation:
        ex = Image.Exif()
        ex[0x0112] = orientation              # EXIF orientation
        ex[0x8825] = {2: (40.0, 26.0, 1.0)}  # a GPS position the camera added
        kw["exif"] = ex.tobytes()
    im.save(out, fmt, **kw)
    return out.getvalue()


JPEG = picture()


class PhotoCheckTests(TestCase):
    def test_kept_copy_is_upright_small_and_has_no_camera_metadata(self):
        from PIL import Image
        data, thumb, w, h = photos.process(picture(4000, 3000, orientation=6))   # taken sideways
        self.assertEqual((w, h), (1200, 1600))                                  # turned upright, 1600 px long side
        im = Image.open(io.BytesIO(data))
        self.assertEqual((im.format, im.size), ("JPEG", (1200, 1600)))
        self.assertFalse(im.getexif())                                         # no GPS, no orientation left
        self.assertLessEqual(max(Image.open(io.BytesIO(thumb)).size), photos.THUMB_SIDE)
        self.assertLess(len(data), 400_000)

    def test_png_with_transparency_and_small_pictures(self):
        from PIL import Image
        im = Image.new("RGBA", (300, 200), (0, 0, 0, 0))
        out = io.BytesIO()
        im.save(out, "PNG")
        data, _, w, h = photos.process(out.getvalue())
        self.assertEqual((w, h), (300, 200))   # never enlarged
        self.assertEqual(Image.open(io.BytesIO(data)).getpixel((5, 5)), (255, 255, 255))

    def test_pictures_only(self):
        for bad in (b"", b"<html><script>alert(1)</script>", b"\xff\xd8\xff" + b"\x00" * 10, b"%PDF-1.4 ..."):
            with self.assertRaises(photos.PhotoError):
                photos.process(bad)
        with mock.patch.object(photos, "MAX_PIXELS", 1000):
            with self.assertRaises(photos.PhotoError):   # a "decompression bomb" is refused before it is decoded
                photos.process(picture(200, 200))


class TicketPhotoTests(TestCase):
    def setUp(self):
        cache.clear()
        seed_demo(reset=True)
        self.c = Client()

    def login(self, pin):
        return self.c.post("/api/hmi/login", json.dumps({"pin": pin}), content_type="application/json").json()

    def cmd(self, name, p):
        return self.c.post("/api/hmi/cmd", json.dumps({"name": name, "p": p, "line": "Barbara"}), content_type="application/json").json()

    def upload(self, ticket, data=JPEG, c=None):
        return (c or self.c).post("/api/hmi/ticket-photo", {"ticket": ticket, "photo": SimpleUploadedFile("p.jpg", data, "image/jpeg")})

    def test_ticket_with_pictures(self):
        self.assertEqual(self.upload(1).status_code, 403)  # log in first
        self.assertTrue(self.login("1111")["ok"])
        r = self.cmd("ticketAdd", {"machine": "Barbara", "type": "Pleater jam", "priority": "High", "notes": "Blade chipped"})
        tid = r["data"]["id"]
        r = self.upload(tid)
        self.assertTrue(r.json()["ok"], r.json())
        self.assertEqual(self.upload(tid, b"<svg onload=alert(1)>").status_code, 400)
        self.assertEqual(self.upload(99999).status_code, 404)
        snap = self.c.get("/api/hmi/state?force=1").json()["snap"]
        t = next(x for x in snap["tickets"] if x["id"] == tid)
        self.assertEqual(len(t["photos"]), 1)
        self.assertEqual(t["photos"][0]["by"], "J. Ortiz")
        got = Client().get(f"/api/hmi/ticket-photo/{t['photos'][0]['id']}")
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got["Content-Type"], "image/jpeg")
        self.assertEqual(got["X-Content-Type-Options"], "nosniff")
        self.assertEqual(got.content[:3], b"\xff\xd8\xff")
        thumb = Client().get(f"/api/hmi/ticket-photo/{t['photos'][0]['id']}?size=thumb")
        self.assertLess(len(thumb.content), len(got.content))
        self.assertEqual(Client().get("/api/hmi/ticket-photo/99999").status_code, 404)
        for _ in range(photos.MAX_PER_TICKET - 1):
            self.assertTrue(self.upload(tid).json()["ok"])
        r = self.upload(tid)
        self.assertEqual(r.status_code, 400)
        self.assertIn("up to", r.json()["error"])

    def test_old_closed_tickets_lose_their_pictures(self):
        t0 = now()
        old = Ticket.objects.create(t=t0 - timedelta(days=400), by="x", machine="Barbara", type="Jam", status="Closed",
                                    updated=t0 - timedelta(days=399))
        still_open = Ticket.objects.create(t=t0 - timedelta(days=400), by="x", machine="Barbara", type="Jam", status="Open")
        recent = Ticket.objects.create(t=t0 - timedelta(days=10), by="x", machine="Barbara", type="Jam", status="Closed", updated=t0)
        for t in (old, still_open, recent):
            photos.add(t, JPEG, "x", t0)
        self.assertEqual(photos.purge(t0, 0), 0)          # 0 = keep for good
        self.assertEqual(photos.purge(t0, 365), 1)
        self.assertEqual(set(TicketPhoto.objects.values_list("ticket_id", flat=True)), {still_open.id, recent.id})


class FeedErrorTests(TestCase):
    def setUp(self):
        no_planned_time(self)
        cache.clear()
        seed_demo(reset=True)
        MachineError.objects.all().delete()
        self.bar = Machine.objects.get(name="Barbara")
        self.bar.error_src = {"type": "feed", "field": "fault", "textField": "faultText", "ok": "READY"}
        self.bar.save()

    def test_an_error_lasts_until_it_changes_or_clears(self):
        t = now().replace(second=0, microsecond=0)
        m = lambda i: t + timedelta(minutes=i)  # noqa: E731
        for i, (code, text) in enumerate([(0, ""), ("E214", "Media break"), ("E214", "Media break"), ("E031", "Guard open"),
                                          ("READY", ""), (None, None)]):
            machineerrors.track_feed(self.bar, code, text, m(i))
        e = list(MachineError.objects.order_by("start").values_list("code", "text", "start", "end"))
        self.assertEqual(e, [("E214", "Media break", m(1), m(3)), ("E031", "Guard open", m(3), m(4))])

    def test_pushed_reading_with_an_error(self):
        self.bar.feed_type = "push"
        self.bar.save()
        LineState.objects.filter(machine=self.bar).update(pushed={"ppm": 0, "fault": "E214", "faultText": "Media break"}, pushed_at=now(),
                                                         next_tick=now().replace(second=0, microsecond=0))
        ticker.tick_machines(now())
        ls = LineState.objects.get(machine=self.bar)
        self.assertEqual(ls.feed["error"], "E214 · Media break")
        self.assertEqual(MachineError.objects.get(line="Barbara", end__isnull=True).code, "E214")

    def test_each_downtime_lists_what_the_machine_reported(self):
        sh = ShiftRun.objects.get(machine=self.bar, active=True)
        Downtime.objects.filter(line="Barbara").delete()
        t = now().replace(second=0, microsecond=0) - timedelta(hours=1)
        d = Downtime.objects.create(shift=sh, line="Barbara", start_time=t, end_time=t + timedelta(minutes=10), minutes=10)
        MachineError.objects.create(line="Barbara", start=t - timedelta(minutes=1), end=t + timedelta(minutes=2), code="E031")  # just before
        MachineError.objects.create(line="Barbara", start=t + timedelta(minutes=5), end=t + timedelta(minutes=5), code="A7", source="log")
        MachineError.objects.create(line="Barbara", start=t - timedelta(minutes=30), end=t - timedelta(minutes=20), code="OLD")
        MachineError.objects.create(line="Salvatore", start=t + timedelta(minutes=3), code="OTHER")
        MachineError.objects.create(line="Barbara", start=t + timedelta(minutes=8), code="STILL")  # still showing
        got = machineerrors.for_downtime([d])[d.id]
        self.assertEqual([x["code"] for x in got], ["E031", "A7", "STILL"])
        snap = Client().get("/api/hmi/state?force=1").json()["snap"]
        self.assertEqual([x["code"] for x in next(x for x in snap["downtime"] if x["id"] == d.id)["errors"]], ["E031", "A7", "STILL"])


class AlarmLogTests(TestCase):
    def setUp(self):
        cache.clear()
        seed_demo(reset=True)
        MachineError.objects.all().delete()
        fake_table(self)
        with connection.cursor() as c:
            c.execute("CREATE TABLE plc_alarms (id INTEGER PRIMARY KEY, line TEXT, at TEXT, code TEXT, msg TEXT)")
        self.addCleanup(lambda: connection.cursor().execute("DROP TABLE IF EXISTS plc_alarms"))
        self.bar = Machine.objects.get(name="Barbara")
        self.bar.error_src = {"type": "db", "db": "machines", "table": "plc_alarms", "orderCol": "id", "keyCol": "line", "keyVal": "BARB",
                              "timeCol": "at", "codeCol": "code", "textCol": "msg"}
        self.bar.save()
        self.ls = LineState.objects.get(machine=self.bar)

    def alarm(self, i, line, at, code, msg):
        with connection.cursor() as c:
            c.execute("INSERT INTO plc_alarms (id, line, at, code, msg) VALUES (%s, %s, %s, %s, %s)", [i, line, at, code, msg])

    def test_new_rows_only_and_their_own_time(self):
        self.alarm(1, "BARB", "2026-11-02T09:00:00", "E1", "old history")
        t = now()
        self.assertEqual(machineerrors.read_log(self.bar, self.ls, t), 0)   # starts from the newest row
        self.alarm(2, "BARB", "2026-11-02T10:15:30", "E214", "Media break")
        self.alarm(3, "SAL", "2026-11-02T10:16:00", "E9", "other machine")
        self.alarm(4, "BARB", "", "0", "")                                  # "no error" row
        self.alarm(5, "BARB", "garbled", "A7", "Low air")
        self.assertEqual(machineerrors.read_log(self.bar, self.ls, t), 2)
        self.assertEqual(machineerrors.read_log(self.bar, self.ls, t), 0)   # nothing twice
        rows = list(MachineError.objects.order_by("id").values_list("code", "text", "source"))
        self.assertEqual(rows, [("E214", "Media break", "log"), ("A7", "Low air", "log")])
        e = MachineError.objects.get(code="E214")
        self.assertEqual(timezone.localtime(e.start).strftime("%H:%M:%S"), "10:15:30")
        self.assertEqual(MachineError.objects.get(code="A7").start, t)     # unreadable time: when it was read
        prev = machineerrors.preview(self.bar)
        self.assertEqual([x["code"] for x in prev], ["A7", "0", "E214", "E1"])

    def test_a_broken_table_is_reported_not_raised(self):
        self.bar.error_src = {**self.bar.error_src, "codeCol": "nope"}
        self.assertEqual(machineerrors.read_log(self.bar, self.ls, now()), 0)
        self.assertFalse(self.ls.err_state["ok"])
        self.assertIn("nope", self.ls.err_state["err"])


class AdminErrorSettingsTests(TestCase):
    def setUp(self):
        cache.clear()
        seed_demo(reset=True)
        self.c = Client()
        self.c.post("/api/hmi/login", json.dumps({"pin": "9999"}), content_type="application/json")

    def cmd(self, name, p):
        return self.c.post("/api/hmi/cmd", json.dumps({"name": name, "p": p, "line": "Barbara"}), content_type="application/json").json()

    def test_set_up_an_alarm_log(self):
        for f, v in (("err.type", "db"), ("err.table", "plc_alarms"), ("err.orderCol", "id"), ("err.codeCol", "code"),
                     ("err.textCol", "msg"), ("err.timeCol", "at"), ("err.ok", "0, READY")):
            self.assertTrue(self.cmd("lineSet", {"id": "Barbara", "f": f, "v": v})["ok"], f)
        e = Machine.objects.get(name="Barbara").error_src
        self.assertEqual((e["type"], e["db"], e["table"], e["codeCol"], e["ok"]), ("db", "machines", "plc_alarms", "code", "0, READY"))
        self.cmd("lineSet", {"id": "Barbara", "f": "err.table", "v": "other"})
        e = Machine.objects.get(name="Barbara").error_src
        self.assertEqual((e["codeCol"], e["textCol"], e["timeCol"], e["orderCol"]), ("", "", "", ""))
        self.assertFalse(self.cmd("lineSet", {"id": "Barbara", "f": "err.bogus", "v": 1})["ok"])
        with mock.patch("hmi.machineerrors.preview", return_value=[{"code": "E1", "text": "", "at": "", "row": "9"}]):
            r = self.cmd("lineFeedTest", {"id": "Barbara", "which": "err"})
        self.assertEqual(r["data"]["rows"][0]["code"], "E1")
