"""Digital work instructions: drafts, approval by someone else, obsolete revisions, media, the kiosk and read receipts."""
import io
import json

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from PIL import Image

from hmi import engine
from instructions.cmds import for_station
from instructions.models import Ack, Instruction, Media, Revision
from plant.demo import seed_demo
from plant.models import Machine, Person


def png():
    b = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(b, "PNG")
    return SimpleUploadedFile("knife.png", b.getvalue(), content_type="image/png")


class WorkInstructions(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.admin = Person.objects.get(pin="9999")
        self.lead = Person.objects.filter(role="lead").first()
        self.bar = Machine.objects.get(name="Barbara")

    def ex(self, name, user, **p):
        return engine.execute(name, p, engine.Ctx(user=user))

    def test_lifecycle(self):
        r = self.ex("wiCreate", self.lead, title="Pleater setup MG", kind="setup", parts="MG40*, AQ12*", machines=[str(self.bar.pk)])
        self.assertTrue(r["ok"], r)
        x = Instruction.objects.get(pk=r["data"]["id"])
        self.assertEqual((x.doc_no, x.parts), ("WI-001", "MG40*, AQ12*"))
        self.assertFalse(self.ex("wiApprove", self.lead, id=x.pk)["ok"])            # no steps yet
        self.assertTrue(self.ex("wiSave", self.lead, id=x.pk, change_note="First issue", steps=[{"text": "Set the knife"}, {"text": ""}],
                                params=[{"name": "Pleat height", "target": "25", "min": "24.5", "max": "25.5", "unit": "mm"}, {"name": ""}])["ok"])
        self.assertFalse(self.ex("wiSave", self.lead, id=x.pk, steps=[{"text": "a"}], params=[{"name": "P", "min": "5", "max": "4"}])["ok"])
        r = self.ex("wiApprove", self.lead, id=x.pk)
        self.assertIn("other than its author", r["error"])
        self.assertTrue(self.ex("wiApprove", self.admin, id=x.pk)["ok"])
        r1 = x.current()
        self.assertEqual((r1.rev, len(r1.steps), r1.params[0]["max"]), (1, 1, "25.5"))
        # the kiosk: for Barbara and MG4062 only
        self.assertEqual([d["x"].pk for d in for_station(self.bar, "MG4062HP", "J. Ortiz")], [x.pk])
        self.assertEqual(for_station(self.bar, "XYZ1", "J. Ortiz"), [])
        self.assertEqual(for_station(Machine.objects.get(name="Salvatore"), "MG4062HP", "J. Ortiz"), [])
        # a new revision: the approved one stays in effect until it's approved, then is obsolete
        self.assertTrue(self.ex("wiDraft", self.lead, id=x.pk)["ok"])
        self.assertFalse(self.ex("wiDraft", self.lead, id=x.pk)["ok"])               # one draft at a time
        self.assertTrue(self.ex("wiSave", self.lead, id=x.pk, change_note="Knife torque added",
                                steps=[{"text": "Set the knife"}, {"text": "Torque to 12 Nm", "caution": "Lock out first"}])["ok"])
        self.assertEqual(x.current().rev, 1)
        self.assertTrue(self.ex("wiApprove", self.admin, id=x.pk)["ok"])
        self.assertEqual(Revision.objects.get(instruction=x, rev=1).status, "obsolete")
        self.assertEqual(x.current().rev, 2)
        self.assertFalse(self.ex("wiSave", self.lead, id=x.pk, steps=[{"text": "x"}])["ok"])   # approved can't change
        op = Person.objects.filter(role="operator").first()
        self.assertTrue(self.ex("wiAck", op, rev=x.current().pk)["ok"])
        self.assertTrue(for_station(self.bar, "MG4062HP", op.name)[0]["read"])
        self.assertFalse(self.ex("wiAck", op, rev=r1.pk)["ok"])                       # an obsolete revision can't be acknowledged

    @override_settings(BASE_DIR=__import__("tempfile").mkdtemp())
    def test_pages_media_and_kiosk(self):
        c = Client()
        c.get("/quality/")
        c.post("/api/hmi/login", json.dumps({"pin": "9999"}), content_type="application/json")
        r = c.post("/instructions/do/wiCreate", {"title": "Changeover AQ", "kind": "changeover", "parts": "", "machines": [self.bar.pk]})
        x = Instruction.objects.get()
        self.assertEqual(r["Location"].split("?")[0], f"/instructions/{x.pk}/")
        c.post(f"/instructions/{x.pk}/upload", {"files": [png(), SimpleUploadedFile("clip.mp4", b"\x00\x00\x00\x18ftypmp42", content_type="video/mp4")]})
        img, vid = Media.objects.get(kind="image"), Media.objects.get(kind="video")
        self.assertEqual(c.get(f"/instructions/media/{img.pk}?size=thumb")["Content-Type"], "image/jpeg")
        self.assertEqual(b"".join(c.get(f"/instructions/media/{vid.pk}").streaming_content)[:4], b"\x00\x00\x00\x18")
        self.assertContains(c.get(f"/instructions/{x.pk}/"), "Save the draft")
        c.post("/instructions/do/wiSave", {"id": x.pk, "s_text": ["Swap the guide", ""], "s_caution": ["", ""], "s_media_0": [img.pk, vid.pk],
                                           "p_name": ["Speed"], "p_target": ["180"], "p_min": [""], "p_max": ["200"], "p_unit": ["P/m"], "change_note": "First"})
        rv = x.draft()
        self.assertEqual(rv.steps, [{"text": "Swap the guide", "caution": "", "media": [img.pk, vid.pk]}])
        self.assertEqual(rv.params[0]["target"], "180")
        self.assertEqual(c.get("/quality/wi/")["Location"], "/instructions/")
        self.assertContains(c.get("/instructions/"), "WI-001")
        # approved (by an admin, their own: allowed) and on the kiosk, with the banner until it's read
        self.assertTrue(engine.execute("wiApprove", {"id": x.pk}, engine.Ctx(user=self.admin))["ok"])
        k = Client(HTTP_HX_REQUEST="true")
        k.post("/kiosk/do/station", {"line": "Barbara"})
        k.post("/kiosk/do/login", {"pin": "1111"})
        page = k.get("/kiosk/v/instructions").content.decode()
        self.assertIn("Swap the guide", page)
        self.assertIn("Read and understood", page)
        page = k.post("/kiosk/do/wiAck", {"rev": x.current().pk}).content.decode()
        self.assertIn("READ", page)
        self.assertEqual(Ack.objects.get().revision, x.current())
        self.assertContains(c.get(f"/instructions/{x.pk}/read-by.csv"), "WI-001")
