"""What an Operator Kiosk screen shows, worked out on the server.

Everything a kiosk page used to decide in the browser lives here: which machine the screen is for, whether a
downtime reason or a QC check is due, the alarm list and menu badges, the banner, the run screen's numbers,
what is up next and whether it needs a changeover, part specs, and the changeover step list. Templates only
display what this returns; every button posts to hmi/kviews.py, which runs the plant rules in hmi/engine.py.
"""
from datetime import timedelta
from urllib.parse import quote, urlencode

from django.db.models import Q

from plant.models import MO, Machine, PartSpec, Person, PlantSettings, part_key
from plant.util import Catalog, ms, now, part_spec, shift_at
from suite.svg import speed_chart

from . import integrations
from .engine import shift_stats
from .models import Downtime, LineState, MachineError, Notice, PpmReading, ShiftRun, Ticket

PAGES = [("controls", "Run screen"), ("schedule", "Schedule"), ("qc-log", "QC Log"), ("downtime", "Downtime"), ("maintenance", "Maintenance"),
         ("status", "Status"), ("end-of-shift", "End of Shift"), None, ("team", "Team")]
LOCKED_OUT = {"team", "admin"}           # not on a machine's own screen (/station/<machine>/)
ADMIN_PAGES = {"admin"}
STATE_PILL = {"running": ("ok", "Running"), "changeover": ("warn", "Changeover"), "stopped": ("bad", "Stopped")}
PHOTO_LIMIT = 6


def hms(seconds):
    s = max(0, int(seconds))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


class Screen:
    """One kiosk screen at one moment: the person logged in, the machine it is for, and what follows from them."""

    def __init__(self, request, me=None, lock="", sup=None):
        self.request = request
        self.t = now()
        self.me = me
        self.sup = sup   # a supervisor who has taken over this screen (Supervisor button)
        self.lock = lock
        self.st = PlantSettings.load()
        self.cfg = self.st.config
        self.settings = self.st.settings
        self.rules = self.cfg["rules"]
        self.lines = list(Machine.objects.filter(stage="s1").order_by("sort", "id"))
        self.active_lines = [m for m in self.lines if m.active]
        names = {m.name for m in self.lines}
        want = lock or request.session.get("station") or ""
        if want not in names:
            want = (self.active_lines or self.lines or [None])[0]
            want = want.name if want else ""
        self.station = want
        self.machine = next((m for m in self.lines if m.name == want), None)
        self.shift = ShiftRun.objects.filter(machine=self.machine, active=True).select_related("mo").first() if self.machine else None
        self.ls = LineState.objects.filter(machine=self.machine).first() if self.machine else None
        self.feed = (self.ls.feed if self.ls else {}) or {}
        self._cat = None
        self._specs = None

    @property
    def line_names(self):
        return [m.name for m in self.lines]

    # ------------------------------------------------------------ who
    @property
    def perms(self):
        """What the person on this screen may do (Admin › Permissions): S.perms.lists_edit in a template."""
        if not hasattr(self, "_perms"):
            from plant.perms import PermView
            self._perms = PermView(self.actor)
        return self._perms

    @property
    def actor(self):
        """Who this screen acts as: the supervisor during an override, otherwise the person logged in."""
        return self.sup or self.me

    def can(self, key):
        return bool(self.perms[key.replace(".", "_")])

    @property
    def is_lead(self):
        """Can open Admin and change the day-to-day lists."""
        return self.can("section.admin")

    @property
    def is_admin(self):
        """Holds Admin-level permissions (used for labels; each feature checks its own permission)."""
        return bool(self.me and self.me.role == "admin")

    @property
    def can_override(self):
        return self.can("downtime.reason")

    @property
    def shift_on(self):
        return bool(self.shift)

    # ------------------------------------------------------------ parts
    def part(self, item):
        if self._specs is None:
            self._specs = {p.key: p.as_dict() for p in PartSpec.objects.all()}
            self._cat = Catalog()
        return part_spec(item or "", self._specs, self._cat)

    def mo(self, pk):
        return MO.objects.filter(pk=pk).first() if pk else None

    @property
    def cur_mo(self):
        return self.shift.mo if self.shift and self.shift.mo_id else None

    def co_mo(self, key):
        co = (self.shift.co or {}) if self.shift else {}
        return self.mo(co.get(key))

    def current_item(self):
        """The part the screen is working on: the running MO, else the one being set up."""
        if self.cur_mo:
            return self.cur_mo.part
        nm = self.co_mo("newMo")
        return nm.part if nm else ""

    # ------------------------------------------------------------ the rules that drive prompts and badges
    def need_reason(self):
        return list(Downtime.objects.filter(reason__isnull=True, line=self.station).order_by("start_time"))

    def unexplained_mins(self):
        return sum(d.minutes or 0 for d in self.need_reason())

    def reason_due(self):
        nr = self.need_reason()
        return bool(nr) and sum(d.minutes or 0 for d in nr) >= (self.rules.get("reasonAfter") or 0)

    def reason_prompt(self):
        """Open the "Why did the line stop?" dialog by itself? Not while it was put off a few minutes ago."""
        snooze = self.request.session.get("down_snooze", 0)
        return self.reason_due() and ms(self.t) > snooze

    def qc_due(self):
        if not self.shift_on or not self.settings.get("autoQC"):
            return False
        last = self.shift.qc_last
        return last is None or (self.t - last).total_seconds() >= (self.settings.get("qcEvery") or 30) * 60

    def qc_next(self):
        if not self.settings.get("autoQC"):
            return "Auto QC off"
        if not self.shift_on:
            return "—"
        last = self.shift.qc_last
        left = (self.settings.get("qcEvery") or 30) - ((self.t - last).total_seconds() / 60 if last else 1e9)
        m = int(round(abs(left)))
        return f"Overdue {m // 60}:{m % 60:02d}" if left <= 0 else f"in {m // 60}:{m % 60:02d}"

    def unread_notices(self):
        return list(Notice.objects.filter(read=False).exclude(kind="team").order_by("-t")[:30])

    def open_tickets(self):
        return list(Ticket.objects.exclude(status="Closed").order_by("t"))

    def current_errors(self, line=None):
        """Errors still showing on a machine, plus alarm-log entries since its current stop began (or the last 10 min)."""
        line = line or self.station
        m = next((x for x in self.lines if x.name == line), None)
        if not m or not (m.error_src or {}).get("type"):
            return []
        sh = self.shift if line == self.station else ShiftRun.objects.filter(machine=m, active=True).first()
        since = (sh.stop_start - timedelta(minutes=2)) if sh and sh.stop_start else self.t - timedelta(minutes=10)
        return list(MachineError.objects.filter(line=line).filter(Q(end__isnull=True) | Q(end__gte=since)).order_by("-start")[:12])

    def alarms(self):
        """Everything that needs someone's attention on this screen, most serious first."""
        from .snapshot import server_info
        a = []
        srv = server_info()
        if not srv["storeOk"]:
            a.append({"sev": "bad", "text": f"The server cannot save records: {srv['storeErr']}", "go": "admin", "href": "/admin/"})
        if self.shift_on and self.feed.get("type") and not self.feed.get("ok", True):
            a.append({"sev": "bad", "text": f"No machine data from {self.station}: {self.feed.get('err') or 'feed offline'}", "go": "status"})
        if self.shift_on and self.feed.get("error"):
            a.append({"sev": "warn", "text": f"{self.station} reports: {self.feed['error']}", "go": "downtime"})
        if self.reason_due():
            n = len(self.need_reason())
            a.append({"sev": "bad", "text": f"{n} downtime {'entries need' if n > 1 else 'entry needs'} a reason", "go": "downtime"})
        if self.qc_due():
            a.append({"sev": "warn", "text": f"QC check due (every {self.settings.get('qcEvery')} min)", "go": "qc", "modal": "qc"})
        for n in self.unread_notices():
            a.append({"sev": "warn", "text": n.text, "go": "schedule", "notice": n.id})
        for tk in self.open_tickets():
            if tk.priority == "High":
                a.append({"sev": "bad", "text": f"Maintenance: {tk.machine} — {tk.type}", "go": "maintenance"})
        return a

    # ------------------------------------------------------------ shell
    def nav(self, page):
        flags = {"schedule": "!" if self.unread_notices() else "", "downtime": len(self.need_reason()) if self.reason_due() else "",
                 "qc-log": "!" if self.qc_due() else "", "maintenance": len(self.open_tickets()) or ""}
        out = []
        for p in PAGES:
            if p is None:
                if not self.lock:
                    out.append(None)
                continue
            key, label = p
            if self.lock and key in LOCKED_OUT:
                continue
            f = flags.get(key, "")
            out.append({"key": key, "label": label, "on": key == page, "badge": f, "flag": key in ("schedule", "downtime", "qc-log") and bool(f),
                        "locked": key in ADMIN_PAGES and not self.is_lead})
        return out

    def banner(self):
        parts = []
        if self.reason_due():
            n = len(self.need_reason())
            parts.append({"modal": "downPrompt", "text": f"{n} downtime {'entries need' if n > 1 else 'entry needs'} a reason"})
        if self.unread_notices():
            parts.append({"page": "schedule", "pre": "The ", "text": "schedule", "post": " changed"})
        if self.qc_due():
            parts.append({"modal": "qc", "text": "QC check", "post": " due"})
        sh = self.shift
        state = ("Changeover in progress" if sh.state == "changeover" else "Line stopped" if sh.state == "stopped" else "Running") if sh else "No shift running"
        w = shift_at(ms(self.t))
        from .snapshot import server_info
        srv = server_info()
        return {"parts": parts, "state": state, "shiftName": w["name"] if w else "Off shift",
                "operator": (sh.user_name if sh else (self.me.name if self.me else "")),
                "storeOk": srv["storeOk"], "storeErr": srv["storeErr"], "demo": self.st.demo, "admin": self.is_lead, "sysadmin": self.is_admin,
                "choose": [m.name for m in self.active_lines] if not self.lock and len(self.active_lines) > 1 else []}

    # ------------------------------------------------------------ run screen
    def grafana_src(self, panel):
        g = self.settings.get("grafana") or {}
        if not (g.get("on") and g.get("url") and g.get("uid") and panel):
            return ""
        q = {"orgId": g.get("org") or 1, "panelId": panel, "from": str(ms(self.shift.start)) if self.shift else "now-12h", "to": "now",
             "refresh": g.get("refresh") or "10s", "theme": "dark"}
        if g.get("lineVar") and self.shift:
            q["var-" + g["lineVar"]] = self.shift.line
        return f"{g['url'].rstrip('/')}/d-solo/{quote(g['uid'], safe='')}/{quote(g.get('slug') or '_', safe='')}?{urlencode(q)}"

    def history(self):
        if not self.shift:
            return []
        return list(PpmReading.objects.filter(shift=self.shift).order_by("t").values_list("t", "ppm")[:1500])

    def up_next(self, line=None):
        """Open MOs planned for the line in order, and whether each one needs a changeover first."""
        line = line or self.station
        run = self.cur_mo if line == self.station else None
        qs = MO.objects.filter(line__name=line, status="OPEN").order_by("seq")
        if run:
            qs = qs.exclude(pk=run.pk)
        prev = run.part if run else (self.co_mo("oldMo").part if self.shift and (self.shift.co or {}).get("oldMo") and self.co_mo("oldMo") else None)
        out = []
        for m in qs:
            out.append({"m": m, "co": prev is None or part_key(prev) != part_key(m.part), "left": max(0, m.qty - m.produced)})
            prev = m.part
        return out

    def eta(self):
        if not (self.shift and self.cur_mo and self.shift.state != "changeover"):
            return None
        from scheduler.engine import eta, load_world
        world = load_world()
        mo = world["mos"].get(self.cur_mo.pk)
        if not mo:
            return None
        left = max(0, self.cur_mo.qty - self.cur_mo.produced)
        fin = eta(self.machine, left, ms(self.t), world, Catalog().item_for(mo), mo)
        return fin

    def run_screen(self):
        sh, mo = self.shift, self.cur_mo
        hist = self.history()
        last_ppm = hist[-1][1] if hist else 0
        st = shift_stats(sh) if sh else {"mins": 0, "run": 0, "down": 0, "uptime": 100}
        nx = self.up_next()
        out = {"on": bool(sh), "sh": sh, "stats": st, "lastPpm": last_ppm, "next": nx[:4], "more": max(0, len(nx) - 4), "first": nx[0] if nx else None,
               "feedWarn": bool(sh and self.feed.get("t") and not self.feed.get("ok", True)), "feedErr": self.feed.get("err", ""),
               "recipe": self.feed.get("recipe", ""), "planAt": self.st.plan_at,
               "grafana": self.grafana_src((self.settings.get("grafana") or {}).get("ppm")),
               "chart": speed_chart(hist, self.machine.ppm if self.machine else 0,
                                    empty="Waiting for the first reading…" if sh else "No shift running",
                                    label=f"Pleater speed for {self.station}, this shift"),
               "alert": self.reason_due() or bool(sh and sh.stop_start), "qcDue": self.qc_due(), "reasonDue": self.reason_due()}
        if sh:
            out["pill"] = STATE_PILL.get(sh.state, ("idle", sh.state.title()))
            co = sh.co or {}  # the changeover start is kept as milliseconds
            out["timerSince"] = co["start"] if sh.state == "changeover" and co.get("start") else (ms(sh.mo_start) if mo and sh.mo_start else None)
        if mo:
            p = self.part(mo.part)
            good = max(0, mo.produced - mo.scrap)
            per = (sh.override or {}).get("pleats") or p["pleats"]
            try:
                per = float(per)
            except (TypeError, ValueError):
                per = p["pleats"]
            out.update(mo=mo, p=p, good=good, pct=min(100, good / mo.qty * 100) if mo.qty else 0,
                       rate=f"{last_ppm / per:.1f}" if per else "—", eta=self.eta())
        return out


def plan_label(t, ref):
    """When a planned run starts: "2:30 pm" today, "Tue 2:30 pm" this week, else "4/9 2:30 pm"."""
    from django.utils import timezone

    from suite.templatetags.suite import clock
    if not t:
        return ""
    lt, lr = timezone.localtime(t), timezone.localtime(ref)
    days = (lt.date() - lr.date()).days
    if days == 0:
        return clock(t)
    if 0 < days < 6:
        return f"{lt:%a} {clock(t)}"
    return f"{lt.month}/{lt.day} {clock(t)}"


def people_for_login(perm=""):
    """Who can log in for this: anyone, or the people who have permission `perm` (Admin › Permissions)."""
    from plant import perms
    qs = Person.objects.filter(active=True)
    return perms.who_can(perm, qs) if perm in perms.PERMS else list(qs)


# ------------------------------------------------------------------ the other operator pages
def _local_ms(t_ms):
    from datetime import datetime

    from django.utils import timezone
    return timezone.localtime(datetime.fromtimestamp(t_ms / 1000, timezone.get_current_timezone()))


def schedule_page(s, view="timeline", status="OPEN", sort=None):
    """Schedule page: today's timeline (positions in pixels), this month's due dates, and the MO list."""
    from datetime import date, timedelta as td

    from plant.util import shift_windows, sod_ms
    from suite.templatetags.suite import clock
    t_ms = ms(s.t)
    out = {"view": view, "status": status, "sort": sort, "notices": s.unread_notices()}
    if view == "timeline":
        wins = shift_windows(sod_ms(t_ms))
        ds = wins[0]["s"] if wins else sod_ms(t_ms) + 360 * 60000
        de = max(w["e"] for w in wins) if wins else ds + 16 * 3600000
        SLOT, SW, LW = 30 * 60000, 66, 190
        slots = max(1, -(-(de - ds) // SLOT))
        total, end = LW + slots * SW, ds + slots * SLOT

        def X(t):
            return LW + (t - ds) / SLOT * SW
        head = [{"x": X(ds + i * SLOT) - LW, "label": clock(_local_ms(ds + i * SLOT)).replace(" am", "").replace(" pm", "")} for i in range(slots)]
        days = [{"x": X(w["s"]) + 4, "label": w["name"] + ("" if i else f" · {_local_ms(ds).month}/{_local_ms(ds).day}/{_local_ms(ds).year}")}
                for i, w in enumerate(wins)]
        brks = [b for w in wins for b in w["brk"]]
        lanes = []
        for m in s.active_lines:
            blocks = []
            sh = ShiftRun.objects.filter(machine=m, active=True).select_related("mo").first()
            for b in line_blocks(s, m, sh, t_ms):
                if b["start"] >= end or b["end"] <= ds:
                    continue
                l0 = max(X(b["start"]), LW)
                w = max(30, min(X(b["end"]), total) - l0 - 4)
                blocks.append({**b, "left": round(l0), "width": round(w), "more": b["end"] > end,
                               "hours": f"{(b['end'] - b['start']) / 3600000:.1f}", "from": clock(_local_ms(b["start"])), "to": clock(_local_ms(b["end"]))})
            for i, b in enumerate(blocks):
                if b["kind"] == "co" and i + 1 < len(blocks):
                    b["eta"] = {"left": round(max(LW, X(blocks[i + 1]["start"]) - 110)), "at": blocks[i + 1]["from"]}
            lanes.append({"m": m, "blocks": blocks,
                          "breaks": [{"left": round(X(b["s"]) + 2), "width": round(max(24, X(b["e"]) - X(b["s"]) - 4)), "name": b["name"],
                                      "from": clock(_local_ms(b["s"])), "to": clock(_local_ms(b["e"]))} for b in brks]})
        out["gantt"] = {"total": total, "LW": LW, "SW": SW, "head": head, "days": days, "lanes": lanes,
                        "now": round(X(t_ms)) if ds < t_ms < end else None, "scrollTo": max(0, round(X(t_ms)) - 320) if ds < t_ms < end else 0}
    else:
        today = s.t.astimezone().date() if False else _local_ms(t_ms).date()
        first = date(today.year, today.month, 1)
        start = first - td(days=(first.weekday() + 1) % 7)
        days = [start + td(days=i) for i in range(42)]
        due = {}
        for mo in MO.objects.filter(status="OPEN", due__gte=days[0], due__lte=days[-1]).select_related("line"):
            due.setdefault(mo.due, []).append(mo)
        out["cal"] = [{"d": d, "out": d.month != today.month, "today": d == today, "mos": due.get(d, [])[:3], "more": max(0, len(due.get(d, [])) - 3)}
                      for d in days]
    rows = list(MO.objects.select_related("line").all() if status == "ALL" else MO.objects.select_related("line").filter(status=status))
    keys = {"added": lambda m: m.added or date.min, "mo": lambda m: m.number, "due": lambda m: m.due or date.max, "qty": lambda m: m.qty,
            "item": lambda m: m.part, "process": lambda m: m.process or "", "mfg": lambda m: m.mfg or "", "produced": lambda m: m.produced,
            "scrap": lambda m: m.scrap, "final": lambda m: m.produced - m.scrap, "status": lambda m: m.status, "line": lambda m: m.line.name if m.line else ""}
    if sort and sort.lstrip("-") in keys:
        rows.sort(key=keys[sort.lstrip("-")], reverse=sort.startswith("-"))
    today = _local_ms(t_ms).date()
    out["rows"] = [{"m": m, "late": m.status == "OPEN" and m.due and m.due < today, "final": m.produced - m.scrap,
                    "process": m.process or (m.line.code if m.line else "")} for m in rows]
    out["cols"] = [("added", "Added to sheet"), ("mo", "MO No."), ("due", "Due date"), ("qty", "Make qty"), ("item", "Item"), ("process", "Process"),
                   ("mfg", "MFG/OUT"), ("produced", "Produced qty"), ("scrap", "Scrap qty"), ("final", "Final qty"), ("status", "Open/Close"), ("line", "Line")]
    return out


def line_blocks(s, m, sh, t_ms):
    """A line's runs on the timeline: the running MO from its actual start to the live finish estimate, planned runs
    with their changeovers, and a changeover in progress."""
    out = []
    eta_ms = None
    for mo in MO.objects.filter(line=m, status="OPEN").order_by("seq"):
        running = bool(sh and sh.mo_id == mo.pk)
        if running:
            if eta_ms is None and sh.state != "changeover":
                from scheduler.engine import eta, load_world
                world = load_world()
                wm = world["mos"].get(mo.pk)
                eta_ms = eta(m, max(0, mo.qty - mo.produced), t_ms, world, Catalog().item_for(wm), wm) if wm else None
            out.append({"kind": "mo", "m": mo, "start": ms(sh.mo_start) or t_ms, "end": max(t_ms, eta_ms or ms(mo.plan_end) or t_ms), "running": True})
            continue
        if not mo.plan_start:
            continue
        ps = ms(mo.plan_start)
        if mo.plan_co:
            out.append({"kind": "co", "start": ps, "end": ps + mo.plan_co * 60000, "mins": round(mo.plan_co)})
        out.append({"kind": "mo", "m": mo, "start": ps + (mo.plan_co or 0) * 60000, "end": ms(mo.plan_end), "running": False})
    if sh and sh.state == "changeover" and (sh.co or {}).get("start"):
        cs = sh.co["start"]
        co_mins = s.rules.get("coMins", 45)
        out.insert(0, {"kind": "co", "start": cs, "end": max(t_ms, cs + co_mins * 60000), "mins": co_mins})
    return out


def qc_log_page(s):
    from .models import QCCheck
    since = s.t - timedelta(days=3)
    checks = list(QCCheck.objects.filter(line=s.station, t__gte=since).order_by("-t"))
    rows = []
    for q in checks:
        p = s.part(q.item)
        target = {"len": p["len"], "wid": p["wid"], "ph": p["ph"], "sbl": p["sb"], "sbr": p["sb"]}
        cells = []
        for k in ("len", "wid", "ph", "sbl", "sbr"):
            v = getattr(q, k)
            cells.append({"v": v, "ok": v is None or abs(v - target[k]) <= p["tol"]})
        rows.append({"q": q, "cells": cells})
    from .snapshot import autoqc_summary
    auto = autoqc_summary(s.t).get(s.station)
    if auto:
        auto["pct"] = round(100 * (auto["n"] - auto["fails"]) / auto["n"]) if auto["n"] else 100
    return {"rows": rows, "fails": sum(1 for q in checks if not q.passed), "scrap": sum(q.scrap for q in checks), "next": s.qc_next(),
            "auto": auto, "autoQC": s.settings.get("autoQC"), "every": s.settings.get("qcEvery")}


def downtime_page(s):
    from collections import Counter

    from .kviews import downtime_rows
    sh = s.shift
    mine = list(Downtime.objects.filter(shift=sh).order_by("start_time")) if sh else []
    older = list(Downtime.objects.filter(reason__isnull=True, line=s.station).exclude(shift=sh).order_by("start_time")) if sh else s.need_reason()
    by = Counter()
    for d in mine:
        by[d.reason or "Not entered"] += d.minutes or 0
    top = max(by.values()) if by else 1
    rows = downtime_rows(s, mine)
    for i, x in enumerate(rows):
        x["letter"] = chr(65 + i % 26)
    return {"rows": rows, "older": downtime_rows(s, older), "byReason": [{"k": k, "v": v, "pct": v / top * 100} for k, v in by.most_common()],
            "stopped": sh.stop_start if sh else None, "stoppedMins": round((s.t - sh.stop_start).total_seconds() / 60) if sh and sh.stop_start else 0,
            "errorsNow": s.current_errors() if sh and sh.stop_start else []}


def maintenance_page(s):
    from django.db.models import Q

    from .models import TicketPhoto
    tickets = list(Ticket.objects.filter(Q(t__gte=s.t - timedelta(days=3)) | ~Q(status="Closed")).order_by("-t"))
    pics = {}
    for p in TicketPhoto.objects.filter(ticket__in=tickets).values("id", "ticket_id"):
        pics.setdefault(p["ticket_id"], []).append(p["id"])
    return {"tickets": [{"t": t, "photos": pics.get(t.id, [])} for t in tickets], "limit": PHOTO_LIMIT}


def status_page(s):
    g = s.settings.get("grafana") or {}
    gon = bool(g.get("on") and g.get("url") and g.get("uid"))
    cards = []
    for m in s.lines:
        sh = ShiftRun.objects.filter(machine=m, active=True).select_related("mo").first()
        ls = LineState.objects.filter(machine=m).first()
        f = (ls.feed if ls else {}) or {}
        last = PpmReading.objects.filter(shift=sh).order_by("-t").values_list("ppm", flat=True).first() if sh else None
        nxt = MO.objects.filter(line=m, status="OPEN").exclude(pk=sh.mo_id if sh and sh.mo_id else None).order_by("seq").first()
        pill = (("bad", "Out of service") if not m.active else ("idle", "Idle")) if not sh else STATE_PILL.get(sh.state, ("idle", sh.state))
        cards.append({"m": m, "sh": sh, "f": f, "pill": pill, "ppm": last or 0, "stats": shift_stats(sh) if sh else None, "next": nxt,
                      "errs": s.current_errors(m.name) if (m.error_src or {}).get("type") else None,
                      "errLog": (ls.err_state or {}) if ls else {}, "uptimeSrc": (m.uptime_src or {}).get("type"),
                      "feedLabel": dict(FEED_LABELS).get(m.feed_type, "—")})
    panels = [(t, s.grafana_src(p)) for t, p in (("Pleater speed", g.get("ppm")), ("Uptime", g.get("uptime")), ("Count", g.get("count"))) if p] if gon else []
    from .snapshot import server_info
    return {"cards": cards, "grafana": g if gon else None, "panels": panels, "dash": f"{g.get('url', '').rstrip('/')}/d/{g.get('uid', '')}" if gon else "",
            "chart": speed_chart(s.history(), s.machine.ppm if s.machine else 0, label=f"Pleater speed for {s.station}"), "srv": server_info()}


FEED_LABELS = [("demo", "Demo replay"), ("http", "Read from URL"), ("push", "Pushed to the server"), ("influx", "InfluxDB query"),
               ("db", "Database table"), ("none", "Not connected")]


def shift_report(s):
    """The End of Shift report for this screen's current (or last) shift run."""
    from collections import Counter

    from .models import MoRun, QCCheck, RollTag, ShiftNote, TicketPhoto
    sh = s.shift or ShiftRun.objects.filter(machine=s.machine).order_by("-start").first() if s.machine else None
    if not sh:
        return None
    downs = list(Downtime.objects.filter(shift=sh).order_by("start_time"))
    qc = list(QCCheck.objects.filter(shift=sh))
    rolls = list(RollTag.objects.filter(shift=sh).order_by("t"))
    tickets = list(Ticket.objects.filter(shift=sh).order_by("t"))
    npics = {}
    for p in TicketPhoto.objects.filter(ticket__in=tickets).values("ticket_id"):
        npics[p["ticket_id"]] = npics.get(p["ticket_id"], 0) + 1
    notes = list(ShiftNote.objects.filter(shift=sh).order_by("t"))
    mos = [{"mo": r.mo, "item": r.item, "qty": r.qty, "scrap": r.scrap, "start": r.start_time, "end": r.end_time}
           for r in MoRun.objects.filter(shift=sh).order_by("start_time")]
    if sh.active and sh.mo_id:
        mos.append({"mo": sh.mo.number, "item": sh.mo.part, "qty": sh.mo_count, "scrap": 0, "start": sh.mo_start, "end": None})
    by = Counter()
    for d in downs:
        by[d.reason or "Not entered"] += d.minutes or 0
    top = max(by.values()) if by else 1
    st = shift_stats(sh)
    person = Person.objects.filter(pk=sh.person_id).first() if sh.person_id else None
    r = {"sh": sh, "who": person.name if person else sh.user_name, "st": st, "qc": len(qc), "qcFail": sum(1 for q in qc if not q.passed),
         "scrap": sum(q.scrap for q in qc) + sum(m["scrap"] or 0 for m in mos), "rolls": rolls, "mos": mos,
         "byReason": [{"k": k, "v": v, "pct": v / top * 100} for k, v in by.most_common()],
         "tickets": [{"t": t, "pics": npics.get(t.id, 0)} for t in tickets], "notes": notes, "needReason": len(s.need_reason())}
    from suite.templatetags.suite import clock, day
    L = [f"END OF SHIFT — {sh.line} — {day(sh.start)}", f"Operator: {r['who']}",
         f"Time: {clock(sh.start)} – {'running' if sh.active else clock(sh.end)}",
         f"Units: {sh.count}  Uptime: {st['uptime']}%  Downtime: {st['down']} min  Scrap: {r['scrap']}", "", "MOs:"]
    L += [f"  {m['mo']} {m['item']}: {m['qty']}" for m in mos]
    L += ["Downtime:"] + [f"  {x['k']}: {x['v']} min" for x in r["byReason"]]
    L += [f"QC: {r['qc']} checks, {r['qcFail']} fail", "Rolls:"] + [f"  {x.roll} {x.material} lot {x.lot}" for x in rolls]
    if sh.issues:
        L.append("Start-of-shift issue: " + sh.issues)
    if sh.end_issues:
        L.append("End-of-shift issue: " + sh.end_issues)
    L += [f"Ticket: {x['t'].machine} {x['t'].type} ({x['t'].status})" for x in r["tickets"]]
    L += [f"Note {clock(n.t)}: {n.text}" for n in notes]
    r["text"] = "\n".join(L)
    return r


def team_page(s, show_inactive=False):
    people = list(Person.objects.prefetch_related("trained").order_by("name"))
    lines = s.active_lines
    rows = []
    for u in people:
        if not show_inactive and not u.active:
            continue
        tr = {m.name for m in u.trained.all()}
        rows.append({"u": u, "trained": [(m.name, m.name in tr) for m in lines], "you": bool(s.me and u.id == s.me.id),
                     # a shift lead manages everyone but Admins; an Admin manages everyone
                     "edit": s.can("team.manage") and (u.role != "admin" or s.can("permissions.manage"))})
    act = [u for u in people if u.active]
    return {"rows": rows, "lines": lines, "inactive": sum(1 for u in people if not u.active), "showInactive": show_inactive,
            "needPin": sum(1 for u in act if not u.pin), "untrained": sum(1 for u in act if not u.trained.exists()),
            "paycom": s.settings.get("paycom") or {}, "paycomReady": integrations.paycom_ready()}
