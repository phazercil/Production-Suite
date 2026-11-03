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
         ("status", "Status"), ("end-of-shift", "End of Shift"), None, ("team", "Team"), ("admin", "Admin")]
LOCKED_OUT = {"team", "admin"}           # not on a machine's own screen (/station/<machine>/)
ADMIN_PAGES = {"admin"}
STATE_PILL = {"running": ("ok", "Running"), "changeover": ("warn", "Changeover"), "stopped": ("bad", "Stopped")}
PHOTO_LIMIT = 6


def hms(seconds):
    s = max(0, int(seconds))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


class Screen:
    """One kiosk screen at one moment: the person logged in, the machine it is for, and what follows from them."""

    def __init__(self, request, me=None, lock=""):
        self.request = request
        self.t = now()
        self.me = me
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

    # ------------------------------------------------------------ who
    @property
    def is_admin(self):
        return bool(self.me and self.me.role == "admin")

    @property
    def can_override(self):
        return bool(self.me and self.me.role in ("admin", "maintenance"))

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
            a.append({"sev": "bad", "text": f"The server cannot save records: {srv['storeErr']}", "go": "admin"})
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
                        "locked": key in ADMIN_PAGES and not self.is_admin})
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
                "storeOk": srv["storeOk"], "storeErr": srv["storeErr"], "demo": self.st.demo, "admin": self.is_admin,
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


def people_for_login(admin_only=False):
    return list(Person.objects.filter(active=True).filter(role="admin") if admin_only else Person.objects.filter(active=True))
