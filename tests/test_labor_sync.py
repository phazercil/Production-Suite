"""The Labor Budget in step with the suite: lines, stages, speeds, shifts, work week and people."""
import json

from django.core.cache import cache
from django.test import Client, TestCase

from labor import sync
from labor.models import LaborPlan
from labor.plan import LaborPlanView
from plant.demo import seed_demo
from plant.models import Machine, Shift


def post(c, path, data):
    return c.post(path, json.dumps(data), content_type="application/json").json()


def lead():
    c = Client()
    assert post(c, "/api/hmi/login", {"pin": "4444"})["ok"]   # a shift lead
    return c


OLD_PLAN = {   # a plan made before the suite linked its lines: names only, its own speed and stage
    "schedules": [{"id": "S1A", "shift": 1, "code": "A", "name": "Operators", "startTime": "07:00", "endTime": "15:00", "lunchMins": 45,
                   "paidLunch": True, "daysOfWeek": [1, 2, 3, 4], "enabled": True},
                  {"id": "S1B", "shift": 1, "code": "B", "name": "Packaging", "startTime": "08:00", "endTime": "16:00", "lunchMins": 30,
                   "paidLunch": True, "daysOfWeek": [1, 2, 3, 4, 5], "enabled": True}],
    "calendar": {"defaultWorkDays": [1, 2, 3], "overrides": {"2030-01-01": "HOLIDAY"}},
    "positionsDefaults": [{"title": "Operator", "defaultWage": 20}],
    "slotAssignments": {"LB_1_Operator_0": {"name": "J. Ortiz", "wage": 22}, "LB_1_Operator_1": {"name": "Somebody Else", "wage": 19}},
    "lines": [{"id": "LB", "name": "barbara", "stage": "Stage 2", "hourlyRate": 5, "color": "#000000",
               "shiftPositions": {"1": [{"title": "Operator", "reqCount": 2}]}, "shiftSettings": {"1": {"downtimePct": 13, "scrapPct": 2}}},
              {"id": "LP", "name": "Pack-out", "stage": "Packaging Only", "shiftPositions": {"1": [{"title": "Packer", "reqCount": 4}]}},
              {"id": "LT", "name": "Tone", "stage": "Stage 1"}],
}


class LaborSyncTests(TestCase):
    def setUp(self):
        cache.clear()
        seed_demo(reset=True)

    def merged(self, plan=None):
        return sync.merge(json.loads(json.dumps(plan or OLD_PLAN)))

    def test_lines_come_from_the_suite_and_keep_their_crews(self):
        data, notes = self.merged()
        names = [ln["name"] for ln in data["lines"]]
        machines = list(Machine.objects.order_by("sort", "id"))
        self.assertEqual(names[:len(machines)], [m.name for m in machines])        # every machine, in the suite's order
        bar = data["lines"][0]
        self.assertEqual((bar["id"], bar["machineId"], bar["stage"], bar["color"]), ("LB", machines[0].id, "Stage 1", "#c46c3c"))
        self.assertEqual(bar["shiftPositions"]["1"], [{"title": "Operator", "reqCount": 2}])  # the crew stays
        self.assertEqual(bar["shiftSettings"]["1"]["downtimePct"], 13)
        self.assertGreater(bar["hourlyRate"], 5)                                    # the suite's speed
        self.assertIn("PPM", bar["rateBasis"])
        stages = {ln["name"]: ln["stage"] for ln in data["lines"]}
        self.assertEqual(stages["William"], "Stage 2")
        self.assertEqual(stages["Arts & Crafts"], "Stage 3")
        pack = next(ln for ln in data["lines"] if ln["name"] == "Pack-out")         # the plan's own labor-only line
        self.assertNotIn("machineId", pack)
        self.assertFalse(pack.get("suiteMissing"))
        tone = next(ln for ln in data["lines"] if ln["name"] == "Tone")            # a production line the suite doesn't have
        self.assertTrue(tone["suiteMissing"])
        self.assertTrue(any("Added line Salvatore" in n for n in notes))

    def test_shifts_and_work_week(self):
        data, _ = self.merged()
        s1 = Shift.objects.get(name="1st shift")
        a = next(x for x in data["schedules"] if x["id"] == "S1A")
        self.assertEqual((a["startTime"], a["endTime"], a["daysOfWeek"], a["lunchMins"], a["suiteShift"]), ("06:00", "14:30", [1, 2, 3, 4, 5], 30, s1.id))
        self.assertTrue(a["paidLunch"])                                             # the plan's own choice stays
        b = next(x for x in data["schedules"] if x["id"] == "S1B")                 # extra crew schedules stay the plan's
        self.assertEqual(b["startTime"], "08:00")
        self.assertNotIn("suiteShift", b)
        self.assertTrue(any(x.get("suiteShift") == Shift.objects.get(name="2nd shift").id for x in data["schedules"]))
        self.assertEqual(data["shiftCount"], 2)
        self.assertEqual(data["calendar"]["defaultWorkDays"], [1, 2, 3, 4, 5])
        self.assertEqual(data["calendar"]["overrides"], {"2030-01-01": "HOLIDAY"})  # holidays stay the plan's

    def test_people(self):
        data, _ = self.merged()
        self.assertIn("J. Ortiz", data["suite"]["people"])
        self.assertEqual(data["suite"]["notOnTeam"], ["Somebody Else"])

    def test_the_labor_budget_is_the_master_of_lines_and_shifts(self):
        c = lead()
        data, _ = self.merged()
        bar_id = data["lines"][0]["machineId"]
        data["lines"][0]["hourlyRate"] = 1                                          # a machine setting: stays the suite's
        data["lines"][0]["name"] = "Barbara II"                                     # renamed here: the machine follows
        sal = next(ln for ln in data["lines"] if ln["name"] == "Salvatore")
        sal["activeShifts"]["2"] = False                                            # which shifts it runs
        a = next(x for x in data["schedules"] if x["id"] == "S1A")
        a.update(startTime="07:00", endTime="15:30", lunchMins=45)                  # shift times and lunch
        data["lines"].append({"id": "LNEW", "name": "Hilda", "stage": "Stage 1", "activeShifts": {"1": True, "2": False}})
        r = post(c, "/api/labor/save", {"version": 0, "data": data})
        self.assertTrue(r["ok"], r)
        bar = Machine.objects.get(pk=bar_id)
        self.assertEqual(bar.name, "Barbara II")
        stored = LaborPlan.objects.get(pk=1).data
        self.assertGreater(next(ln for ln in stored["lines"] if ln["name"] == "Barbara II")["hourlyRate"], 1)
        self.assertEqual([s.name for s in Machine.objects.get(name="Salvatore").shifts.all()], ["1st shift"])
        s1 = Shift.objects.get(name="1st shift")
        self.assertEqual((s1.start, s1.end), ("07:00", "15:30"))
        lunch = s1.breaks.get(label__icontains="lunch")
        from plant.util import hm
        self.assertEqual((hm(lunch.end) - hm(lunch.start)) % 1440, 45)
        hilda = Machine.objects.get(name="Hilda")                                   # added here: a new machine
        self.assertEqual((hilda.stage, [s.name for s in hilda.shifts.all()]), ("s1", ["1st shift"]))
        self.assertEqual(next(ln for ln in stored["lines"] if ln["name"] == "Hilda")["machineId"], hilda.id)
        # Admin can't rename it any more; the Labor Budget can
        a2 = Client()
        assert post(a2, "/api/hmi/login", {"pin": "9999"})["ok"]
        r = post(a2, "/api/hmi/cmd", {"name": "lineSet", "p": {"id": "Hilda", "f": "id", "v": "X"}, "line": "Hilda"})
        self.assertFalse(r["ok"])
        self.assertIn("Labor Budget", r["error"])
        # deleting it in the Labor Budget takes the machine out of service, and it isn't brought back
        st = c.get("/api/labor/state").json()
        d2 = st["data"]
        d2["lines"] = [ln for ln in d2["lines"] if ln["name"] != "Hilda"]
        self.assertTrue(post(c, "/api/labor/save", {"version": st["version"], "data": d2})["ok"])
        self.assertFalse(Machine.objects.get(pk=hilda.pk).active)
        self.assertNotIn("Hilda", [ln["name"] for ln in c.get("/api/labor/state").json()["data"]["lines"]])

    def test_a_plan_without_days_or_lunch_leaves_them_alone(self):
        from labor.sync import apply_to_suite
        s1 = Shift.objects.get(name="1st shift")
        days, brks = list(s1.days), list(s1.breaks.values_list("label", "start", "end"))
        apply_to_suite({"shiftCount": 2, "schedules": [{"id": "S1A", "shift": 1, "startTime": s1.start, "endTime": s1.end}], "lines": []})
        s1.refresh_from_db()
        self.assertEqual(list(s1.days), days)
        self.assertEqual(list(s1.breaks.values_list("label", "start", "end")), brks)

    def test_lines_linked_to_a_machine_that_is_gone_relink_by_name(self):
        data, _ = self.merged()
        for ln in data["lines"]:
            if ln.get("machineId"):
                ln["machineId"] += 1000                                             # e.g. machines recreated by a restore
        from labor.sync import merge
        again, notes = merge(data)
        bar = Machine.objects.get(name="Barbara")
        self.assertEqual(sum(1 for ln in again["lines"] if ln["name"] == "Barbara"), 1)
        self.assertEqual(next(ln for ln in again["lines"] if ln["name"] == "Barbara")["machineId"], bar.id)
        self.assertFalse(any("no longer in the suite" in n for n in notes))

    def test_admin_renames_until_a_plan_is_saved(self):
        LaborPlan.objects.create(pk=1, data=json.loads(json.dumps(OLD_PLAN)), version=1)   # saved before lines were linked
        LaborPlan.objects.filter(pk=1).update(data={**OLD_PLAN, "lines": []})
        c = Client()
        assert post(c, "/api/hmi/login", {"pin": "9999"})["ok"]
        r = post(c, "/api/hmi/cmd", {"name": "lineSet", "p": {"id": "Barbara", "f": "id", "v": "Barbara II"}, "line": "Barbara"})
        self.assertTrue(r["ok"], r)                                                 # a plan with no lines isn't the master yet

    def test_a_new_plan_starts_from_the_plant(self):
        c = lead()
        r = c.get("/labor/")
        boot = json.loads(r.content.decode().split('<script type="application/json" id="labor-boot">')[1].split("</script>")[0])
        self.assertIsNone(boot["budgets"]["A"]["data"])
        self.assertEqual([ln["name"] for ln in boot["starter"]["lines"]], [m.name for m in Machine.objects.order_by("sort", "id")])
        self.assertIn('list="suite-people"', r.content.decode())


    def test_budget_b_is_its_own(self):
        c = lead()
        data, _ = self.merged()
        data["projectName"] = "What if"
        sal = next(ln for ln in data["lines"] if ln["name"] == "Salvatore")
        sal["activeShifts"]["2"] = False
        r = post(c, "/api/labor/save", {"budget": "B", "version": 0, "data": data})
        self.assertTrue(r["ok"], r)
        self.assertEqual(LaborPlan.load("B").data["projectName"], "What if")
        self.assertEqual(LaborPlan.load("A").version, 0)                            # A untouched
        self.assertEqual(len(Machine.objects.get(name="Salvatore").shifts.all()), 2)  # only budget A changes the machines
        self.assertEqual(len(c.get("/api/labor/history?budget=B").json()["items"]), 1)
        self.assertEqual(c.get("/api/labor/history?budget=A").json()["items"], [])
