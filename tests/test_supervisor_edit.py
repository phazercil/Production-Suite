"""Supervisor › History corrections: downtime, shift logs, QC checks, changeovers and MO runs, each change kept with
who, when, old and new value, and only for people with Correct history."""
import json
from datetime import timedelta

from django.test import Client, TestCase
from django.utils import timezone

from hmi import engine
from hmi.models import ChangeoverPiece, Downtime, MoRun, QCCheck, ShiftRun
from plant.demo import seed_demo
from plant.models import Person
from supervisor.models import HistoryEdit


def loc(dt):
    return timezone.localtime(dt).strftime("%Y-%m-%dT%H:%M")


class HistoryEditTests(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.lead = engine.Ctx(user=Person.objects.get(name="S. Lead"))
        self.op = engine.Ctx(user=Person.objects.get(name="J. Ortiz"))
        self.old = ShiftRun.objects.filter(active=False).exclude(user_name="").order_by("-start").first()
        t = self.old.start + timedelta(hours=1)
        self.d = Downtime.objects.create(shift=self.old, line=self.old.line, start_time=t, end_time=t + timedelta(minutes=481), minutes=481)

    def test_operators_cannot_correct(self):
        r = engine.execute("histEdit", {"kind": "downtime", "id": self.d.pk, "start": loc(self.d.start_time), "end": loc(self.d.end_time),
                                        "reason": "Jam"}, self.op)
        self.assertFalse(r["ok"])

    def test_downtime_edit_split_and_delete(self):
        reason = engine.config()["reasons"][0]
        r = engine.execute("histEdit", {"kind": "downtime", "id": self.d.pk, "start": loc(self.d.start_time),
                                        "end": loc(self.d.start_time + timedelta(minutes=20)), "reason": reason, "notes": "Left open", "mo": self.d.mo,
                                        "why": "overnight"}, self.lead)
        self.assertTrue(r["ok"], r)
        self.d.refresh_from_db()
        self.assertEqual((self.d.minutes, self.d.reason), (20, reason))
        fields = set(HistoryEdit.objects.filter(kind="downtime", obj_id=self.d.pk).values_list("field", flat=True))
        self.assertTrue({"Reason", "Notes", "End", "Minutes"} <= fields, fields)
        e = HistoryEdit.objects.get(kind="downtime", obj_id=self.d.pk, field="Minutes")
        self.assertEqual((e.old, e.new, e.by, e.why), ("481", "20", "S. Lead", "overnight"))
        self.old.refresh_from_db()
        self.assertEqual(self.old.down_mins, sum(x.minutes for x in Downtime.objects.filter(shift=self.old)))   # the log's figure follows
        r = engine.execute("downSplit", {"id": self.d.pk, "at": loc(self.d.start_time + timedelta(minutes=5))}, self.lead)
        self.assertTrue(r["ok"], r)
        self.d.refresh_from_db()
        self.assertEqual((self.d.minutes, Downtime.objects.get(pk=r["data"]["id"]).minutes), (5, 15))
        self.assertTrue(engine.execute("histDelete", {"kind": "downtime", "id": self.d.pk, "why": "bogus"}, self.lead)["ok"])
        self.assertFalse(Downtime.objects.filter(pk=self.d.pk).exists())
        self.assertTrue(HistoryEdit.objects.filter(kind="downtime", obj_id=self.d.pk, field="Deleted", why="bogus").exists())

    def test_log_qc_co_and_morun(self):
        r = engine.execute("histEdit", {"kind": "log", "id": self.old.pk, "operator": "M. Lee", "shift_name": self.old.shift_name, "count": 1234,
                                        "start": loc(self.old.start), "end": loc(self.old.end)}, self.lead)
        self.assertTrue(r["ok"], r)
        self.old.refresh_from_db()
        self.assertEqual((self.old.user_name, self.old.person.name, self.old.count), ("M. Lee", "M. Lee", 1234))
        self.assertFalse(engine.execute("histEdit", {"kind": "log", "id": self.old.pk, "count": 5, "start": loc(self.old.start),
                                                     "end": loc(self.old.start - timedelta(hours=1))}, self.lead)["ok"])   # end before start
        q = QCCheck.objects.filter(shift__isnull=False).first()
        vals = dict(q.values or {}) or {k: getattr(q, k) for k in ("len", "wid", "ph", "sbl", "sbr", "glue", "cut", "scrap")}
        vals = {k: v for k, v in vals.items() if v not in (None, "")}
        vals["scrap"] = 7
        r = engine.execute("histEdit", {"kind": "qc", "id": q.pk, **{k: str(v) for k, v in vals.items()}, "by": q.by, "review": q.review,
                                        "late_reason": ""}, self.lead)
        self.assertTrue(r["ok"], r)
        q.refresh_from_db()
        self.assertEqual(q.scrap, 7)
        sh = self.old
        co = ChangeoverPiece.objects.create(line=sh.line, co_id="x1", shift=sh, by="J. Ortiz", shift_name="1st shift", start=sh.start,
                                            end=sh.start + timedelta(minutes=30), minutes=30, ended="passed over")
        r = engine.execute("histEdit", {"kind": "co", "id": co.pk, "by": "M. Lee", "shift_name": "1st shift", "start": loc(co.start),
                                        "end": loc(co.start + timedelta(minutes=45)), "ended": "finished", "note": ""}, self.lead)
        self.assertTrue(r["ok"], r)
        co.refresh_from_db()
        self.assertEqual((co.by, co.minutes, co.ended), ("M. Lee", 45, "finished"))
        mr = MoRun.objects.create(shift=sh, line=sh.line, mo="309215", item="MG4062HP", qty=100, start_time=sh.start, end_time=sh.start + timedelta(hours=1))
        r = engine.execute("histEdit", {"kind": "morun", "id": mr.pk, "mo": "309215", "item": "MG4062HP", "qty": 150, "scrap": 2,
                                        "start": loc(mr.start_time), "end": loc(mr.end_time)}, self.lead)
        self.assertTrue(r["ok"], r)
        mr.refresh_from_db()
        self.assertEqual((mr.qty, mr.scrap), (150, 2))
        self.assertFalse(engine.execute("histDelete", {"kind": "log", "id": ShiftRun.objects.filter(active=True).first().pk}, self.lead)["ok"])

    def test_pages(self):
        c = Client(enforce_csrf_checks=True)
        c.get("/supervisor/")
        c.post("/api/hmi/login", json.dumps({"pin": "4444", "perm": "section.supervisor"}), content_type="application/json",
               HTTP_X_CSRFTOKEN=c.cookies["csrftoken"].value)
        r = c.get("/supervisor/?tab=down")
        self.assertContains(r, f"/supervisor/edit/downtime/{self.d.pk}/")
        self.assertContains(c.get("/supervisor/?tab=morun"), "MO runs")
        page = c.get(f"/supervisor/edit/downtime/{self.d.pk}/?back=/supervisor/%3Ftab%3Ddown")
        self.assertContains(page, "Split this stop")
        tok = c.cookies["csrftoken"].value
        r = c.post(f"/supervisor/edit/downtime/{self.d.pk}/", {"csrfmiddlewaretoken": tok, "action": "save", "back": "/supervisor/?tab=down",
                                                               "start": loc(self.d.start_time), "end": loc(self.d.start_time + timedelta(minutes=30)),
                                                               "reason": "", "notes": "", "mo": "", "why": "test"})
        self.assertEqual(r.status_code, 302)
        self.d.refresh_from_db()
        self.assertEqual(self.d.minutes, 30)
        r = c.post(f"/supervisor/edit/downtime/{self.d.pk}/", {"csrfmiddlewaretoken": tok, "action": "delete", "back": "/supervisor/?tab=down"})
        self.assertContains(r, "Yes, delete it")                       # not ticked: not deleted
        self.assertTrue(Downtime.objects.filter(pk=self.d.pk).exists())
        self.assertContains(c.get(f"/supervisor/log/{self.old.pk}/"), "Edit log")
        # a short stop whose times weren't touched keeps its minutes (the boxes only go to the minute)
        t0 = self.old.start + timedelta(hours=2, seconds=10)
        d2 = Downtime.objects.create(shift=self.old, line=self.old.line, start_time=t0, end_time=t0 + timedelta(seconds=40), minutes=1)
        r = engine.execute("histEdit", {"kind": "downtime", "id": d2.pk, "start": loc(d2.start_time), "end": loc(d2.end_time), "reason": "",
                                        "notes": "checked", "mo": ""}, self.lead)
        self.assertTrue(r["ok"], r)
        d2.refresh_from_db()
        self.assertEqual((d2.minutes, d2.notes, d2.end_time), (1, "checked", t0 + timedelta(seconds=40)))
