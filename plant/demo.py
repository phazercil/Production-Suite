"""Demo plant: two pleaters, four downstream stations, a running shift on Barbara and a small MO backlog.
`python manage.py seed_demo` loads it; Operator › Admin › Backup can clear it to go live."""
from datetime import date, timedelta

from django.db import transaction

from .models import MO, CostItem, Machine, PartSpec, Person, PlantSettings, Shift, ShiftBreak
from .util import ms, now, shift_at

PARTS = {
    "MG4062HP": dict(pleats=42, len=9.13, wid=8.82, ph=1.18, sb=1.25, tol=0.06, rev="C", media="Carbon composite, 2-ply"),
    "MG3045": dict(pleats=30, len=10.24, wid=7.87, ph=1.00, sb=1.10, tol=0.06, rev="B", media="Synthetic, 1-ply"),
    "MG3686": dict(pleats=24, len=8.66, wid=7.48, ph=0.98, sb=1.05, tol=0.06, rev="D", media="Synthetic, 1-ply"),
    "MG3686HP": dict(pleats=16, len=8.66, wid=7.48, ph=0.98, sb=1.05, tol=0.06, rev="A", media="HEPA-grade, 1-ply"),
    "MG4108": dict(pleats=36, len=9.45, wid=8.27, ph=1.10, sb=1.20, tol=0.06, rev="B", media="Carbon composite, 2-ply"),
    "AQ1182": dict(pleats=28, len=11.02, wid=8.07, ph=1.26, sb=1.35, tol=0.08, rev="E", media="Cellulose blend"),
}


def ensure_basics():
    """Cost sheet and settings row; safe to run on a live plant."""
    from scheduler.imports import builtin_catalog
    st = PlantSettings.load()
    if not CostItem.objects.exists():
        items, src = builtin_catalog()
        CostItem.objects.bulk_create(items)
        st.catalog_source = {"source": src, "builtin": True}
        st.save(update_fields=["catalog_source"])
    return st


def wipe():
    from hmi import models as H
    from scheduler.models import Job
    for model in (H.PpmReading, H.Downtime, H.QCCheck, H.RollTag, H.Ticket, H.ShiftNote, H.MoRun, H.Notice, H.AuditLog, H.Event, H.MachineError,
                  H.ChangeoverCache, H.ToolingCache, H.LineState, H.ShiftRun, Job, MO, Person, PartSpec, ShiftBreak):
        model.objects.all().delete()
    Machine.objects.all().delete()
    Shift.all_objects.all().delete()


@transaction.atomic
def seed_demo(reset=True):
    from hmi import engine
    from hmi.models import Downtime, LineState, Notice, QCCheck, RollTag, ShiftRun, Ticket
    from hmi.ticker import demo_sample
    from scheduler.engine import refresh_floor_queue
    from scheduler.models import Job

    if reset:
        wipe()
    st = ensure_basics()
    t = now().replace(second=0, microsecond=0)

    s1 = Shift.objects.create(name="1st shift", start="06:00", end="14:30", days=[1, 2, 3, 4, 5], sort=0)
    s2 = Shift.objects.create(name="2nd shift", start="14:30", end="23:00", days=[1, 2, 3, 4, 5], sort=1)
    for sh, brs in ((s1, [("Break", "08:30", "08:45"), ("Lunch", "10:30", "11:00"), ("Break", "12:45", "13:00")]),
                    (s2, [("Break", "16:30", "16:45"), ("Lunch", "18:30", "19:00"), ("Break", "20:45", "21:00")])):
        for lb, a, b in brs:
            ShiftBreak.objects.create(shift=sh, label=lb, start=a, end=b)

    def machine(name, **kw):
        shifts = kw.pop("shifts", [s1, s2])
        m = Machine.objects.create(name=name, **kw)
        m.shifts.set(shifts)
        return m

    bar = machine("Barbara", code="BARB", stage="s1", speed_mode="new", ppm=285, scrap_pct=5, downtime_pct=10, changeover_min=30, run_lunch=True,
                  color="#c46c3c", feed_type="demo", demo_scale=1, demo_offset=0, sort=0)
    sal = machine("Salvatore", code="SALV", stage="s1", speed_mode="old", ppm=180, scrap_pct=5, downtime_pct=10, changeover_min=30,
                  color="#5e9951", feed_type="demo", demo_scale=0.65, demo_offset=140, sort=1)
    wil = machine("William", code="WILL", stage="eb", speed_mode="mask", rate=360, scrap_pct=2, downtime_pct=5, changeover_min=15, color="#3f7fb5", sort=2)
    machine("John Sr.", code="JSR", stage="eb", speed_mode="fixed", rate=360, scrap_pct=2, downtime_pct=5, changeover_min=15, color="#8c5bb0",
            shifts=[s1], sort=3)
    machine("Arts & Crafts", code="AC", stage="ac", speed_mode="sheet", rate=120, scrap_pct=2, downtime_pct=5, changeover_min=10, color="#b5933a",
            shifts=[s1], sort=4)
    machine("Glue Bead", code="GB", stage="gb", speed_mode="fixed", rate=300, scrap_pct=2, downtime_pct=5, changeover_min=10, color="#3a9a98",
            shifts=[s1], sort=5)

    for k, v in PARTS.items():
        PartSpec.objects.create(key=k, **v)

    people = {}
    for pid, name, pin, role, trained in (("jortiz", "J. Ortiz", "1111", "operator", [bar]), ("mlee", "M. Lee", "2222", "operator", [bar, sal]),
                                          ("kpatel", "K. Patel", "3333", "maintenance", [bar, sal]), ("slead", "S. Lead", "4444", "lead", [bar, sal]),
                                          ("lead", "Plant Admin", "9999", "admin", [bar, sal])):
        p = Person.objects.create(name=name, pin=pin, role=role, ext_id=pid)
        p.trained.set(trained)
        people[pid] = p

    today = t.date()

    def mo(number, part, qty, due_days, **kw):
        return MO.objects.create(number=str(number), part=part, qty=qty, due=today + timedelta(days=due_days), process=kw.pop("process", ""),
                                 ops=kw.pop("ops", {"s1": True, "eb": False, "ac": False, "gb": False}), source="demo", example=True, **kw)

    running = mo(309215, "MG4062HP", 1500, 2, process="BARB", ops={"s1": True, "eb": True, "ac": False, "gb": False})
    q2 = mo(306785, "MG3045", 32000, 10, process="BARB", pleats=30)
    q3 = mo(306855, "MG3686", 3750, 1, process="SALV")
    q4 = mo(306856, "MG3686HP", 8750, 3, process="SALV")
    mo(308627, "MG4031", 7992, 6, process="BARB")
    mo(308147, "MG4017", 1260, -1, process="BARB")
    mo(308149, "MG3041", 7158, 8, process="BARB")
    mo(308607, "MG4068", 7500, 4, process="BARB", hot=True, ops={"s1": True, "eb": False, "ac": False, "gb": True})
    mo(309099, "MG4108", 7248, 9, process="BARB", ops={"s1": True, "eb": True, "ac": False, "gb": False})

    start = t - timedelta(minutes=150)
    Job.objects.create(mo=running, op="s1", machine=bar, req_start=start)
    Job.objects.create(mo=q2, op="s1", machine=bar, req_start=t)
    Job.objects.create(mo=q3, op="s1", machine=sal, req_start=t)
    Job.objects.create(mo=q4, op="s1", machine=sal, req_start=t + timedelta(minutes=1))
    Job.objects.create(mo=running, op="eb", machine=wil, req_start=t)

    st.demo = True
    st.save(update_fields=["demo"])
    refresh_floor_queue(by="demo setup")
    Notice.objects.all().delete()

    # a shift on Barbara that started 150 minutes ago, replayed minute by minute
    jo = people["jortiz"]
    sh = ShiftRun.objects.create(machine=bar, line=bar.name, person=jo, user_name=jo.name, shift_name=_demo_window_name(bar, t),
                                 start=start, mo=running, mo_start=start, state="running", safety=[True] * 5, pm=[True] * 5, qc_last=start)
    for m in (bar, sal):
        LineState.objects.create(machine=m, next_tick=t + timedelta(minutes=1))
    ls = LineState.objects.get(machine=bar)
    for i in range(150):
        s = demo_sample(bar, ls)
        ls.save(update_fields=["demo_idx"])
        engine.tick(bar, s, start + timedelta(minutes=i + 1))
    ls.refresh_from_db()
    ls.feed = {"type": "demo", "ok": True, "err": "", "ppm": None, "t": ms(t)}
    ls.save()
    first = Downtime.objects.filter(shift=sh).order_by("start_time").first()
    if first:
        first.reason = "Jam or misfeed"
        first.save()
    for m_ in (5, 35, 65, 95, 125):
        QCCheck.objects.create(shift=sh, t=start + timedelta(minutes=m_), line="Barbara", mo="309215", item="MG4062HP", by=jo.name,
                               len=9.13 + ((m_ % 3) - 1) * 0.02, wid=8.82 + (m_ % 2) * 0.01, ph=1.18 + (0.09 if m_ == 95 else 0), sbl=1.25,
                               sbr=1.24, glue="Pass", cut="Pass", scrap=6 if m_ == 95 else 1, passed=m_ != 95)
    MO.objects.filter(pk=running.pk).update(scrap=10)
    ShiftRun.objects.filter(pk=sh.pk).update(qc_last=start + timedelta(minutes=125))
    RollTag.objects.create(shift=sh, t=start + timedelta(minutes=2), line="Barbara", roll="R-24518", material="Carbon composite, 2-ply",
                           lot="L2603-117", feet=3200, mo="309215", count_at=0, by=jo.name)
    Ticket.objects.create(shift=sh, t=start + timedelta(minutes=70), line="Barbara", by=jo.name, machine="Barbara", type="Pleater jam",
                          priority="Medium", notes="Media walking left at the score wheels after restart.")
    # what the pleater reported during its stops (demo machine errors)
    from hmi.models import MachineError
    demo_errs = [("E214", "Media break detected"), ("E031", "Score wheel guard open"), ("E107", "Glue pot low temperature")]
    for i, d in enumerate(Downtime.objects.filter(shift=sh).order_by("start_time")[:3]):
        code, text = demo_errs[i % len(demo_errs)]
        MachineError.objects.create(machine=bar, line="Barbara", start=d.start_time + timedelta(seconds=20), end=d.end_time, code=code,
                                    text=text, source="feed")
    Notice.objects.create(t=start + timedelta(minutes=120), text="MO 306856 (MG3686HP) moved from Barbara to Salvatore", kind="schedule")
    ShiftRun.objects.filter(pk=sh.pk).update(issues="Side band guide on the left was loose at start-up; tightened it.")
    _demo_history(bar, sal, people, start)
    PlantSettings.bump()


def _demo_window_name(bar, t):
    """The demo run is named for the shift going on now, so it stays one log even when it began in the shift before."""
    from hmi.engine import machine_window
    return machine_window(bar, t)["name"]


def _demo_history(bar, sal, people, cur_start):
    """Finished runs of the running part on both pleaters, so the run screen has averages and shift reports to show."""
    import random

    from hmi.models import Downtime, MoRun, PpmReading, ShiftRun
    rnd = random.Random(7)
    # (machine, hours before the current shift started, length in hours, speed share, scrap per 1000, stops)
    runs = [(bar, 8.5, 8.0, 0.66, 30, 3), (bar, 32.5, 7.5, 0.70, 26, 2), (bar, 56.5, 8.0, 0.62, 35, 4),
            (sal, 30.0, 7.0, 0.80, 44, 3), (sal, 78.0, 6.5, 0.78, 40, 5)]
    who = [people["mlee"], people["jortiz"]]
    reads = []
    for i, (m, back, hrs, share, spk, stops) in enumerate(runs):
        a = cur_start - timedelta(hours=back)
        b = a + timedelta(hours=hrs)
        p = who[i % 2]
        sh = ShiftRun.objects.create(machine=m, line=m.name, person=p, user_name=p.name, shift_name="1st shift" if i % 2 else "2nd shift",
                                     start=a, end=b, active=False, state="idle", safety=[True] * 5, pm=[True] * 5)
        mins = int(hrs * 60)
        down = set()
        for k in range(stops):
            s0 = rnd.randrange(20, mins - 30)
            ln = rnd.randrange(4, 16)
            Downtime.objects.create(shift=sh, line=m.name, mo="309001", start_time=a + timedelta(minutes=s0), end_time=a + timedelta(minutes=s0 + ln),
                                    minutes=ln, reason=rnd.choice(["Jam or misfeed", "Material / media change", "Glue system"]), by=p.name)
            down.update(range(s0, s0 + ln))
        made = 0
        for k in range(mins):
            if k in down:
                reads.append(PpmReading(machine=m, line=m.name, shift=sh, t=a + timedelta(minutes=k), ppm=0, units=0, run=False))
                continue
            ppm = int(m.ppm * share * rnd.uniform(0.9, 1.1))
            u = round(ppm / 42)
            made += u
            reads.append(PpmReading(machine=m, line=m.name, shift=sh, t=a + timedelta(minutes=k), ppm=ppm, units=u, run=True))
        sh.count = made
        if i == 0:
            sh.end_issues = "Glue bead ran thin on the left side late in the shift; cleaned the nozzle and it came back."
        sh.save(update_fields=["count", "end_issues"])
        MoRun.objects.create(shift=sh, line=m.name, mo="309001", item="MG4062HP", qty=made, scrap=round(made * spk / 1000), start_time=a, end_time=b)
    PpmReading.objects.bulk_create(reads, batch_size=2000)
