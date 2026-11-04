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

    def test_saving_keeps_the_suite_and_writes_running_shifts_back(self):
        c = lead()
        data, _ = self.merged()
        data["lines"][0]["hourlyRate"] = 1                                          # can't be changed here
        data["lines"][0]["name"] = "Renamed here"
        sal = next(ln for ln in data["lines"] if ln["name"] == "Salvatore")
        sal["activeShifts"]["2"] = False                                            # this can, and the Scheduler follows
        r = post(c, "/api/labor/save", {"version": 0, "data": data})
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["suiteChanged"])
        stored = LaborPlan.objects.get(pk=1).data
        self.assertEqual(stored["lines"][0]["name"], "Barbara")
        self.assertGreater(stored["lines"][0]["hourlyRate"], 1)
        self.assertEqual([s.name for s in Machine.objects.get(name="Salvatore").shifts.all()], ["1st shift"])
        self.assertFalse(next(ln for ln in stored["lines"] if ln["name"] == "Salvatore")["activeShifts"]["2"])
        # the Scheduler turning it back on shows in the Labor Budget
        Machine.objects.get(name="Salvatore").shifts.add(Shift.objects.get(name="2nd shift"))
        st = c.get("/api/labor/state").json()
        self.assertTrue(next(ln for ln in st["data"]["lines"] if ln["name"] == "Salvatore")["activeShifts"]["2"])

    def test_a_renamed_machine_keeps_its_crew(self):
        LaborPlan.objects.create(pk=1, data=json.loads(json.dumps(OLD_PLAN)), version=1)   # saved before lines were linked
        c = Client()
        assert post(c, "/api/hmi/login", {"pin": "9999"})["ok"]
        r = post(c, "/api/hmi/cmd", {"name": "lineSet", "p": {"id": "Barbara", "f": "id", "v": "Barbara II"}, "line": "Barbara"})
        self.assertTrue(r["ok"], r)
        view = LaborPlanView.current()
        ln = view.line_for("Barbara II")
        self.assertEqual(ln["id"], "LB")
        self.assertEqual(ln["shiftPositions"]["1"][0]["reqCount"], 2)

    def test_a_new_plan_starts_from_the_plant(self):
        c = lead()
        r = c.get("/labor/")
        boot = json.loads(r.content.decode().split('<script type="application/json" id="labor-boot">')[1].split("</script>")[0])
        self.assertIsNone(boot["data"])
        self.assertEqual([ln["name"] for ln in boot["starter"]["lines"]], [m.name for m in Machine.objects.order_by("sort", "id")])
        self.assertIn('list="suite-people"', r.content.decode())
