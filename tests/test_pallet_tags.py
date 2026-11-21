"""Packaging Kiosk › pallet tags: part, line, pallet number, quantity and who signed it, and the printed tag."""
from django.test import Client, TestCase

from hmi.models import Person
from packkiosk import tags
from packkiosk.models import PalletTag
from plant.demo import seed_demo
from plant.models import MO

from .test_quality import sup_login


class PalletTagTests(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.op = Person.objects.get(pin="1111")      # J. Ortiz, operator
        self.op2 = Person.objects.get(pin="2222")     # M. Lee, operator
        self.lead = Person.objects.get(pin="9999")
        self.mo = MO.objects.create(number="312153-030", part="MG3672", qty=1000, stage="pk")

    def make(self, me=None, **p):
        base = {"part": "MG3672", "line": "Barbara", "qty": "336", "description": "MicroGard"}
        return tags.create({**base, **p}, me or self.op)

    def test_numbers_pallets_per_mo_and_refuses_a_second_tag_for_one_pallet(self):
        r = self.make(mo=self.mo.number)
        self.assertTrue(r["ok"], r)
        t = PalletTag.objects.get(pk=r["id"])
        self.assertEqual((t.pallet_no, t.qty, t.line, t.packed_by, t.mo), (1, 336, "Barbara", "J. Ortiz", "312153-030"))
        self.assertEqual(tags.next_pallet(self.mo.number, "MG3672"), 2)
        self.assertTrue(self.make(mo=self.mo.number, pallet_no="54")["ok"])
        self.assertEqual(tags.next_pallet(self.mo.number, "MG3672"), 55)
        dup = self.make(mo=self.mo.number, pallet_no="54")
        self.assertFalse(dup["ok"])
        self.assertIn("Use pallet 55", dup["error"])
        s = tags.suggest(self.mo.number, "")
        self.assertEqual((s["part"], s["pallet"], s["qty"], s["line"], s["description"], s["packed"]), ("MG3672", 55, 336, "Barbara", "MicroGard", 672))

    def test_checks_what_was_typed(self):
        self.assertFalse(self.make(part="")["ok"])
        self.assertFalse(self.make(line="Nowhere")["ok"])
        self.assertFalse(self.make(qty="0")["ok"])
        self.assertIn("is for MG3672", self.make(part="MG9999", mo=self.mo.number)["error"])
        self.assertIn("isn't open", self.make(mo="000000")["error"])

    def test_sign_offs_need_another_person_and_the_right_permission(self):
        t = PalletTag.objects.get(pk=self.make()["id"])
        self.assertFalse(tags.sign(t, self.op, "verify")["ok"])            # not your own pallet
        self.assertTrue(tags.sign(t, self.op2, "verify")["ok"])
        self.assertFalse(tags.sign(t, self.op2, "supervisor")["ok"])       # operators can't sign as supervisor
        self.assertTrue(tags.sign(t, self.lead, "supervisor")["ok"])
        t.refresh_from_db()
        self.assertEqual((t.verified_by, t.supervisor), ("M. Lee", self.lead.name))

    def test_void_frees_the_pallet_number(self):
        t = PalletTag.objects.get(pk=self.make(mo=self.mo.number)["id"])
        self.assertFalse(tags.void(t, self.op2, "wrong count")["ok"])      # someone else's tag
        self.assertFalse(tags.void(t, self.op, "")["ok"])
        self.assertTrue(tags.void(t, self.op, "wrong count")["ok"])
        self.assertEqual(tags.next_pallet(self.mo.number, ""), 1)

    def test_pages_make_sign_and_print_a_tag(self):
        c = Client(enforce_csrf_checks=True)
        self.assertContains(c.get("/packaging/"), "Pallet tags for finished filters")    # PIN first
        self.assertTrue(sup_login(c, "1111", perm="section.packaging", page="/packaging/")["ok"])
        r = c.get("/packaging/")
        self.assertContains(r, "New pallet tag")
        tok = c.cookies["csrftoken"].value
        r = c.post("/packaging/do/create", {"part": "mg3672", "line": "Barbara", "qty": "336", "pallet_no": "54", "description": "MicroGard",
                                            "andPrint": "1"}, HTTP_X_CSRFTOKEN=tok)
        self.assertEqual(r.status_code, 302)
        t = PalletTag.objects.get()
        self.assertIn(f"print={t.pk}", r["Location"])
        self.assertEqual(c.get("/packaging/suggest?part=MG3672").json()["pallet"], 55)
        r = c.post("/packaging/do/sign", {"id": t.pk, "kind": "verify", "pin": "2222"}, HTTP_X_CSRFTOKEN=tok)
        self.assertIn("msg=", r["Location"])
        r = c.post("/packaging/do/sign", {"id": t.pk, "kind": "supervisor", "pin": "2222"}, HTTP_X_CSRFTOKEN=tok)
        self.assertIn("err=", r["Location"])
        r = c.get(f"/packaging/tag/{t.pk}/?auto=1")
        for s in ("MG3672", "MicroGard", "336", ">54<", "Barbara", "J. Ortiz", "M. Lee", "Supervisor", "data-auto"):
            self.assertContains(r, s)
        t.refresh_from_db()
        self.assertEqual(t.prints, 1)
