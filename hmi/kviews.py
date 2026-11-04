"""Operator Kiosk, built on the server (thin client).

  GET  /kiosk/<page>/          the whole screen
  GET  /kiosk/v/<page>         the page's main part, re-loaded when the plant changes
  GET  /kiosk/r/<part>         rail, menu, banner and the reason prompt
  GET  /kiosk/m/<dialog>       a dialog (login, start shift, changeover, QC, ...)
  POST /kiosk/do/<action>      a button or form: runs the plant rules (hmi/engine.py) and answers with the next
                               dialog, a message, or "refresh"
The browser only shows what comes back (static/suite/thin.js + htmx). See hmi/kscreen.py for what each page shows.
"""
import json
import re

from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.utils.html import escape
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from plant.models import MO, Machine, Person
from plant.util import ms, now
from suite import thin

from . import engine, kscreen
from .views import current_person, pin_login, touch

PAGES = {p[0] for p in kscreen.PAGES if p}
VIEWS = PAGES | {"admin"}   # Admin is its own section (/admin/) but its view is built the same way
BUILT = {"controls", "schedule", "qc-log", "downtime", "maintenance", "status", "end-of-shift", "team", "admin"}


# ------------------------------------------------------------------ helpers
def screen(request):
    me, note = current_person(request)
    s = kscreen.Screen(request, me=me, lock=request.session.get("lock", ""))
    s.note = note
    return s


def ctx_for(s, **kw):
    from urllib.parse import quote
    base = f"/station/{quote(s.lock)}" if s.lock else "/operator"
    return {"S": s, "me": s.me, "station": s.station, "lock": s.lock, "now_ms": ms(s.t), "base": base, **kw}


def modal_response(request, template, **ctx):
    """A dialog: goes into #modal, which the screen then shows."""
    resp = render(request, template, ctx)
    resp["HX-Retarget"] = "#modal"
    resp["HX-Reswap"] = "innerHTML"
    return resp


def err_in_modal(msg):
    """A dialog's own error line: the dialog stays open with what was typed."""
    resp = HttpResponse(escape(msg))
    resp["HX-Retarget"] = "#modal .err"
    resp["HX-Reswap"] = "innerHTML"
    return resp


def go(page, toast=None, s=None):
    """Move the screen to another page."""
    base = ctx_for(s)["base"] if s else "/operator"
    resp = HttpResponse("")
    resp["HX-Redirect"] = f"{base}/{page}/"
    return thin.trigger(resp, toast=toast, close=True)


def form_params(request):
    """The fields a form sent, typed by their names: x:b is a yes/no, x:n a number, x:l a list of every value,
    x:j JSON. Plain names are text. The engine checks every value again."""
    p = {}
    for key in request.POST:
        if key == "csrfmiddlewaretoken":
            continue
        vals = request.POST.getlist(key)
        name, _, kind = key.partition(":")
        v = vals[-1] if vals else ""
        if kind == "b":
            p[name] = v in ("1", "true", "on", "yes")
        elif kind == "n":
            try:
                p[name] = float(v) if v.strip() else None
            except ValueError:
                p[name] = None
        elif kind == "l":
            p[name] = vals
        elif kind == "j":
            try:
                p[name] = json.loads(v)
            except ValueError:
                p[name] = None
        else:
            p[name] = v
    return p


def run(request, s, name, p):
    """Run one plant command for this screen (the same rules every screen uses)."""
    return engine.execute(name, p, engine.Ctx(user=s.me, line=s.station, client_ip=request.META.get("REMOTE_ADDR", "")))


def login_needed(request, s, r, next_modal="", next_q=""):
    """The command needs someone logged in (or a shift lead, or an Admin): ask for the PIN, then come back."""
    level = "admin" if r.get("onlyAdmin") else "lead" if r.get("admin") else ""
    return modal_response(request, "hmi/k/m_login.html", **ctx_for(s, level=level, people=kscreen.people_for_login(level),
                                                                   next=next_modal, next_q=next_q))


# ------------------------------------------------------------------ pages
@ensure_csrf_cookie
@require_GET
def page(request, page="controls", lock=None):
    if lock is not None:
        m = Machine.objects.filter(name__iexact=lock, stage="s1").first()
        if not m:
            names = ", ".join(Machine.objects.filter(stage="s1").values_list("name", flat=True))
            return render(request, "hmi/no_station.html", {"name": lock, "names": names, "kiosk_lock": True}, status=404)
        request.session["lock"] = m.name
    else:
        request.session.pop("lock", None)
        if request.GET.get("line"):
            request.session["station"] = request.GET["line"]
    if page == "admin" and lock is None:   # Admin is its own section of the suite now
        q = request.GET.urlencode()
        return redirect("/admin/" + (f"?{q}" if q else ""))
    page = page if page in PAGES else "controls"
    for k in ("tab", "view", "status", "sort", "inactive"):   # a link straight to a page's option (/operator/schedule/?view=calendar)
        if request.GET.get(k) is not None:
            request.session[f"k.{k}"] = request.GET[k][:40]
    s = screen(request)
    if s.lock and page in kscreen.LOCKED_OUT:
        return redirect(ctx_for(s)["base"] + "/controls/")
    return render(request, "hmi/k/page.html", ctx_for(s, page=page, thin=True, mode="operator", kiosk_lock=bool(s.lock), nav=s.nav(page),
                                                      B=s.banner(), alarms=s.alarms()))


@ensure_csrf_cookie
@require_GET
def admin_page(request):
    """Admin, its own section in the top bar: the suite's settings, for shift leads (day-to-day lists) and Admins."""
    if request.GET.get("tab") is not None:
        request.session["k.tab"] = request.GET["tab"][:40]
    request.session.pop("lock", None)   # never a machine's own screen
    s = screen(request)
    return render(request, "hmi/k/admin_page.html", ctx_for(s, page="admin", thin=True, mode="admin"))


@require_GET
def view(request, page):
    if page not in VIEWS:
        raise Http404("No such page")
    s = screen(request)
    if page not in BUILT:
        return render(request, "hmi/k/v_soon.html", ctx_for(s, page=page))
    return render(request, f"hmi/k/v_{page.replace('-', '_')}.html", ctx_for(s, page=page, **view_data(s, page, request)))


def page_option(request, key, allowed, default):
    """A page's display choice (timeline/calendar, a filter, a sort): from the request, remembered for this screen."""
    v = request.GET.get(key)
    if v is not None and (allowed is None or v in allowed):
        request.session[f"k.{key}"] = v
    v = request.session.get(f"k.{key}", default)
    return v if allowed is None or v in allowed else default


def view_data(s, page, request):
    if page == "controls":
        return {"R": s.run_screen()}
    if page == "schedule":
        sort = page_option(request, "sort", None, "")
        return {"P": kscreen.schedule_page(s, page_option(request, "view", {"timeline", "calendar"}, "timeline"),
                                           page_option(request, "status", {"OPEN", "CLOSE", "ALL"}, "OPEN"), sort)}
    if page == "qc-log":
        return {"P": kscreen.qc_log_page(s)}
    if page == "downtime":
        return {"P": kscreen.downtime_page(s)}
    if page == "maintenance":
        return {"P": kscreen.maintenance_page(s)}
    if page == "status":
        return {"P": kscreen.status_page(s)}
    if page == "end-of-shift":
        return {"P": kscreen.shift_report(s)}
    if page == "team":
        return {"P": kscreen.team_page(s, page_option(request, "inactive", {"0", "1"}, "0") == "1")}
    if page == "admin":
        from . import kadmin
        tab = page_option(request, "tab", {k for k, _ in kadmin.TABS}, "machines")
        from .models import AuditLog
        return {"tab": tab, "tabs": kadmin.TABS, "leadTab": tab in kadmin.LEAD_TABS, "A": kadmin.tab_data(s, tab, request), "audit": list(AuditLog.objects.all()[:12])}
    return {}


@require_GET
def part(request, name):
    s = screen(request)
    page = request.GET.get("page", "controls")
    if name == "rail":
        return render(request, "hmi/k/_rail.html", ctx_for(s, alarms=s.alarms()))
    if name == "nav":
        return render(request, "hmi/k/_nav.html", ctx_for(s, nav=s.nav(page), page=page))
    if name == "banner":
        return render(request, "hmi/k/_banner.html", ctx_for(s, B=s.banner(), page=page))
    if name == "prompt":
        return render(request, "hmi/k/_prompt.html", ctx_for(s, open=s.reason_prompt()))
    raise Http404("No such part")


# ------------------------------------------------------------------ dialogs
MODALS = {}


def modal(name, login=False):
    def deco(fn):
        MODALS[name] = (fn, login)
        return fn
    return deco


@require_GET
def dialog(request, name):
    if name not in MODALS:
        raise Http404("No such dialog")
    fn, need_login = MODALS[name]
    s = screen(request)
    if need_login and not s.me:
        return modal_response(request, "hmi/k/m_login.html", **ctx_for(s, people=kscreen.people_for_login(), next=name, next_q=request.GET.urlencode()))
    return fn(request, s)


@modal("login")
def m_login(request, s):
    level = {"1": "lead", "2": "admin"}.get(request.GET.get("admin", ""), "")
    return modal_response(request, "hmi/k/m_login.html", **ctx_for(s, level=level, people=kscreen.people_for_login(level),
                                                                   next=request.GET.get("next", ""), next_q=request.GET.get("next_q", "")))


@modal("alarms")
def m_alarms(request, s):
    return modal_response(request, "hmi/k/m_alarms.html", **ctx_for(s, alarms=s.alarms()))


@modal("start", login=True)
def m_start(request, s):
    if s.shift_on:
        return modal_response(request, "hmi/k/m_note.html", **ctx_for(s, title="Shift already running",
                              text=f"{s.shift.line} has been running since {engine.fmt_t(s.shift.start)}. End that shift first."))
    trained = set(s.me.trained.values_list("name", flat=True))
    want = request.GET.get("line") or s.lock or ""
    if not want:
        want = s.station if s.station in trained and s.machine and s.machine.active else next((m.name for m in s.active_lines if m.name in trained), "")
    lines = [{"name": m.name, "ok": m.name in trained and m.active,
              "why": " — out of service" if not m.active else ("" if m.name in trained else " — not trained")}
             for m in s.lines if not s.lock or m.name == s.lock]
    q = list(MO.objects.filter(line__name=want, status="OPEN").order_by("seq")[:3]) if want else []
    pick = request.GET.get("mo") or (str(q[0].number) if q else "")
    return modal_response(request, "hmi/k/m_start.html", **ctx_for(s, lines=lines, line=want, queue=q, trained=bool(trained), mo_pick=pick,
                                                                   safety=s.cfg.get("safety", []), pm=s.cfg.get("pm", [])))


@modal("changeover", login=True)
def m_changeover(request, s):
    if request.GET.get("refresh"):   # the open dialog keeping up with the line: never starts a changeover by itself
        return changeover_modal(request, s)
    if not s.shift_on:
        return thin.refused("Start a shift first.")
    if not s.shift.co:
        r = run(request, s, "coOpen", {})
        if not r["ok"]:
            return thin.refused(r["error"])
        s = screen(request)
    return changeover_modal(request, s)


def changeover_modal(request, s):
    sh = s.shift
    if not sh or not sh.co:
        return thin.done(close=True)
    co = sh.co
    c = {"co": co, "sh": sh, "step": co.get("step", 1)}
    if c["step"] == 1:
        mo = s.mo(co.get("oldMo"))
        c.update(mo=mo, moTimer=kscreen.hms((s.t - sh.mo_start).total_seconds()) if sh.mo_start else "0:00:00",
                 close=bool(mo and mo.produced >= mo.qty))
    elif c["step"] == 2:
        c["queue"] = list(MO.objects.filter(line__name=sh.line, status="OPEN").exclude(pk=co.get("oldMo")).order_by("seq")[:4])
        c["mo_pick"] = request.GET.get("mo") or (str(c["queue"][0].number) if c["queue"] else "")
    else:
        nmo = s.mo(co.get("newMo"))
        p = s.part(nmo.part if nmo else "")
        steps = co.get("steps") or [{"text": t} for t in s.cfg.get("coSteps", [])]
        def step(i, st):
            st = dict(st)
            setting, value, unit = str(st.get("setting") or ""), str(st.get("value") if st.get("value") is not None else ""), str(st.get("unit") or "")
            st.update(i=i, done=i in (co.get("done") or []), tag=f"{setting} {value}{unit}".strip() if (setting or value) else "",
                      imageOk=str(st.get("image") or "").lower().startswith(("http://", "https://")))
            return st
        c.update(nmo=nmo, p=p, steps=[step(i, st) for i, st in enumerate(steps)],
                 settingsRows=co.get("settings") or [], src=co.get("src"), cacheT=co.get("cacheT"),
                 canReload=bool(s.settings.get("useDb")) and engine.integrations.assets_ready(),
                 override=" · ".join(f"{k} {v}" for k, v in (sh.override or {}).items()))
    return modal_response(request, "hmi/k/m_changeover.html", **ctx_for(s, **c))


@modal("qc", login=True)
def m_qc(request, s):
    if not s.shift_on:
        return thin.refused("Start a shift before logging QC.")
    p = s.part(s.current_item())
    fields = [("len", "Length", p["len"]), ("wid", "Width", p["wid"]), ("ph", "Pleat height", p["ph"]), ("sbl", "Side band height L", p["sb"]),
              ("sbr", "Side band height R", p["sb"])]
    return modal_response(request, "hmi/k/m_qc.html", **ctx_for(s, p=p, fields=fields))


@modal("rolls", login=True)
def m_rolls(request, s):
    if not s.shift_on:
        return thin.refused("Start a shift before scanning rolls.")
    from .models import RollTag
    rolls = list(RollTag.objects.filter(shift=s.shift).order_by("-t"))
    rows = []
    for i, r in enumerate(rolls):
        nxt = s.shift.count if i == 0 else rolls[i - 1].count_at
        rows.append({"r": r, "units": nxt - r.count_at})
    return modal_response(request, "hmi/k/m_rolls.html", **ctx_for(s, rows=rows, media=s.part(s.current_item())["media"] if s.current_item() else ""))


@modal("ticket", login=True)
def m_ticket(request, s):
    machines = [m.name for m in s.lines] + list(s.cfg.get("equipment", []))
    return modal_response(request, "hmi/k/m_ticket.html", **ctx_for(s, machines=machines, issues=s.cfg.get("issueTypes", []),
                                                                    sel=s.shift.line if s.shift else s.station, limit=kscreen.PHOTO_LIMIT))


@modal("photo")
def m_photo(request, s):
    from .models import TicketPhoto
    ph = TicketPhoto.objects.filter(pk=request.GET.get("id") or 0).select_related("ticket").defer("data", "thumb").first()
    if not ph:
        return thin.refused("That picture is no longer on file.")
    return modal_response(request, "hmi/k/m_photo.html", **ctx_for(s, ph=ph))


@modal("drawing")
def m_drawing(request, s):
    item = s.current_item() or "MG4062HP"
    p = s.part(item)
    note = (f"Rev {p['rev']} confirmed current at {engine.fmt_t(s.shift.draw_checked)}." if s.shift and s.shift.draw_checked
            else "Showing the drawing on file at shift start.")
    return modal_response(request, "hmi/k/m_drawing.html", **ctx_for(s, p=p, note=request.GET.get("msg") or note,
                                                                     pleat_xs=[70 + i * 14 for i in range(16)]))


@modal("override")
def m_override(request, s):
    if not s.is_lead:
        return modal_response(request, "hmi/k/m_login.html", **ctx_for(s, level="lead", people=kscreen.people_for_login("lead"), next="override"))
    if not s.shift_on:
        return thin.refused("Start a shift first.")
    p = s.part(s.current_item())
    return modal_response(request, "hmi/k/m_override.html", **ctx_for(s, o=s.shift.override or {}, p=p, rated=int(s.machine.ppm)))


@modal("notes", login=True)
def m_notes(request, s):
    from django.db.models import Q

    from .models import ShiftNote
    qs = ShiftNote.objects.filter(Q(shift=s.shift) if s.shift else Q(pk__in=[])) | ShiftNote.objects.filter(shift__isnull=True, line=s.station)
    return modal_response(request, "hmi/k/m_notes.html", **ctx_for(s, notes=list(qs.order_by("-t")[:50])))


@modal("end")
def m_end(request, s):
    if not s.shift_on:
        return go("end-of-shift", s=s)
    if not s.me:
        return modal_response(request, "hmi/k/m_login.html", **ctx_for(s, people=kscreen.people_for_login(), next="end"))
    from .engine import shift_stats
    return modal_response(request, "hmi/k/m_end.html", **ctx_for(s, stats=shift_stats(s.shift), mo=s.cur_mo, need=len(s.need_reason())))


@modal("errlog")
def m_errlog(request, s):
    from .models import Downtime
    d = Downtime.objects.filter(pk=request.GET.get("id") or 0).first()
    if not d:
        return thin.refused("That stop is no longer on file.")
    rows = downtime_rows(s, [d])
    return modal_response(request, "hmi/k/m_errlog.html", **ctx_for(s, d=d, errors=rows[0]["errors"] if rows else []))


@modal("report")
def m_report(request, s):
    r = kscreen.shift_report(s)
    return modal_response(request, "hmi/k/m_report.html", **ctx_for(s, text=r["text"] if r else ""))


@modal("paycomFields")
def m_paycom_fields(request, s):
    """What Paycom sends: its field names and the first employees as the import reads them."""
    from . import kadmin
    r = kadmin.run(request, s, "paycomFields", {})
    if not r["ok"]:
        return login_needed(request, s, r) if r.get("admin") or r.get("login") else thin.refused(r["error"])
    d = r["data"]
    return modal_response(request, "hmi/k/m_paycom_fields.html", **ctx_for(s, fields=d["fields"], rows=d["sample"],
                                                                          columns=["code", "name", "status", "active", "department", "position", "badge"]))


@modal("addPerson")
def m_add_person(request, s):
    if not s.is_lead:
        return modal_response(request, "hmi/k/m_login.html", **ctx_for(s, level="lead", people=kscreen.people_for_login("lead"), next="addPerson"))
    return modal_response(request, "hmi/k/m_add_person.html", **ctx_for(s))


@modal("confirm")
def m_confirm(request, s):
    """Are you sure? for something that can't easily be undone. The button runs the action with its values."""
    from . import kadmin
    a, key = request.GET.get("a", ""), request.GET.get("k", "")
    spec = kadmin.CONFIRM.get(key or a)
    if not spec:
        return thin.refused("Unknown action.")
    vals = {k: v for k, v in request.GET.items() if k not in ("a", "k", "t")}
    tgt = request.GET.get("t", "")   # a panel the answer replaces (#sources), when the action sends one back
    tgt = tgt if re.fullmatch(r"#[a-z][a-z0-9-]*", tgt) else ""
    fmt = dict(vals)
    if a == "run" and vals.get("c") == "lineDel":
        n = MO.objects.filter(line__name=vals.get("id", ""), status="OPEN").count()
        fmt["queued"] = f"{n} queued MO{'s' if n != 1 else ''} will go back to Unscheduled. " if n else ""
    title, body, note, yes = (x.format_map(_Blank(fmt)) for x in spec)
    return modal_response(request, "hmi/k/m_confirm.html", **ctx_for(s, title=title, body=body, note=note, yes=yes, action=a, tgt=tgt,
                                                                     vals=json.dumps(vals)))


class _Blank(dict):
    def __missing__(self, k):
        return ""


@modal("downPrompt")
def m_down_prompt(request, s):
    rows = downtime_rows(s, s.need_reason())
    return modal_response(request, "hmi/k/m_down_prompt.html", **ctx_for(s, rows=rows, total=sum(d.minutes or 0 for d in s.need_reason()),
                                                                         reasons=s.cfg.get("reasons", [])))


def downtime_rows(s, downs):
    from . import machineerrors
    errs = machineerrors.for_downtime(downs)
    lead = s.can_override
    out = []
    for d in downs:
        custom = bool(d.reason and d.reason not in s.cfg.get("reasons", []))
        out.append({"d": d, "errors": errs.get(d.id, []), "custom": custom,
                    "locked": bool(d.reason and not lead and s.me and d.by and d.by != s.me.name)})
    return out


# ------------------------------------------------------------------ actions
@require_POST
def act(request, name):
    s = screen(request)
    touch(request)
    from . import kadmin
    fn = ACTIONS.get(name) or kadmin.ACTIONS.get(name)
    if not fn:
        raise Http404("No such action")
    return fn(request, s, form_params(request))


ACTIONS = {}


def action(name):
    def deco(fn):
        ACTIONS[name] = fn
        return fn
    return deco


def simple(cmd, toast=True, close=False, then=None):
    """A form that runs one plant command; errors go in the dialog's error line."""
    def handler(request, s, p):
        r = run(request, s, cmd, p)
        if not r["ok"]:
            if r.get("login") or r.get("admin"):
                return login_needed(request, s, r)
            return err_in_modal(r["error"]) if request.headers.get("X-In-Modal") else thin.refused(r["error"])
        if then:
            return then(request, screen(request), r)
        return thin.done(toast=r.get("msg") if toast else None, close=close)
    return handler


@action("login")
def a_login(request, s, p):
    level = p.get("level") if p.get("level") in ("lead", "admin") else ""
    u, err = pin_login(request, p.get("pin"), p.get("user") or None, level)
    if not u:
        return err_in_modal(err)
    nxt = p.get("next") or ""
    if nxt in MODALS:
        request.GET = request.GET.copy()
        if p.get("next_q"):
            from django.http import QueryDict
            request.GET = QueryDict(p["next_q"], mutable=True)
        resp = MODALS[nxt][0](request, screen(request))
        return thin.trigger(resp, toast=f"Logged in as {u.name}", changed=True)
    return thin.done(toast=f"Logged in as {u.name}", close=True)


@action("logout")
def a_logout(request, s, p):
    request.session.pop("pid", None)
    return thin.done(toast="Logged out")


@action("station")
def a_station(request, s, p):
    if not s.lock and Machine.objects.filter(name=p.get("line", ""), stage="s1").exists():
        request.session["station"] = p["line"]
    return thin.done()


@action("startShift")
def a_start(request, s, p):
    n_safety, n_pm = len(s.cfg.get("safety", [])), len(s.cfg.get("pm", []))
    on = set(request.POST.getlist("check"))
    p = {"line": p.get("line", ""), "mo": (p.get("mo") or "").strip(), "issues": p.get("issues", ""),
         "safety": [f"s{i}" in on for i in range(n_safety)], "pm": [f"p{i}" in on for i in range(n_pm)]}
    r = run(request, s, "startShift", p)
    if not r["ok"]:
        return err_in_modal(r["error"])
    if not s.lock:
        request.session["station"] = p["line"]
    return go("controls", toast=r.get("msg"), s=s)


def _co_next(request, s, r):
    return changeover_modal(request, screen(request))


ACTIONS["coCancel"] = simple("coCancel", toast=False, close=True)
ACTIONS["coEnd"] = simple("coEnd", toast=False, then=_co_next)
ACTIONS["coPick"] = simple("coPick", toast=False, then=_co_next)
ACTIONS["coReload"] = simple("coReload", toast=False, then=_co_next)
ACTIONS["coFinish"] = simple("coFinish", close=True)
@action("coStep")
def a_co_step(request, s, p):
    try:
        i = int(p.get("i"))
    except (TypeError, ValueError):
        return thin.refused("Unknown step.")
    r = run(request, s, "coStep", {"i": i, "checked": bool(p.get(f"step{i}"))})
    return thin.done(changed=True) if r["ok"] else thin.refused(r["error"])
ACTIONS["rollAdd"] = simple("rollAdd", toast=False, then=lambda request, s, r: m_rolls(request, s))
ACTIONS["noteAdd"] = simple("noteAdd", toast=False, then=lambda request, s, r: m_notes(request, s))
ACTIONS["overrideSet"] = simple("overrideSet", close=True)
ACTIONS["overrideClear"] = simple("overrideClear", toast=False, close=True)
ACTIONS["ticketStatus"] = simple("ticketStatus", toast=False)
ACTIONS["ackNotices"] = simple("ackNotices", toast=False)
ACTIONS["settingSet"] = simple("settingSet", toast=False)
ACTIONS["userSet"] = simple("userSet", toast=False)
ACTIONS["paycomImport"] = simple("paycomImport")


@action("userAdd")
def a_user_add(request, s, p):
    r = run(request, s, "userAdd", {k: p.get(k, "") for k in ("name", "pin", "role")})
    if not r["ok"]:
        return login_needed(request, s, r) if r.get("admin") or r.get("login") else err_in_modal(r["error"])
    return thin.done(toast=r.get("msg") or "Added", close=True)


@action("train")
def a_train(request, s, p):
    r = run(request, s, "userSet", {"id": p.get("id"), "f": "train", "v": {"line": p.get("line"), "on": bool(p.get("on"))}})
    return thin.done() if r["ok"] else (login_needed(request, s, r) if r.get("admin") else thin.refused(r["error"]))


@action("qcSave")
def a_qc(request, s, p):
    p = {k: p.get(k, "") for k in ("len", "wid", "ph", "sbl", "sbr", "glue", "cut")} | {"scrap": int(p.get("scrap") or 0)}
    r = run(request, s, "qcSave", p)
    return err_in_modal(r["error"]) if not r["ok"] else thin.done(toast=r.get("msg"), close=True)


@action("qcField")
def a_qc_field(request, s, p):
    """As a measurement is typed: in or out of tolerance (the part's spec), shown on the field."""
    k = p.get("k", "")
    pp = s.part(s.current_item())
    target = {"len": pp["len"], "wid": pp["wid"], "ph": pp["ph"], "sbl": pp["sb"], "sbr": pp["sb"]}.get(k)
    try:
        v = float(p.get(k) or "")
    except ValueError:
        v = None
    cls = "" if v is None or target is None else ("in" if abs(v - target) <= pp["tol"] + 1e-9 else "out")
    resp = HttpResponse("")
    resp["HX-Reswap"] = "none"
    return thin.trigger(resp, **{"suite:mark": {"id": f"q-{k}", "cls": cls}})


@action("ticketAdd")
def a_ticket(request, s, p):
    from . import photos
    files = request.FILES.getlist("photo")[:kscreen.PHOTO_LIMIT]
    r = run(request, s, "ticketAdd", {k: p.get(k, "") for k in ("machine", "type", "priority", "notes")})
    if not r["ok"]:
        return err_in_modal(r["error"])
    from .models import Ticket
    t = Ticket.objects.get(pk=r["data"]["id"])
    bad = 0
    for f in files:
        try:
            if f.size > photos.MAX_BYTES:
                raise photos.PhotoError("too large")
            photos.add(t, f.read(), s.me.name, now())
        except photos.PhotoError:
            bad += 1
    engine.PlantSettings.bump()
    n = len(files) - bad
    msg = (f"Ticket sent, but {bad} picture{'s' if bad > 1 else ''} couldn't be read. Add {'them' if bad > 1 else 'it'} from the Maintenance page."
           if bad else f"Maintenance ticket sent with {n} picture{'s' if n > 1 else ''}" if n else r.get("msg") or "Maintenance ticket sent")
    return thin.done(toast=msg, kind="bad" if bad else None, close=True)


@action("ticketPhoto")
def a_ticket_photo(request, s, p):
    from . import photos
    from .models import Ticket
    if not s.me:
        return login_needed(request, s, {"login": True})
    t = Ticket.objects.filter(pk=p.get("id") or 0).first()
    if not t:
        return thin.refused("That ticket is no longer on file.")
    n, err = 0, ""
    for f in request.FILES.getlist("photo"):
        try:
            if f.size > photos.MAX_BYTES:
                raise photos.PhotoError("That picture is too large (over 25 MB).")
            photos.add(t, f.read(), s.me.name, now())
            n += 1
        except photos.PhotoError as e:
            err = str(e)
    engine.PlantSettings.bump()
    return thin.done(toast=f"{n} picture{'s' if n != 1 else ''} added" + (f" · {err}" if err and n else "") if n else err or "No picture added",
                     kind="bad" if err and not n else None)


@action("getDrawing")
def a_drawing(request, s, p):
    r = run(request, s, "getDrawing", {"part": p.get("part", "")})
    if not r["ok"]:
        return thin.refused(r["error"])
    request.GET = request.GET.copy()
    request.GET["msg"] = r.get("msg", "")
    return m_drawing(request, screen(request))


@action("endShift")
def a_end(request, s, p):
    r = run(request, s, "endShift", {"issues": p.get("issues", "")})
    if not r["ok"]:
        return err_in_modal(r["error"])
    return go("end-of-shift", toast=r.get("msg"), s=s)


@action("snooze")
def a_snooze(request, s, p):
    """The "Why did the line stop?" dialog was put off: ask again in 5 minutes."""
    request.session["down_snooze"] = ms(now()) + 5 * 60000
    return thin.done(close=True, changed=False)


@action("downtimeSet")
def a_downtime(request, s, p):
    q = {"id": p.get("id")}
    if "reason" in p:
        if p["reason"] == "__other":   # maintenance / shift lead: type a reason of their own
            from .models import Downtime
            d = Downtime.objects.filter(pk=p.get("id") or 0).first()
            return modal_response(request, "hmi/k/m_reason_other.html", **ctx_for(s, d=d, back=p.get("back", "")))
        q["reason"] = (p.get("reason") or "").strip()[:120] or None
    if "notes" in p:
        q["notes"] = p["notes"]
    r = run(request, s, "downtimeSet", q)
    if not r["ok"]:
        if r.get("login"):
            return login_needed(request, s, r, next_modal="downPrompt")
        return thin.refused(r["error"])
    if p.get("back") == "downPrompt":
        s2 = screen(request)
        if not s2.need_reason():
            return thin.done(toast="Thanks — every stop has a reason.", close=True)
        return thin.trigger(m_down_prompt(request, s2), changed=True)
    return thin.done(changed=True)
