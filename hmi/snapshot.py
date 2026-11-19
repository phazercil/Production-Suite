"""What a kiosk screen receives. PINs only go to a shift lead's screen."""
from datetime import timedelta

from django.conf import settings
from django.db import connections
from django.db.models import Q

from plant.models import MO, Machine, PartSpec, Person, PlantSettings, Shift, part_key
from plant.util import Catalog, hm, iso_date, ms, now, part_spec, shift_at, shift_windows, sod_ms
from scheduler.engine import eta, load_world

from plant import perms

from . import integrations
from .engine import shift_stats
from .models import (AuditLog, Downtime, LineState, MachineError, MoRun, Notice, PpmReading, QCCheck, RollTag, ShiftNote, ShiftRun, Ticket,
                     TicketPhoto)

QDEF = {
    "changeover_steps": {"title": "Changeover steps", "help": "One row per step, in the order operators do them.", "order": True,
                         "roles": [["step", "Step text", 1], ["seq", "Step order"], ["detail", "Detail / note"], ["setting", "Setting name"],
                                   ["value", "Setting value"], ["unit", "Unit"], ["asset", "Asset / tool"], ["image", "Photo URL"]]},
    "asset_settings": {"title": "Machine settings for the part", "help": "One row per setting, shown above the steps (pleat height, glue temperature, blade, dies…).",
                       "roles": [["name", "Setting", 1], ["value", "Value", 1], ["unit", "Unit"], ["asset", "Asset"]]},
    "part_spec": {"title": "Part specs (drawing)", "help": "One row for the part. \"Get updated drawing\" copies these into Parts & specs, so QC checks use them.",
                  "perPart": True,
                  "roles": [["rev", "Drawing rev"], ["pleats", "Pleats per unit"], ["len", "Length"], ["wid", "Width"], ["ph", "Pleat height"],
                            ["sb", "Side band height"], ["tol", "Tolerance ±"], ["media", "Media"],
                            ["uom", "Unit of measure (mm, in…): each dimension's own"]]},
    "part_drawing": {"title": "Drawing file", "help": "The part's drawing: its rev and a link to the file (PDF or image, e.g. on AWS). \"Get updated drawing\" "
                                                  "keeps them, and operators get an Open drawing button. Its rev wins over Part specs' rev.",
                     "perPart": True,
                     "roles": [["url", "Drawing link (PDF or image)"], ["rev", "Drawing rev"],
                               ["newest", "Newest first by (date or rev), when the part has a row per rev"]]},
    "part_tooling": {"title": "Tooling for the part", "help": "What the run screen shows under Up Next and Current MO. Either one row per tool (Tool type + Tool), "
                                                                "or one row with a column each for screws, helix, media and side bands.",
                     "roles": [["kind", "Tool type (screws, helix, media, side band)"], ["name", "Tool / part number"], ["detail", "Size or note"],
                               ["screws", "Screws"], ["helix", "Helix"], ["media", "Media"], ["sideband", "Side bands"]]},
}



def qdef():
    """QDEF as it is now: Part specs also reads each measurement added in Quality › Tolerances (role = its key)."""
    import copy
    from . import qc
    d = copy.deepcopy(QDEF)
    roles = d["part_spec"]["roles"]
    i = next((n for n, r in enumerate(roles) if r[0] == "uom"), len(roles))
    try:
        feats = qc.features()
    except Exception:  # noqa: BLE001 - no settings to read (no database): the standard roles
        feats = []
    d["part_spec"]["roles"] = roles[:i] + [[f["key"], f["label"]] for f in feats] + roles[i:]
    return d


def _influx_info():
    from . import influx
    c = influx.config()
    return {"url": c["url"], "version": c["version"], "out": c["out"] if influx.out_ready(c) else ""}

def mo_json(m):
    return {"id": m.id, "added": m.added.isoformat() if m.added else "", "mo": m.number, "due": m.due.isoformat() if m.due else "", "qty": m.qty,
            "item": m.part, "process": m.process or (m.line.code if m.line else ""), "mfg": m.mfg, "produced": m.produced, "scrap": m.scrap,
            "status": m.status, "hot": m.hot, "line": m.line.name if m.line else None, "seq": m.seq, "planStart": ms(m.plan_start),
            "planEnd": ms(m.plan_end), "planCo": m.plan_co, "source": m.source}


def shift_json(sh):
    if not sh:
        return None
    return {"id": sh.id, "active": sh.active, "line": sh.line, "shiftName": sh.shift_name, "user": sh.person_id, "userName": sh.user_name,
            "start": ms(sh.start), "end": ms(sh.end), "moId": sh.mo_id, "moStart": ms(sh.mo_start), "count": sh.count, "moCount": sh.mo_count,
            "state": sh.state, "stopStart": ms(sh.stop_start), "co": sh.co, "override": sh.override, "issues": sh.issues,
            "endIssues": sh.end_issues, "drawChecked": ms(sh.draw_checked)}


def autoqc_summary(t):
    """Machine vision QC per line over the last 8 hours: how many measured, how many failed, the latest fails."""
    from django.db.models import Count
    from django.db.models import Q as DQ

    from .models import AutoQC
    since = t - timedelta(hours=8)
    out = {}
    for r in AutoQC.objects.filter(t__gte=since).values("line").annotate(n=Count("id"), fails=Count("id", filter=DQ(passed=False))):
        out[r["line"]] = {"n": r["n"], "fails": r["fails"], "last": [], "sources": []}
    for line in out:
        out[line]["last"] = [{"t": ms(q.t), "item": q.item, "mo": q.mo, "len": q.len, "wid": q.wid, "ph": q.ph, "sbl": q.sbl, "sbr": q.sbr,
                              "src": q.source_name} for q in AutoQC.objects.filter(line=line, t__gte=since, passed=False).order_by("-t")[:5]]
        out[line]["sources"] = sorted(set(AutoQC.objects.filter(line=line, t__gte=since).values_list("source_name", flat=True)[:200]))
    return out


def network_info(st):
    from plant import netsafety
    nets, source = netsafety.current(with_source=True)
    net = st.network or {}
    return {"allowed": [{"range": str(n), "desc": netsafety.describe(n)} for n in nets], "source": source,
            "by": net.get("by", ""), "at": net.get("at", ""), "firewall": net.get("firewall") or None,
            "default": netsafety.DEFAULT_NETWORKS[0]}


def downtime_json(qs):
    """Downtime with the errors and alarms the machine reported during each stop."""
    from . import machineerrors
    downs = list(qs)
    errs = machineerrors.for_downtime(downs)
    return [{"id": d.id, "shift": d.shift_id, "line": d.line, "mo": d.mo, "start": ms(d.start_time), "end": ms(d.end_time), "mins": d.minutes,
             "reason": d.reason, "notes": d.notes, "by": d.by, "errors": errs.get(d.id, [])} for d in downs]


def tickets_json(qs):
    tickets = list(qs)
    pics = {}
    for p in TicketPhoto.objects.filter(ticket__in=tickets).values("id", "ticket_id", "by", "t", "width", "height"):
        pics.setdefault(p["ticket_id"], []).append({"id": p["id"], "by": p["by"], "t": ms(p["t"]), "w": p["width"], "h": p["height"]})
    return [{"id": k.id, "shift": k.shift_id, "t": ms(k.t), "line": k.line, "by": k.by, "machine": k.machine, "type": k.type,
             "priority": k.priority, "notes": k.notes, "status": k.status, "photos": pics.get(k.id, [])} for k in tickets]


def build(me=None, station=None, light=False):
    """Full state for the kiosk. `light` drops the history tables (Data Kiosk)."""
    t = now()
    st = PlantSettings.load()
    since = t - timedelta(days=3)
    machines = list(Machine.objects.filter(stage="s1").prefetch_related("trained_people"))
    lines = [{"id": m.name, "code": m.code, "rated": m.ppm, "color": m.color, "active": m.active,
              "feed": {"type": m.feed_type, "url": m.feed_url, "poll": m.feed_poll, "ppmField": m.feed_ppm_field, "countField": m.feed_count_field,
                       "recipeField": m.feed_recipe_field, "query": m.feed_query, "src": m.feed_src or {}, "uptime": m.uptime_src or {},
                       "errors": m.error_src or {}}}
             for m in machines]
    mos = list(MO.objects.select_related("line").filter(Q(status="OPEN") | Q(updated__gte=t - timedelta(days=30))))
    cat = Catalog()
    specs_db = {p.key: p.as_dict() for p in PartSpec.objects.all()}
    keys = {part_key(m.part) for m in mos} | set(specs_db)
    specs = {k: part_spec(k, specs_db, cat) for k in keys}
    world = None
    runs, views = {}, {}
    for m in machines:
        sh = ShiftRun.objects.filter(machine=m, active=True).select_related("mo").first()
        last = sh or ShiftRun.objects.filter(machine=m).order_by("-start").first()
        ls = LineState.objects.filter(machine=m).first()
        hist = []
        if sh:
            hist = [{"t": ms(r["t"]), "ppm": r["ppm"]} for r in PpmReading.objects.filter(shift=sh).order_by("t").values("t", "ppm")[:1500]]
        stats = shift_stats(sh) if sh else {"mins": 0, "run": 0, "down": 0, "uptime": 100}
        errors = []
        if (m.error_src or {}).get("type"):
            from . import machineerrors
            errors = [machineerrors.as_json(x) for x in MachineError.objects.filter(line=m.name, start__gte=sh.start if sh else t - timedelta(hours=8))
                      .order_by("-start")[:30]]
        runs[m.name] = {"shift": shift_json(last), "hist": hist, "qcLast": ms(sh.qc_last) if sh else 0, "feed": (ls.feed if ls else {}) or {},
                        "stats": stats, "errors": errors, "errLog": (ls.err_state or {}) if ls else {}}
        fin = None
        if sh and sh.mo and sh.state != "changeover":
            world = world or load_world()
            mo = world["mos"].get(sh.mo_id)
            left = max(0, sh.mo.qty - sh.mo.produced)
            fin = eta(m, left, ms(t), world, cat.item_for(mo) if mo else None, mo) if mo else None
        views[m.name] = {"eta": round(fin) if fin else None}
    users = [{"id": u.id, "name": u.name, "role": u.role, "trained": [x.name for x in u.trained.all()], "active": u.active, "source": u.source,
              "eecode": u.eecode, "dept": u.dept, "position": u.position, "leftList": u.left_list, "hasPin": bool(u.pin),
              **({"pin": u.pin} if perms.can(me, "team.manage") and (u.role != "admin" or perms.can(me, "permissions.manage")) else {})}
             for u in Person.objects.prefetch_related("trained")]
    shifts = []
    for sh in Shift.objects.prefetch_related("breaks"):
        shifts.append({"name": sh.name, "start": sh.start, "end": sh.end, "days": sh.days,
                       "breaks": [{"name": b.label, "start": b.start, "mins": (hm(b.end) - hm(b.start)) % 1440} for b in sh.breaks.all()]})
    today = sod_ms(ms(t))
    cfg = dict(st.config)
    cfg["parts"] = {k: {f: v[f] for f in ("pleats", "len", "wid", "ph", "sb", "tol", "rev", "media")} for k, v in specs_db.items()}
    cfg["shifts"] = shifts
    w = shift_at(ms(t))
    snap = {
        "v": st.version, "demo": st.demo, "now": ms(t), "lines": lines, "mos": [mo_json(m) for m in mos], "config": cfg, "settings": st.settings,
        "runs": runs, "views": views, "users": users, "specs": specs, "conn": st.conn, "planAt": ms(st.plan_at), "qdef": QDEF,
        "shiftNow": w["name"] if w else None,
        "today": {"windows": shift_windows(today), "day": today},
        "notices": [{"id": n.id, "t": ms(n.t), "text": n.text, "kind": n.kind, "read": n.read} for n in Notice.objects.all()[:30]],
    }
    snap["autoqc"] = autoqc_summary(t)
    if perms.can(me, "network.edit"):
        snap["network"] = network_info(st)
    if light:
        return snap
    snap.update({
        "downtime": downtime_json(Downtime.objects.filter(Q(end_time__gte=since) | Q(reason__isnull=True))),
        "qc": [{"id": q.id, "shift": q.shift_id, "t": ms(q.t), "line": q.line, "mo": q.mo, "item": q.item, "by": q.by, "len": q.len, "wid": q.wid, "ph": q.ph,
                "sbl": q.sbl, "sbr": q.sbr, "glue": q.glue, "cut": q.cut, "scrap": q.scrap, "pass": q.passed} for q in QCCheck.objects.filter(t__gte=since)],
        "rolls": [{"id": r.id, "shift": r.shift_id, "t": ms(r.t), "line": r.line, "roll": r.roll, "material": r.material, "lot": r.lot, "feet": r.feet,
                   "mo": r.mo, "countAt": r.count_at, "by": r.by} for r in RollTag.objects.filter(t__gte=since)],
        "tickets": tickets_json(Ticket.objects.filter(Q(t__gte=since) | ~Q(status="Closed"))),
        "notes": [{"id": n.id, "shift": n.shift_id, "t": ms(n.t), "line": n.line, "by": n.by, "text": n.text} for n in ShiftNote.objects.filter(t__gte=since)],
        "moLog": [{"id": r.id, "shift": r.shift_id, "line": r.line, "mo": r.mo, "item": r.item, "qty": r.qty, "scrap": r.scrap, "start": ms(r.start_time),
                   "end": ms(r.end_time)} for r in MoRun.objects.filter(end_time__gte=since)],
        "audit": [{"t": ms(a.t), "by": a.by, "what": a.what} for a in AuditLog.objects.all()[:30]],
    })
    return snap


def server_info():
    db = connections["default"]
    ok, err = True, ""
    try:
        with db.cursor() as c:
            c.execute("SELECT 1")
    except Exception as e:
        ok, err = False, str(e)
    return {"server": True, "version": settings.SUITE["VERSION"], "revision": settings.SUITE["REVISION"], "store": "mariadb" if db.vendor == "mysql" else db.vendor, "storeOk": ok,
            "storeErr": err, "assets": integrations.assets_ready(), "s2kdb": integrations.s2k_db_ready(), "machinesdb": integrations.machines_db_ready(), "paycom": integrations.paycom_ready(), "speed": 1,
            "influx": _influx_info()}
