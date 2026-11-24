"""Quality and planning: SPC and drift warnings, lot genealogy and recall, changeover analytics and historical
estimates, the suggested run order, downstream stations, and S2K posting."""
import json
from datetime import timedelta

from django.core.cache import cache
from django.test import Client, TestCase
from django.utils import timezone

from hmi.models import (
    ChangeoverPiece,
    MoRun,
    Notice,
    PpmReading,
    QCCheck,
    RollTag,
    ShiftRun,
)
from packkiosk.models import PalletTag
from plant.demo import seed_demo
from plant.models import MO, Machine, PartSpec
from plant.util import ms
from quality import spc
from scheduler.engine import compute, load_world
from scheduler.models import Job

from .test_quality import sup_login


def post(c, path, data):
    return c.post(path, json.dumps(data), content_type="application/json").json()


class Spc(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        cache.clear()
        PartSpec.objects.update_or_create(key="MG4062HP", defaults=dict(pleats=25, len=10, wid=8, ph=1, sb=0.75, tol=0.06))
        QCCheck.objects.all().delete()
        self.t0 = timezone.now() - timedelta(hours=5)

    def add(self, vals):
        for i, v in enumerate(vals):
            QCCheck.objects.create(t=self.t0 + timedelta(minutes=10 * i), line="Barbara", item="MG4062HP", by="x", passed=True, len=v)

    def test_capability_and_rules(self):
        self.add([10.0, 10.01, 9.99, 10.0, 10.02, 9.98, 10.01, 10.0])
        lim = spc.limits("MG4062HP", "len")
        self.assertEqual(lim, (10.0, 9.94, 10.06))
        vals = [v for _, v, _ in spc.series("Barbara", "MG4062HP", "len", self.t0, timezone.now())]
        st = spc.stats(vals, lim)
        self.assertGreater(st["cpk"], 1.33)
        self.assertEqual(spc.warnings([10.0, 10.01, 10.03, 10.05], lim)[0][0], "near")
        self.assertIn("trend", [r for r, _ in spc.warnings([10.0, 10.01, 10.02, 10.03], lim)])
        self.assertIn("side", [r for r, _ in spc.warnings([10.01] * 7, lim)])
        rows = spc.capability(self.t0, timezone.now(), "len")
        self.assertEqual((rows[0]["line"], rows[0]["part"], rows[0]["n"]), ("Barbara", "MG4062HP", 8))

    def test_drift_tells_the_line_once(self):
        self.add([10.0, 10.02, 10.04, 10.055])
        Notice.objects.all().delete()
        told = spc.check_new("Barbara", "MG4062HP", ["len"])
        self.assertTrue(told)
        self.assertTrue(Notice.objects.filter(text__contains="Length").exists())
        self.assertEqual(spc.check_new("Barbara", "MG4062HP", ["len"]), [])   # once an hour

    def test_spc_tab(self):
        self.add([10.0, 10.01, 9.99, 10.0, 10.02, 9.98])
        c = Client()
        self.assertTrue(sup_login(c, "4444", "section.quality", "/quality/")["ok"])
        r = c.get("/quality/?tab=spc&m=len").content.decode()
        self.assertIn("Capability by machine and part", r)
        self.assertIn("<svg class=\"spc\"", r)
        self.assertIn("Cpk", r)


class RecallAndChangeovers(TestCase):
    def setUp(self):
        seed_demo(reset=True)
        self.bar = Machine.objects.get(name="Barbara")
        self.t0 = timezone.now().replace(second=0, microsecond=0) - timedelta(hours=4)
        PpmReading.objects.filter(line="Barbara", t__gte=self.t0 - timedelta(hours=1)).delete()
        MoRun.objects.filter(line="Barbara").delete()
        RollTag.objects.all().delete()
        ShiftRun.objects.filter(line="Barbara").update(active=False)
        sh = ShiftRun.objects.create(machine=self.bar, line="Barbara", start=self.t0, end=self.t0 + timedelta(hours=1), active=False)
        for i in range(60):
            PpmReading.objects.create(machine=self.bar, shift=sh, line="Barbara", t=self.t0 + timedelta(minutes=i), ppm=200, units=5)
        MoRun.objects.create(shift=sh, line="Barbara", mo="777001", item="MG4062HP", qty=300, start_time=self.t0, end_time=self.t0 + timedelta(hours=1))
        RollTag.objects.create(t=self.t0, line="Barbara", roll="R-1", lot="LOT-9", material="Media", by="J. Ortiz")
        PalletTag.objects.create(part="MG4062HP", mo="777001", line="Barbara", pallet_no=1, qty=150, t=self.t0 + timedelta(hours=2), packed_by="P")
        PalletTag.objects.create(part="MG4062HP", mo="777001", line="Barbara", pallet_no=2, qty=150, t=self.t0 + timedelta(hours=2), packed_by="P")

    def test_lot_forward_to_pallets_and_back(self):
        from hmi import trace
        R = trace.recall("LOT-9", timezone.now())
        self.assertEqual([m["mo"] for m in R["mos"]], ["777001"])
        self.assertEqual(R["units"], 300)
        self.assertEqual(sorted(p.pallet_no for p in R["pallets"]), [1, 2])
        B = trace.recall("pallet:777001-2", timezone.now())
        self.assertEqual([p.pallet_no for p in B["pallets"]], [2])
        self.assertEqual(B["lots"], ["LOT-9"])
        c = Client()
        self.assertTrue(sup_login(c, "4444", "section.quality", "/quality/")["ok"])
        r = c.get("/quality/?tab=trace&view=recall&q=LOT-9").content.decode()
        self.assertIn("Pallets to quarantine", r)
        self.assertIn("#2", r)
        csv_text = c.get("/quality/?tab=trace&view=recall&q=LOT-9&csv=1").content.decode()
        self.assertIn("Recall for,LOT-9", csv_text)
        self.assertEqual(csv_text.count("777001"), 2)          # one row per pallet
        self.assertIn("Counts against MOs and rolls", c.get("/quality/?tab=trace&view=rec").content.decode())

    def test_changeover_report_and_estimates(self):
        from scheduler import cohist
        MO.objects.get_or_create(number="A1", defaults={"part": "MG100", "qty": 10})
        MO.objects.get_or_create(number="B1", defaults={"part": "MG200", "qty": 10})
        ChangeoverPiece.objects.all().delete()
        for i, mins in enumerate((40, 50, 60)):
            s = self.t0 - timedelta(days=i + 1)
            ChangeoverPiece.objects.create(machine=self.bar, line="Barbara", co_id=f"c{i}", by="J", start=s, end=s + timedelta(minutes=mins - 10),
                                           minutes=mins - 10, old_mo="A1", new_mo="B1", ended="passed over",
                                           step_t=[["Change blades", ms(s + timedelta(minutes=20))]])
            ChangeoverPiece.objects.create(machine=self.bar, line="Barbara", co_id=f"c{i}", by="", start=s + timedelta(minutes=mins - 10),
                                           end=s + timedelta(minutes=mins), minutes=10, old_mo="A1", new_mo="B1", ended="finished")
        rep = cohist.report(self.t0 - timedelta(days=5), self.t0)
        self.assertEqual(rep["lines"][0]["n"], 3)
        self.assertEqual(rep["lines"][0]["wait"], 30)
        self.assertEqual(rep["pairs"][0]["median"], 50)
        self.assertEqual(rep["steps"][0]["step"], "Change blades")
        self.assertEqual(cohist.estimates(self.t0)["Barbara"][("MG100", "MG200")], 50.0)


class SequenceStationsS2K(TestCase):
    def setUp(self):
        cache.clear()
        seed_demo(reset=True)
        self.c = Client()
        assert post(self.c, "/api/hmi/login", {"pin": "9999"})["ok"]

    def test_suggest_groups_parts_and_applies(self):
        from scheduler.sequence import suggest
        sal = Machine.objects.get(name="Salvatore")
        Job.objects.filter(machine=sal).delete()
        base = timezone.now() + timedelta(days=1)
        parts = ["MG100", "MG200", "MG100", "MG200"]
        for i, p in enumerate(parts):
            mo = MO.objects.create(number=f"SQ{i}", part=p, qty=200, status="OPEN", due=(base + timedelta(days=30)).date())
            Job.objects.create(mo=mo, op="s1", machine=sal, req_start=base + timedelta(hours=i))
        s = suggest(load_world(), sal.pk)
        self.assertTrue(s["ok"], s)
        self.assertGreater(s["saved"], 0)
        self.assertEqual([x["part"] for x in s["new"]], ["MG100", "MG100", "MG200", "MG200"])
        r = post(self.c, "/api/scheduler/suggest/apply", {"machineId": sal.pk, "keys": s["keys"]})
        self.assertTrue(r["ok"], r)
        sch = compute(load_world())
        order = sorted((k for k in sch["res"] if sch["res"][k]["machineId"] == sal.pk), key=lambda k: sch["res"][k]["start"])
        self.assertEqual([MO.objects.get(pk=int(k.split("__")[0])).part for k in order], ["MG100", "MG100", "MG200", "MG200"])

    def test_station_start_log_complete(self):
        eb = Machine.objects.filter(stage="eb").first()
        if not eb:
            eb = Machine.objects.create(name="EB-1", stage="eb", ppm=10)
        mo = MO.objects.filter(status="OPEN").first()
        Job.objects.filter(mo=mo, op="eb").delete()
        j = Job.objects.create(mo=mo, op="eb", machine=eb, req_start=timezone.now())
        c = Client()
        self.assertTrue(sup_login(c, "1111", "section.stations", "/stations/")["ok"])
        self.assertContains(c.get(f"/stations/?line={eb.name}"), f"Start MO {mo.number}")
        c.post("/stations/do/start", {"id": j.pk})
        c.post("/stations/do/qty", {"id": j.pk, "qty": "40", "how": "add"})
        j.refresh_from_db()
        self.assertEqual(j.wip_qty, 40)
        r = compute(load_world())["res"].get(j.key)
        self.assertTrue(r and r["running"])
        c.post("/stations/do/complete", {"id": j.pk})
        self.assertNotIn(j.key, compute(load_world())["res"])

    def test_s2k_posting(self):
        mo = MO.objects.filter(status="OPEN").first()
        mo.remaining, mo.produced, mo.scrap = mo.qty, 100, 0
        mo.save()
        now = timezone.now()
        MoRun.objects.create(line="Barbara", mo=mo.number, item=mo.part, qty=100, scrap=0, start_time=now - timedelta(hours=1), end_time=now - timedelta(minutes=5))
        c = Client()
        self.assertTrue(sup_login(c, "4444")["ok"])
        r = c.get("/supervisor/?tab=s2k").content.decode()
        self.assertIn(mo.number, r)
        self.assertIn("to post", r)
        self.assertIn("MO run", c.get("/supervisor/?tab=s2k&csv=1").content.decode())
        c.post("/supervisor/s2k-mark", {})
        self.assertNotIn("MO run", c.get("/supervisor/?tab=s2k&csv=1").content.decode())
