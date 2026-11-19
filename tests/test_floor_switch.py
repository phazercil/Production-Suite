"""End Order and the changeover: the end-of-order checklist, Back from Set up, a supervisor's emergency switch to an MO
that isn't lined up (the Scheduler follows), picking an MO for a line running without one, per-machine checklists and
inkjet-only changeovers between filters with the same base part."""
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase

from hmi import engine
from hmi.models import AuditLog, Downtime, ShiftRun
from plant.demo import seed_demo
from plant.models import MO, Machine, PartSpec, Person, PlantSettings
from plant.util import now
from scheduler.models import Job

from .test_suite import no_planned_time

ALL = [True] * 10


class FloorSwitchTests(TestCase):
    def setUp(self):
        no_planned_time(self)
        p = mock.patch.object(engine, "roll_over", side_effect=lambda sh, t: sh)
        p.start()
        self.addCleanup(p.stop)
        cache.clear()
        seed_demo(reset=True)
        self.sh = ShiftRun.objects.filter(line="Barbara", active=True).select_related("mo").first()
        self.lead = engine.Ctx(user=Person.objects.get(pin="4444"), line="Barbara")
        self.op = engine.Ctx(user=Person.objects.get(pin="1111"), line="Barbara")
        self.bar = Machine.objects.get(name="Barbara")

    def do(self, name, p=None, ctx=None):
        return engine.execute(name, p or {}, ctx or self.lead)

    def end_order(self, **p):
        self.assertTrue(self.do("coOpen")["ok"])
        return self.do("coEnd", {"qty": self.sh.mo_count, **p})

    def finish(self):
        sh = ShiftRun.objects.get(pk=self.sh.pk)
        sh.co = {**sh.co, "done": list(range(len(engine.co_steps(sh.co, "Barbara"))))}
        sh.save(update_fields=["co"])
        r = self.do("coFinish")
        self.assertTrue(r["ok"], r)

    # --- End order
    def test_end_order_needs_its_checklist(self):
        n = len(engine.machine_list("endSteps", "Barbara"))
        r = self.end_order(checks=[True] * (n - 1))
        self.assertIn(f"Check off all {n} end-of-order items", r["error"])
        r = self.do("coEnd", {"qty": self.sh.mo_count, "checks": ALL})
        self.assertTrue(r["ok"], r)
        sh = ShiftRun.objects.get(pk=self.sh.pk)
        self.assertEqual((sh.co["step"], sh.mo_id, sh.co["ended"]["checks"]), (2, None, n))

    def test_end_order_scrap_is_the_runs_total(self):
        from hmi.models import QCCheck
        mo = self.sh.mo
        QCCheck.objects.create(shift=self.sh, t=now(), line="Barbara", mo=mo.number, item=mo.part, by="x", passed=True, scrap=5)
        before = MO.objects.get(pk=mo.pk).scrap
        logged = engine.run_scrap(self.sh, mo)                    # what the QC checks logged this run (5 of it just now)
        self.assertGreaterEqual(logged, 5)
        self.end_order(checks=ALL, scrap=str(logged + 3))         # the run's total: 3 more than was logged
        self.assertEqual(MO.objects.get(pk=mo.pk).scrap, before + 3)

    def test_kiosk_end_order_then_changeover(self):
        c = Client(HTTP_HX_REQUEST="true")
        c.post("/kiosk/do/station", {"line": "Barbara"})
        c.post("/kiosk/do/login", {"pin": "1111"})
        self.assertContains(c.get("/kiosk/v/controls"), "End order")
        r = c.get("/kiosk/m/changeover")
        self.assertContains(r, "Before the changeover")
        self.assertContains(r, "Leftover media rolls tagged")
        r = c.post("/kiosk/do/coEnd", {"qty": str(self.sh.mo_count), "scrap": "0"}, HTTP_X_IN_MODAL="1")
        self.assertContains(r, "Check off all")
        n = len(engine.machine_list("endSteps", "Barbara"))
        r = c.post("/kiosk/do/coEnd", {"qty": str(self.sh.mo_count), "scrap": "0", "end": [str(i) for i in range(n)]})
        self.assertContains(r, "Scan next MO")                    # the changeover pops up
        self.assertContains(c.get("/kiosk/v/controls"), "Start changeover")

    # --- Back, and filter numbers the suite doesn't know
    def test_back_from_set_up_removes_the_unused_filter_number_mo(self):
        self.end_order(checks=ALL)
        self.assertTrue(self.do("coPickPart", {"part": "MG4062HP", "why": "S2K down"})["ok"])
        nomo = MO.objects.get(source="floor")
        self.assertTrue(self.do("coBack")["ok"])
        sh = ShiftRun.objects.get(pk=self.sh.pk)
        self.assertEqual((sh.co["step"], sh.co.get("newMo")), (2, None))
        self.assertFalse(MO.objects.filter(pk=nomo.pk).exists())
        self.assertTrue(self.do("coPickPart", {"part": "MG4062", "why": "S2K down"})["ok"])   # typed again

    # --- a supervisor's emergency switch
    def test_supervisor_switches_to_an_mo_from_another_line(self):
        other = MO.objects.filter(status="OPEN", line__name="Salvatore").exclude(pk=self.sh.mo_id).first() \
            or MO.objects.filter(status="OPEN").exclude(pk=self.sh.mo_id).exclude(line__name="Barbara").first()
        self.assertIsNotNone(other)
        MO.objects.filter(pk=other.pk).update(line=Machine.objects.get(name="Salvatore"))
        self.end_order(checks=ALL)
        r = self.do("coPick", {"mo": other.number})
        self.assertIn("Ask your shift lead", r["error"])           # an operator can't take another line's MO
        self.assertFalse(self.do("coPickMo", {"mo": other.number, "why": "x"}, self.op)["ok"])   # supervisors only
        self.assertIn("Say why", self.do("coPickMo", {"mo": other.number})["error"])
        r = self.do("coPickMo", {"mo": other.number, "why": "planner called an emergency switch"})
        self.assertTrue(r["ok"], r)
        self.finish()
        self.assertTrue(Job.objects.filter(mo=other, op="s1", machine=self.bar).exists())   # the board follows the floor
        self.assertEqual(MO.objects.get(pk=other.pk).line_id, self.bar.id)
        self.assertIn("Supervisor override on Barbara", " ".join(AuditLog.objects.values_list("what", flat=True)))

    def test_supervisor_switches_to_an_mo_the_suite_does_not_have(self):
        self.end_order(checks=ALL)
        r = self.do("coPickMo", {"mo": "777001", "part": "ZZ123", "qty": "500", "why": "x"})
        self.assertIn("isn't in the cost sheet", r["error"])
        r = self.do("coPickMo", {"mo": "777001", "part": "MG4062HP", "qty": "", "why": "x"})
        self.assertIn("quantity", r["error"])
        r = self.do("coPickMo", {"mo": "777001", "part": "MG4062HP", "qty": "500", "why": "emergency"})
        self.assertTrue(r["ok"], r)
        self.finish()
        mo = MO.objects.get(number="777001")
        self.assertEqual((mo.part, mo.qty, mo.source), ("MG4062HP", 500, "floor"))
        self.assertTrue(Job.objects.filter(mo=mo, op="s1", machine=self.bar).exists())

    # --- Pick MO without a changeover
    def test_pick_mo_for_a_run_by_filter_number(self):
        self.end_order(checks=ALL)
        self.do("coPickPart", {"part": "MG4062HP", "why": "S2K down"})
        self.finish()
        nomo = MO.objects.get(source="floor")
        ShiftRun.objects.filter(pk=self.sh.pk).update(mo_count=40)
        MO.objects.filter(pk=nomo.pk).update(produced=40, scrap=2)
        real = MO.objects.create(number="888001", part="MG4062HP", qty=300, status="OPEN", line=self.bar)
        self.assertTrue(self.do("moAttach", {"mo": "888001", "qty": "40"}, self.op)["ok"])
        sh = ShiftRun.objects.get(pk=self.sh.pk)
        real.refresh_from_db(); nomo.refresh_from_db()
        self.assertEqual((sh.mo_id, sh.mo_count, real.produced, real.scrap), (real.pk, 40, 40, 2))
        self.assertEqual((nomo.status, nomo.produced), ("CLOSE", 0))
        self.assertTrue(Job.objects.filter(mo=real, op="s1", machine=self.bar).exists())
        self.assertIn("already running MO", self.do("moAttach", {"mo": "888001", "qty": "0"}, self.op)["error"])

    def test_pick_mo_button_shows_without_an_mo(self):
        ShiftRun.objects.filter(pk=self.sh.pk).update(mo=None)
        c = Client(HTTP_HX_REQUEST="true")
        c.post("/kiosk/do/station", {"line": "Barbara"})
        c.post("/kiosk/do/login", {"pin": "1111"})
        r = c.get("/kiosk/v/controls")
        self.assertContains(r, "Pick MO")
        self.assertContains(r, "Start changeover")
        self.assertContains(c.get("/kiosk/m/attach"), "Filters already made for it")

    # --- per-machine checklists
    def test_each_machine_has_its_own_checklists(self):
        admin = engine.Ctx(user=Person.objects.get(pin="9999"))
        std = engine.machine_list("coSteps", "Barbara")
        self.assertTrue(engine.execute("listAdd", {"k": "coSteps", "m": "Barbara", "v": "Swap the Barbara die set"}, admin)["ok"])
        self.assertEqual(engine.machine_list("coSteps", "Barbara"), std + ["Swap the Barbara die set"])
        self.assertEqual(engine.machine_list("coSteps", "Salvatore"), std)                # the others keep the standard list
        self.assertTrue(engine.execute("listReset", {"k": "coSteps", "m": "Barbara"}, admin)["ok"])
        self.assertEqual(engine.machine_list("coSteps", "Barbara"), std)
        self.assertFalse(engine.execute("listAdd", {"k": "reasons", "m": "Barbara", "v": "x"}, admin)["ok"])
        c = Client(HTTP_HX_REQUEST="true")
        c.post("/kiosk/do/login", {"pin": "9999"})
        r = c.get("/kiosk/v/admin?tab=checklists&listm=Barbara")
        self.assertContains(r, "End-of-order checklist")
        self.assertContains(r, "Barbara uses the standard list")

    # --- inkjet-only changeovers
    def test_same_base_part_is_an_inkjet_change(self):
        PartSpec.objects.get_or_create(key="AQ1278", defaults={"pleats": 30, "len": 9, "wid": 8, "ph": 1, "sb": 1, "tol": .06})
        PartSpec.objects.get_or_create(key="AQ1278C", defaults={"pleats": 30, "len": 9, "wid": 8, "ph": 1, "sb": 1, "tol": .06})
        a = MO.objects.create(number="900001", part="AQ1278", qty=100, status="OPEN", line=self.bar)
        b = MO.objects.create(number="900002", part="AQ1278C", qty=100, status="OPEN", line=self.bar)
        ShiftRun.objects.filter(pk=self.sh.pk).update(mo=a, mo_count=0)
        self.sh.refresh_from_db()
        self.end_order(checks=ALL)
        self.assertTrue(self.do("coPick", {"mo": b.number})["ok"])
        sh = ShiftRun.objects.get(pk=self.sh.pk)
        self.assertTrue(sh.co["inkjet"])
        self.assertEqual([s["text"] for s in sh.co["steps"]], engine.machine_list("inkjetSteps", "Barbara"))
        # the line keeps running: what it makes is already the new MO's
        before = MO.objects.get(pk=b.pk).produced
        t = now()
        engine.tick(self.bar, {"ppm": 200, "count": 1000}, t)
        engine.tick(self.bar, {"ppm": 200, "count": 1012}, t + engine.timedelta(minutes=1))
        self.assertEqual(MO.objects.get(pk=b.pk).produced, before + 12)
        # a stop during it still counts as changeover downtime
        engine.tick(self.bar, {"ppm": 0, "count": 1012}, t + engine.timedelta(minutes=2))
        engine.tick(self.bar, {"ppm": 0, "count": 1012}, t + engine.timedelta(minutes=3))
        engine.tick(self.bar, {"ppm": 200, "count": 1020}, t + engine.timedelta(minutes=4))
        self.assertTrue(Downtime.objects.filter(line="Barbara", reason="Changeover").exists())
        self.finish()
        sh = ShiftRun.objects.get(pk=self.sh.pk)
        self.assertEqual((sh.mo_id, sh.mo_count), (b.pk, 20))

    def test_scheduler_plans_no_changeover_time_for_it(self):
        from scheduler.engine import compute, load_world
        ShiftRun.objects.filter(machine=self.bar).update(mo=None)
        Job.objects.all().delete()
        a = MO.objects.create(number="900001", part="AQ1278", qty=100, status="OPEN", pleats=30)
        b = MO.objects.create(number="900002", part="AQ1278C", qty=100, status="OPEN", pleats=30)
        c = MO.objects.create(number="900003", part="MG4062HP", qty=100, status="OPEN", pleats=30)
        t = now()
        for i, m in enumerate((a, b, c)):
            Job.objects.create(mo=m, op="s1", machine=self.bar, req_start=t + engine.timedelta(minutes=i))
        res = compute(load_world())["res"]
        self.assertGreater(res[f"{a.id}__s1"]["coMin"], 0)          # first run: a changeover
        self.assertEqual(res[f"{b.id}__s1"]["coMin"], 0)            # AQ1278 → AQ1278C: inkjet only
        self.assertTrue(res[f"{b.id}__s1"]["inkjet"])
        self.assertGreater(res[f"{c.id}__s1"]["coMin"], 0)          # then a real changeover
        st = PlantSettings.load()
        st.planner = {**st.planner, "inkjetMin": 5}
        st.save()
        self.assertEqual(compute(load_world())["res"][f"{b.id}__s1"]["coMin"], 5)


class ForceEndTests(TestCase):
    """A supervisor ends a changeover nobody finished."""

    def setUp(self):
        no_planned_time(self)
        p = mock.patch.object(engine, "roll_over", side_effect=lambda sh, t: sh)
        p.start()
        self.addCleanup(p.stop)
        cache.clear()
        seed_demo(reset=True)
        self.sh = ShiftRun.objects.filter(line="Barbara", active=True).select_related("mo").first()
        self.lead = engine.Ctx(user=Person.objects.get(pin="4444"), line="Barbara")
        self.op = engine.Ctx(user=Person.objects.get(pin="1111"), line="Barbara")
        engine.execute("coOpen", {}, self.op)
        r = engine.execute("coEnd", {"qty": self.sh.mo_count, "checks": ALL}, self.op)
        self.assertTrue(r["ok"], r)
        self.next = MO.objects.filter(status="OPEN", line__name="Barbara").exclude(pk=self.sh.mo_id).first()

    def test_operator_cannot_and_a_reason_is_needed(self):
        self.assertFalse(engine.execute("coForceEnd", {"why": "x"}, self.op)["ok"])
        self.assertIn("Say why", engine.execute("coForceEnd", {}, self.lead)["error"])

    def test_force_end_runs_the_picked_mo(self):
        from hmi.models import ChangeoverPiece
        self.assertTrue(engine.execute("coPick", {"mo": self.next.number}, self.op)["ok"])   # steps never checked off
        r = engine.execute("coForceEnd", {"why": "operator went home", "run": True}, self.lead)
        self.assertTrue(r["ok"], r)
        sh = ShiftRun.objects.get(pk=self.sh.pk)
        self.assertEqual((sh.co, sh.mo_id, sh.state in ("running", "stopped"), sh.qc_after_co), (None, self.next.pk, True, True))
        piece = ChangeoverPiece.objects.filter(line="Barbara").latest("id")
        self.assertEqual(piece.ended, "force-ended")
        self.assertIn("Force-ended by S. Lead: operator went home", piece.note)
        self.assertIn("changeover force-ended", AuditLog.objects.latest("id").what)

    def test_force_end_without_an_mo(self):
        r = engine.execute("coForceEnd", {"why": "left open"}, self.lead)
        self.assertTrue(r["ok"], r)
        sh = ShiftRun.objects.get(pk=self.sh.pk)
        self.assertEqual((sh.co, sh.mo_id), (None, None))
        self.assertIn("with no MO", r["msg"])

    def test_from_supervisor_line_status_and_kiosk(self):
        from .test_quality import sup_login
        c = Client(enforce_csrf_checks=True)
        self.assertTrue(sup_login(c, "4444")["ok"])
        self.assertContains(c.get("/supervisor/?tab=status"), f"/supervisor/coend/{self.sh.pk}/")
        page = c.get(f"/supervisor/coend/{self.sh.pk}/")
        self.assertContains(page, "Force end the changeover")
        r = c.post(f"/supervisor/coend/{self.sh.pk}/", {"why": "operator left", "run": "0", "csrfmiddlewaretoken": c.cookies["csrftoken"].value})
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(ShiftRun.objects.get(pk=self.sh.pk).co)
        self.assertNotContains(c.get("/supervisor/?tab=status"), f"/supervisor/coend/{self.sh.pk}/")
        # the kiosk: a supervisor's PIN first
        engine.execute("coOpen", {}, self.op)
        engine.execute("coEnd", {"checks": ALL}, self.op)
        k = Client(HTTP_HX_REQUEST="true")
        k.post("/kiosk/do/station", {"line": "Barbara"})
        k.post("/kiosk/do/login", {"pin": "1111"})
        self.assertContains(k.get("/kiosk/m/changeover"), "Force end - Supervisor")
        self.assertContains(k.get("/kiosk/m/coforce"), "Force-ending a changeover needs a supervisor")
