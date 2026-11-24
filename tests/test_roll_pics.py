"""Start of shift and End shift ask for a picture of the roll on the machine and of its tag."""
import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from PIL import Image

from hmi import engine
from hmi.models import RollTagPhoto, ShiftRun
from plant.demo import seed_demo
from plant.models import MO


def jpeg(name, color):
    b = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(b, "JPEG")
    return SimpleUploadedFile(name, b.getvalue(), content_type="image/jpeg")


class RollPictures(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.c = Client(HTTP_HX_REQUEST="true")
        self.c.post("/kiosk/do/station", {"line": "Barbara"})
        self.c.post("/kiosk/do/login", {"pin": "9999"})

    def test_end_then_start_keeps_both_pictures_with_the_shifts(self):
        sh = ShiftRun.objects.get(line="Barbara", active=True)
        self.assertContains(self.c.get("/kiosk/m/end"), "The roll you're leaving for the next shift")
        r = self.c.post("/kiosk/do/endShift", {"issues": ""})
        self.assertContains(r, "Take a picture of the roll and the roll&#x27;s tag", status_code=r.status_code)
        self.assertTrue(ShiftRun.objects.get(pk=sh.pk).active)          # not ended without them
        r = self.c.post("/kiosk/do/endShift", {"issues": "", "rollPic": jpeg("r.jpg", "red"), "rollTag": jpeg("t.jpg", "blue")})
        self.assertFalse(ShiftRun.objects.get(pk=sh.pk).active)
        self.assertEqual(sorted(RollTagPhoto.objects.filter(shift=sh, kind="end").values_list("what", flat=True)), ["roll", "tag"])
        r = self.c.get("/kiosk/m/start?line=Barbara")
        self.assertContains(r, "Left by the last shift")
        mo = MO.objects.filter(line__name="Barbara", status="OPEN").order_by("seq").first()
        checks = {"check": [f"s{i}" for i in range(len(engine.config()["safety"]))]}
        r = self.c.post("/kiosk/do/startShift", {"line": "Barbara", "mo": mo.number, **checks, "rollPic": jpeg("r.jpg", "green")})
        self.assertContains(r, "the roll&#x27;s tag", status_code=r.status_code)
        r = self.c.post("/kiosk/do/startShift", {"line": "Barbara", "mo": mo.number, **checks,
                                                 "rollPic": jpeg("r.jpg", "green"), "rollTag": jpeg("t.jpg", "white")})
        new = ShiftRun.objects.get(line="Barbara", active=True)
        self.assertNotEqual(new.pk, sh.pk)
        self.assertEqual(RollTagPhoto.objects.filter(shift=new, kind="start").count(), 2)
        rep = self.c.get("/kiosk/v/end-of-shift")
        self.assertContains(rep, "Roll started with")

    def test_optional_and_off(self):
        r = dict(engine.rules())
        r["rollPhotos"] = 1
        sh = ShiftRun.objects.get(line="Barbara", active=True)
        from unittest import mock
        with mock.patch.object(engine, "rules", return_value=r):
            self.c.post("/kiosk/do/endShift", {"issues": ""})
        self.assertFalse(ShiftRun.objects.get(pk=sh.pk).active)        # asked, skipped
        r = dict(r, rollPhotos=0)
        with mock.patch.object(engine, "rules", return_value=r):
            self.assertNotContains(self.c.get("/kiosk/m/start?line=Barbara"), "The roll you're starting with")
