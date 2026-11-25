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
from datetime import timedelta
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
from .models import ShiftRun
from .views import current_person, pin_login, touch

PAGES = {p[0] for p in kscreen.PAGES if p}
VIEWS = PAGES | {"admin"}   # Admin is its own section (/admin/) but its view is built the same way
BUILT = {"controls", "schedule", "part-info", "instructions", "model", "cameras", "qc-log", "downtime", "maintenance", "status", "end-of-shift", "team", "admin"}


# ------------------------------------------------------------------ helpers
SUP_MINUTES = 10   # a supervisor override ends after this long without a tap


def screen(request):
    me, note = current_person(request)
    s = kscreen.Screen(request, me=me, lock=request.session.get("lock", ""), sup=supervisor(request))
    s.note = note
    return s


def supervisor(request):
    """The supervisor who has taken over this screen (Supervisor button), while it lasts."""
    from plant import perms
    sup = request.session.get("sup") or {}
    if not sup or sup.get("until", 0) < now().timestamp():
        request.session.pop("sup", None)
        return None
    p = Person.objects.filter(pk=sup.get("id"), active=True).first()
    if not perms.can(p, "floor.override"):
        request.session.pop("sup", None)
        return None
    return p


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
    return engine.execute(name, p, engine.Ctx(user=s.actor, line=s.station, client_ip=request.META.get("REMOTE_ADDR", "")))


def _perm_label(perm):
    from plant import perms
    return perms.PERMS[perm]["label"] if perm in perms.PERMS else ""


def login_needed(request, s, r, next_modal="", next_q=""):
    """The command needs someone logged in (or a shift lead, or an Admin): ask for the PIN, then come back."""
    perm = r.get("perm") or ("section.admin" if r.get("admin") else "")
    return modal_response(request, "hmi/k/m_login.html", **ctx_for(s, perm=perm, perm_label=_perm_label(perm), people=kscreen.people_for_login(perm),
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
    for k in ("tab", "view", "status", "sort", "inactive", "part", "hl"):   # a link straight to a page's option (/operator/schedule/?view=calendar)
        if request.GET.get(k) is not None:
            request.session[f"k.{k}"] = request.GET[k][:40]
    s = screen(request)
    if s.lock and page in kscreen.LOCKED_OUT:
        return redirect(ctx_for(s)["base"] + "/controls/")
    return render(request, "hmi/k/page.html", ctx_for(s, page=page, thin=True, mode="operator", kiosk_lock=bool(s.lock), nav=s.nav(page),
                                                      B=s.banner(), alarms=s.alarms(), N=s.notibar()))


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
        hl = request.session.pop("k.hl", "")   # an MO a notice pointed to: shown once, marked in the list
        return {"hl": hl, "P": kscreen.schedule_page(s, page_option(request, "view", {"timeline", "week"}, "timeline"),
                                                     page_option(request, "status", {"OPEN", "CLOSE", "ALL"}, "ALL" if hl else "OPEN"), sort,
                                                     int(page_option(request, "week", {"0", "1"}, "0")))}
    if page == "model":   # the 3D viewer, view only, following the filter this line runs
        sh = s.shift
        mo = sh.mo if sh else None
        return {"line": s.station, "mo": mo}
    if page == "cameras":   # this line's cameras (Admin › Cameras)
        from cameras.grab import every
        from cameras.models import Camera
        cams = list(Camera.objects.filter(enabled=True, machines__name=s.station).distinct()) if s.station else []
        return {"line": s.station, "cams": [{"pk": c.pk, "name": c.name, "ms": round(every(c) * 1000), "live": c.kind == "rtsp" and c.live}
                                            for c in cams]}
    if page == "instructions":   # work instructions for this machine and part (Quality › Work instructions)
        from instructions.views import kiosk_data
        return kiosk_data(s)
    if page == "part-info":
        P = kscreen.part_info(s, page_option(request, "part", None, ""))   # another part picked here stays until "Running now"
        return {"P": P, "p": P.get("p"), "tolTxt": P.get("tolTxt"), "pleat_xs": P.get("pleat_xs")}
    if page == "qc-log":
        return {"P": kscreen.qc_log_page(s)}
    if page == "downtime":
        return {"P": kscreen.downtime_page(s)}
    if page == "maintenance":
        return {"P": kscreen.maintenance_page(s, page_option(request, "show", {"open", "done", "all"}, "open"))}
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
        from plant import perms
        tperm = perms.TAB_PERM.get(tab, "permissions.manage")
        return {"tab": tab, "tabs": kadmin.TABS, "tabPerm": tperm, "tabPermLabel": perms.PERMS[tperm]["label"], "tabCan": s.can(tperm),
                "canOpen": s.can("section.admin"), "roleLabel": dict(perms.ROLES).get(s.me.role, "") if s.me else "", "A": kadmin.tab_data(s, tab, request), "audit": list(AuditLog.objects.all()[:12])}
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
    if name == "coPick":
        return render(request, "hmi/k/_co_pick.html", ctx_for(s, **co_pick(request.GET.get("line"), request.GET.get("mo"))))
    if name == "notibar":
        return render(request, "hmi/k/_notibar.html", ctx_for(s, N=s.notibar(), page=page))
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
    if request.GET.get("sup"):   # the Supervisor button
        return modal_response(request, "hmi/k/m_login.html", **ctx_for(s, perm="floor.override", perm_label=_perm_label("floor.override"),
                                                                       sup=True, people=kscreen.people_for_login("floor.override")))
    from plant import perms
    perm = request.GET.get("perm") if request.GET.get("perm") in perms.PERMS else ("section.admin" if request.GET.get("admin") else "")
    return modal_response(request, "hmi/k/m_login.html", **ctx_for(s, perm=perm, perm_label=_perm_label(perm), people=kscreen.people_for_login(perm),
                                                                   next=request.GET.get("next", ""), next_q=request.GET.get("next_q", "")))


@modal("alarms")
def m_alarms(request, s):
    return modal_response(request, "hmi/k/m_alarms.html", **ctx_for(s, alarms=s.alarms()))


@modal("start", login=True)
def m_start(request, s):
    if s.shift_on and not s.unattended:
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
                                                                   safety=s.cfg.get("safety", []), pm=s.cfg.get("pm", []),
                                                                   handoffs=engine.handoffs(want, s.t) if want else [],
                                                                   rollPics=int(engine.rules().get("rollPhotos", 2) or 0), pics=PIC_FIELDS, left=last_left(want, s.t)))


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


def co_history(co):
    """Who has worked on this changeover so far (pieces passed over or handed on), for the kiosk."""
    from plant.util import from_ms
    out = []
    for x in (co or {}).get("pieces") or []:
        out.append({**x, "who": x.get("by") or "Nobody logged in", "from": engine.fmt_t(from_ms(x["start"])), "to": engine.fmt_t(from_ms(x["end"])),
                    "stepsTxt": ", ".join(str(n) for n in x.get("steps") or []) or "none"})
    h = (co or {}).get("handoff")
    hand = {**h, "at": engine.fmt_t(from_ms(h["t"]))} if h and h.get("t") else None
    return out, hand


def changeover_modal(request, s):
    sh = s.shift
    if not sh or not sh.co:
        return thin.done(close=True)
    co = sh.co
    from plant.util import from_ms
    hist, hand = co_history(co)
    c = {"co": co, "sh": sh, "step": co.get("step", 1), "history": hist, "handoff": hand}
    if co.get("start") and co.get("step", 1) >= 2 and sh.person_id:
        w = engine.log_window(sh, s.t)
        left = (w["e"] - s.t).total_seconds() / 60
        c.update(canPass=True, shiftEnd=engine.fmt_t(w["e"]), nearEnd=not w.get("noShift") and left <= 30, shiftName=sh.shift_name,
                 coTotal=kscreen.hms((s.t - from_ms(co.get("first") or co["start"])).total_seconds()))
    if c["step"] == 1:
        mo = s.mo(co.get("oldMo"))
        c.update(mo=mo, moTimer=kscreen.hms((s.t - sh.mo_start).total_seconds()) if sh.mo_start else "0:00:00",
                 close=bool(mo and mo.produced - mo.scrap >= mo.qty), endSteps=engine.machine_list("endSteps", sh.line),
                 logged=engine.run_scrap(sh, mo) if mo else 0)
    elif c["step"] == 2:
        c["queue"] = list(MO.objects.filter(line__name=sh.line, status="OPEN").exclude(pk=co.get("oldMo")).order_by("seq")[:4])
        c["mo_pick"] = request.GET.get("mo") or (str(c["queue"][0].number) if c["queue"] else "")
    else:
        nmo = s.mo(co.get("newMo"))
        p = s.fresh_part(nmo.part if nmo else "")
        steps = engine.co_steps(co, sh.line)
        from instructions.models import Media
        ids = {k for st in steps for k in st.get("media") or []}
        media = {m.pk: m for m in Media.objects.filter(pk__in=ids).defer("data", "thumb")} if ids else {}
        def step(i, st):
            st = dict(st)
            setting, value, unit = str(st.get("setting") or ""), str(st.get("value") if st.get("value") is not None else ""), str(st.get("unit") or "")
            who = (co.get("doneBy") or {}).get(str(i)) if i in (co.get("done") or []) else None
            st.update(whoDone=f"{who.get('by') or 'Nobody logged in'} · {engine.fmt_t(from_ms(who['t']))}" if who and who.get("t") else "",
                      i=i, done=i in (co.get("done") or []), tag=f"{setting} {value}{unit}".strip() if (setting or value) else "",
                      imageOk=str(st.get("image") or "").lower().startswith(("http://", "https://")),
                      m=[media[k] for k in st.get("media") or [] if k in media])
            return st
        c.update(nmo=nmo, p=p, steps=[step(i, st) for i, st in enumerate(steps)], inkjet=co.get("inkjet"), ovr=co.get("override"),
                 oldMo=s.mo(co.get("oldMo")) if co.get("oldMo") else None,
                 settingsRows=co.get("settings") or [], src=co.get("src"), cacheT=co.get("cacheT"),
                 wi=co.get("wi"), canReload=True,
                 override=" · ".join(f"{k} {v}" for k, v in (sh.override or {}).items()))
    return modal_response(request, "hmi/k/m_changeover.html", **ctx_for(s, **c))


@modal("copass", login=True)
def m_copass(request, s):
    sh = s.shift
    if not (sh and sh.co and sh.co.get("start") and sh.person_id):
        return thin.refused("There's no changeover under way to pass over.")
    co = sh.co
    n, done = len(engine.co_steps(co, sh.line)), len(co.get("done") or [])
    w = engine.log_window(sh, s.t)
    return modal_response(request, "hmi/k/m_copass.html", **ctx_for(s, sh=sh, n=n, done=done, nextShift="the next operator" if w.get("noShift") else "the next shift",
                                                                 newMo=s.mo(co.get("newMo"))))


@modal("qc", login=True)
def m_qc(request, s):
    """A QC check: the items this machine's stage checks (Quality › QC checks). ?edit=id corrects a finished one."""
    from . import qc
    from .models import QCCheck
    edit = None
    if request.GET.get("edit"):
        if not s.can("qc.edit"):
            return login_needed(request, s, {"perm": "qc.edit"})
        edit = QCCheck.objects.filter(pk=request.GET["edit"]).select_related("shift__machine").first()
        if not edit:
            return thin.refused("That QC check is no longer on file.")
        stage = edit.shift.machine.stage if edit.shift and edit.shift.machine else "s1"
        if not edit.item:
            return thin.refused("This check has no part, so it can't be judged here. Correct its part in Supervisor › History › Edit.")
        p = s.fresh_part(edit.item)
        vals = edit.values or {k: getattr(edit, k) for k in ("len", "wid", "ph", "sbl", "sbr", "glue", "cut", "scrap")}
    else:
        if not s.shift_on:
            return thin.refused("Start a shift before logging QC.")
        stage = s.machine.stage if s.machine else "s1"
        if not s.current_item():
            return thin.refused("No MO is running, so there's no part to check against. Start an MO in Change over first "
                                "(or a supervisor runs it by filter number).")
        p = s.fresh_part(s.current_item())
        vals = {}
    tols = qc.plant_tols(s.cfg)
    items = [{**it, "target": qc.target(it, p), "tolTxt": qc.fmt_tol(qc.item_spec(it), p, tols) if it["type"] == "measure" else "",
              "v": vals.get(it["key"])} for it in qc.items_for(stage, s.cfg)]
    late = 0 if edit else s.qc_late()
    return modal_response(request, "hmi/k/m_qc.html", **ctx_for(s, p=p, items=items, edit=edit, late=late,
                                                                 afterCo=bool(not edit and s.shift and s.shift.qc_after_co),
                                                                 recheck=None if edit else (s.qc_recheck() or [None])[0]))


@modal("qcDisapprove")
def m_qc_disapprove(request, s):
    from .models import QCCheck
    if not s.can("qc.approve"):
        return login_needed(request, s, {"perm": "qc.approve"})
    q = QCCheck.objects.filter(pk=request.GET.get("id")).first()
    if not q:
        return thin.refused("That QC check is no longer on file.")
    return modal_response(request, "hmi/k/m_qc_disapprove.html", **ctx_for(s, q=q))


@modal("rolls", login=True)
def m_rolls(request, s, pre=None, photo=None, how=None, read=None):
    """Roll tags: scan or type a roll's tag, or take a picture of it and let the screen read it (hmi/rolltag.py)."""
    if not s.shift_on:
        return thin.refused("Start a shift before scanning rolls.")
    from . import trace
    from .models import RollTag
    rolls = list(RollTag.objects.filter(shift=s.shift).order_by("-t"))
    rows = []
    for i, r in enumerate(rolls):
        nxt = s.shift.count if i == 0 else rolls[i - 1].count_at
        rows.append({"r": r, "units": nxt - r.count_at})
    media = s.part(s.current_item())["media"] if s.current_item() else ""
    pre = dict(pre or {})
    pre.setdefault("material", media)
    cur = trace.current_roll(s.station)
    return modal_response(request, "hmi/k/m_rolls.html", **ctx_for(s, rows=rows, media=media, pre=pre, photo=photo, how=how or {}, read=read,
                                                                   cur=cur, curPhoto=cur.photos.values_list("id", flat=True).first() if cur else None,
                                                                   static_v=_rolltag_assets()))


def _rolltag_assets():
    """Where the screen finds the text reader (Tesseract, served by the suite itself: no internet)."""
    from django.conf import settings
    from django.templatetags.static import static
    base = settings.STATIC_URL.rstrip("/") + "/vendor/tesseract"   # folders: the reader adds the file names itself
    return {"lib": static("vendor/tesseract/tesseract.min.js"), "worker": static("vendor/tesseract/worker.min.js"), "core": base + "/core",
            "lang": base + "/lang"}


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


@modal("rollPhoto")
def m_roll_photo(request, s):
    from .models import RollTagPhoto
    ph = RollTagPhoto.objects.filter(pk=request.GET.get("id") or 0).select_related("roll").defer("data", "thumb").first()
    if not ph:
        return thin.refused("That picture is no longer on file.")
    return modal_response(request, "hmi/k/m_roll_photo.html", **ctx_for(s, ph=ph, back=s.shift_on))


@modal("drawing")
def m_drawing(request, s):
    item = s.current_item()
    if not item:
        return thin.refused("No MO is running, so there's no drawing to show. Part Info shows any part's drawing.")
    p = s.fresh_part(item)
    note = (f"Rev {p['rev']} confirmed current at {engine.fmt_t(s.shift.draw_checked)}." if s.shift and s.shift.draw_checked
            else "Showing the drawing on file at shift start.")
    from . import qc
    tols = qc.plant_tols(s.cfg)
    tol_txt = {k: qc.fmt_tol(k, p, tols) for k in ("len", "wid")}
    tol_txt["other"] = qc.fmt_tol("ph", p, tols)
    return modal_response(request, "hmi/k/m_drawing.html", **ctx_for(s, p=p, tolTxt=tol_txt, note=request.GET.get("msg") or note,
                                                                     pleat_xs=[70 + i * 14 for i in range(16)]))


@modal("override")
def m_override(request, s):
    if not s.can("runs.override"):
        return login_needed(request, s, {"perm": "runs.override"}, "override")
    if not s.shift_on:
        return thin.refused("Start a shift first.")
    p = s.part(s.current_item())
    return modal_response(request, "hmi/k/m_override.html", **ctx_for(s, o=s.shift.override or {}, p=p, rated=int(s.machine.ppm), unit=s.machine.speed_short))


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
    return modal_response(request, "hmi/k/m_end.html", **ctx_for(s, stats=shift_stats(s.shift), mo=s.cur_mo, need=len(s.need_reason()),
                                                                 rollPics=int(engine.rules().get("rollPhotos", 2) or 0), pics=PIC_FIELDS))


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


@modal("paycomPositions")
def m_paycom_positions(request, s):
    """Pick the Paycom position titles whose people are imported to the Team list."""
    from . import kadmin
    r = kadmin.run(request, s, "paycomPositions", {})
    if not r["ok"]:
        return login_needed(request, s, r) if r.get("admin") or r.get("login") else thin.refused(r["error"])
    P = s.settings.get("paycom") or {}
    cur = [x.strip().lower() for x in str(P.get("position") or "").split(",") if x.strip()]
    exact = bool(P.get("positionExact"))
    rows = [{"name": n, "n": c, "on": bool(cur) and ((n.lower() in cur) if exact else any(x in n.lower() for x in cur))} for n, c in r["data"]]
    return modal_response(request, "hmi/k/m_paycom_positions.html", **ctx_for(s, rows=rows, dept=P.get("dept") or ""))


@modal("notices")
def m_notices(request, s):
    """Everything this machine's screen hasn't acknowledged yet, newest first."""
    return modal_response(request, "hmi/k/m_notices.html", **ctx_for(s, N=s.notibar(all_=True)))


@modal("extraBreak")
def m_extra_break(request, s):
    if not s.can("lists.edit"):
        return login_needed(request, s, {"perm": "lists.edit"})
    from datetime import timedelta

    from plant.util import local
    t = local(s.t)
    nxt = (t + timedelta(minutes=30 - t.minute % 30 + 30)).strftime("%H:%M")
    return modal_response(request, "hmi/k/m_extra_break.html",
                          **ctx_for(s, today=t.date().isoformat(), start=nxt, lines=[m.name for m in s.active_lines]))


@modal("addPerson")
def m_add_person(request, s):
    if not s.can("team.manage"):
        return login_needed(request, s, {"perm": "team.manage"}, "addPerson")
    return modal_response(request, "hmi/k/m_add_person.html", **ctx_for(s))


@modal("editPerson")
def m_edit_person(request, s):
    if not s.can("team.manage"):
        return login_needed(request, s, {"perm": "team.manage"})
    u = Person.objects.filter(pk=request.GET.get("id")).prefetch_related("trained").first()
    if not u:
        return thin.refused("That person is no longer on the list.")
    if u.role == "admin" and not s.can("permissions.manage"):
        return thin.refused("Only someone who manages permissions can change an Admin's login.")
    tr = {m.name for m in u.trained.all()}
    return modal_response(request, "hmi/k/m_edit_person.html",
                          **ctx_for(s, u=u, lines=[(m.name, m.name in tr) for m in s.active_lines], you=bool(s.me and u.pk == s.me.pk)))


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
    if s.sup:   # each tap keeps a supervisor override going
        request.session["sup"] = {"id": s.sup.pk, "until": now().timestamp() + SUP_MINUTES * 60}
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
    from plant import perms
    perm = p.get("perm") if p.get("perm") in perms.PERMS else ""
    u, err = pin_login(request, p.get("pin"), p.get("user") or None, perm, require_name=bool(s.me))   # switching person: name and PIN
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


@action("supStart")
def a_sup_start(request, s, p):
    """Supervisor override: a supervisor's PIN takes over this screen's permissions, without logging the operator out."""
    from .views import check_pin
    u, err = check_pin(request, p.get("pin"), p.get("user") or None, "floor.override")
    if not u:
        return err_in_modal(err)
    request.session["sup"] = {"id": u.pk, "until": now().timestamp() + SUP_MINUTES * 60}
    engine.audit(engine.Ctx(user=u), f"Supervisor override on {s.station or 'a screen'}" + (f" (logged in: {s.me.name})" if s.me else ""))
    if p.get("next") in ("nomo", "como", "coforce"):   # the override was for one thing: go straight on to it
        resp = MODALS[p["next"]][0](request, screen(request))
        return thin.trigger(resp, toast=f"Supervisor override: {u.name}", changed=True)
    return thin.done(toast=f"Supervisor override: {u.name}. It ends after {SUP_MINUTES} minutes without a tap.", close=True)


@action("supEnd")
def a_sup_end(request, s, p):
    request.session.pop("sup", None)
    return thin.done(toast="Supervisor override ended")


@action("logout")
def a_logout(request, s, p):
    request.session.pop("pid", None)
    return thin.done(toast="Logged out")


@action("station")
def a_station(request, s, p):
    if not s.lock and Machine.objects.filter(name=p.get("line", ""), stage="s1").exists():
        request.session["station"] = p["line"]
    return thin.done()


ROLL_PICS = (("rollPic", "roll", "the roll"), ("rollTag", "tag", "the roll's tag"))
PIC_FIELDS = [("rollPic", "Roll"), ("rollTag", "Tag")]


def _roll_pics(request, when):
    """The start/end-of-shift pictures of the roll and its tag sent with the form, processed (not saved yet).
    Raises PhotoError when one is missing and Admin › Rules requires them, or a picture can't be read."""
    from . import photos
    need = int(engine.rules().get("rollPhotos", 2) or 0)
    if not need:
        return []
    out, missing = [], []
    for field, what, label in ROLL_PICS:
        f = request.FILES.get(field)
        if not f:
            missing.append(label)
            continue
        if f.size > photos.MAX_BYTES:
            raise photos.PhotoError("That picture is too large (over 25 MB).")
        out.append((what, *photos.process(f.read())))
    if missing and need >= 2:
        left = "you're starting with" if when == "start" else "you're leaving for the next shift"
        raise photos.PhotoError(f"Take a picture of {' and '.join(missing)} {left}.")
    return out


def _keep_roll_pics(pics, sh, when, who, t):
    from .models import RollTagPhoto
    for what, jpeg, thumb, w, h in pics:
        RollTagPhoto.objects.create(shift=sh, kind=when, what=what, t=t, by=who, line=sh.line if sh else "", data=jpeg, thumb=thumb,
                                    size=len(jpeg), width=w, height=h)


def last_left(line, t):
    """The roll and tag pictures the last shift on this line left for the next one (shown at Start of shift)."""
    from .models import RollTagPhoto
    last = RollTagPhoto.objects.filter(line=line, kind="end", t__gte=t - timedelta(days=4)).order_by("-t").first() if line else None
    if not last:
        return []
    return list(RollTagPhoto.objects.filter(shift_id=last.shift_id, kind="end").defer("data", "thumb").order_by("what"))


def co_pick(line, number):
    """Start of shift on a line that ran another MO with nobody logged in: the stops to pick the changeover from."""
    from .models import Downtime
    m = engine.line_of(line)
    cur = engine.active_run(m) if m else None
    mo = engine.open_mo(str(number or "").strip()) if number else None
    if not (cur and engine.unattended(cur) and cur.mo_id and cur.count and mo and mo.pk != cur.mo_id) or (cur.state == "changeover" and cur.co) \
            or engine.recipe_cut(cur)[0]:   # (the machine reported when: no need to ask)
        return {"ask": False}
    stops = list(Downtime.objects.filter(shift=cur).order_by("start_time"))
    return {"ask": True, "old": cur.mo, "new": mo, "since": cur.start, "made": cur.count, "stops": stops, "stopped": cur.stop_start}


@action("startShift")
def a_start(request, s, p):
    n_safety, n_pm = len(s.cfg.get("safety", [])), len(s.cfg.get("pm", []))
    on = set(request.POST.getlist("check"))
    p = {"line": p.get("line", ""), "mo": (p.get("mo") or "").strip(), "issues": p.get("issues", ""),
         "safety": [f"s{i}" in on for i in range(n_safety)], "pm": [f"p{i}" in on for i in range(n_pm)],
         "acks": request.POST.getlist("ack"), "coAt": p.get("coAt", "")}
    from . import photos
    try:
        pics = _roll_pics(request, "start")
    except photos.PhotoError as e:
        return err_in_modal(str(e))
    r = run(request, s, "startShift", p)
    if not r["ok"]:
        return err_in_modal(r["error"])
    if pics:
        sh = ShiftRun.objects.filter(machine__name=p["line"], active=True).first()
        _keep_roll_pics(pics, sh, "start", s.me.name if s.me else "", now())
    if not s.lock:
        request.session["station"] = p["line"]
    return go("controls", toast=r.get("msg"), s=s)


def _co_next(request, s, r):
    return changeover_modal(request, screen(request))


ACTIONS["coCancel"] = simple("coCancel", toast=False, close=True)
@action("coEnd")
def a_co_end(request, s, p):
    """End Order: the run's final count and scrap, and every end-of-order item checked; then the changeover opens."""
    n = len(engine.machine_list("endSteps", s.station))
    on = set(request.POST.getlist("end"))
    r = run(request, s, "coEnd", {"qty": p.get("qty", ""), "scrap": p.get("scrap", ""), "close": p.get("close"),
                                  "checks": [str(i) in on for i in range(n)]})
    if not r["ok"]:
        return err_in_modal(r["error"])
    return thin.trigger(changeover_modal(request, screen(request)), toast="Order ended. Now the changeover: pick the next MO.", changed=True)


ACTIONS["coBack"] = simple("coBack", toast=False, then=_co_next)


@modal("como")
def m_como(request, s):
    """Supervisor override: change over to an MO that isn't lined up for this line."""
    if not s.can("runs.nomo"):
        return modal_response(request, "hmi/k/m_login.html", **ctx_for(s, perm="floor.override", perm_label=_perm_label("floor.override"),
                                                                       sup=True, next="como", why="nomo",
                                                                       people=kscreen.people_for_login("runs.nomo")))
    if not (s.shift and (s.shift.co or {}).get("step", 0) >= 2):
        return thin.refused("End the current order first.")
    return modal_response(request, "hmi/k/m_como.html", **ctx_for(s, sh=s.shift))


@modal("coforce")
def m_coforce(request, s):
    """A supervisor force-ends the changeover on this screen's line (nobody finished it)."""
    if not s.can("floor.override"):
        return modal_response(request, "hmi/k/m_login.html", **ctx_for(s, perm="floor.override", perm_label=_perm_label("floor.override"),
                                                                       sup=True, next="coforce", why="coforce",
                                                                       people=kscreen.people_for_login("floor.override")))
    sh = s.shift
    if not (sh and sh.co and sh.co.get("step", 1) >= 2):
        return thin.refused("There's no changeover to end.")
    new = s.mo(sh.co.get("newMo")) if sh.co.get("newMo") else None
    return modal_response(request, "hmi/k/m_coforce.html", **ctx_for(s, sh=sh, new=new))


@action("coForceEnd")
def a_co_force_end(request, s, p):
    r = run(request, s, "coForceEnd", {"why": p.get("why", ""), "run": p.get("run") == "1"})
    if not r["ok"]:
        return err_in_modal(r["error"])
    return thin.done(toast=r.get("msg"), close=True, changed=True)


@action("coPickMo")
def a_co_pick_mo(request, s, p):
    r = run(request, s, "coPickMo", {k: p.get(k, "") for k in ("mo", "part", "qty", "why")})
    if not r["ok"]:
        return err_in_modal(r["error"])
    return thin.trigger(changeover_modal(request, screen(request)), toast=r.get("msg"), changed=True)


@modal("attach", login=True)
def m_attach(request, s):
    """Running without an MO (or by filter number): pick the MO it's for."""
    sh = s.shift
    if not s.shift_on or not sh or sh.co or (sh.mo and sh.mo.source != "floor"):
        return thin.refused("This line is already running an MO.")
    from django.db.models import Sum
    from .models import MoRun
    if sh.mo:
        made = sh.mo_count
    else:   # filters counted this shift that no MO has taken yet
        made = max(0, sh.count - (MoRun.objects.filter(shift=sh).aggregate(n=Sum("qty"))["n"] or 0))
    queue = list(MO.objects.filter(line__name=sh.line, status="OPEN").exclude(pk=sh.mo_id).order_by("seq")[:4])
    pick = request.GET.get("mo") or (str(queue[0].number) if queue else "")
    return modal_response(request, "hmi/k/m_attach.html", **ctx_for(s, sh=sh, made=made, queue=queue, mo_pick=pick))


@action("moAttach")
def a_mo_attach(request, s, p):
    r = run(request, s, "moAttach", {"mo": p.get("mo", ""), "qty": p.get("qty", "")})
    if not r["ok"]:
        if r.get("login"):
            return login_needed(request, s, r)
        return err_in_modal(r["error"])
    return thin.done(toast=r.get("msg"), close=True, changed=True)
ACTIONS["coPick"] = simple("coPick", toast=False, then=_co_next)


@modal("nomo")
def m_nomo(request, s):
    """No MO to run: a supervisor enters the filter number (Supervisor button first when the operator can't)."""
    if not s.can("runs.nomo"):
        return modal_response(request, "hmi/k/m_login.html", **ctx_for(s, perm="floor.override", perm_label=_perm_label("floor.override"),
                                                                       sup=True, next="nomo", why="nomo",
                                                                       people=kscreen.people_for_login("runs.nomo")))
    if not (s.shift and (s.shift.co or {}).get("step", 0) >= 2):
        return thin.refused("Open Change over and end the current MO first.")
    return modal_response(request, "hmi/k/m_nomo.html", **ctx_for(s, sh=s.shift))


@action("coPickPart")
def a_co_pick_part(request, s, p):
    r = run(request, s, "coPickPart", {k: p.get(k, "") for k in ("part", "qty", "why")})
    if not r["ok"]:
        return err_in_modal(r["error"])
    return thin.trigger(changeover_modal(request, screen(request)), toast=r.get("msg"), changed=True)
ACTIONS["coReload"] = simple("coReload", toast=False, then=_co_next)
ACTIONS["coFinish"] = simple("coFinish", close=True)
@action("coPass")
def a_co_pass(request, s, p):
    r = run(request, s, "coPass", {"note": p.get("note", "")})
    if not r["ok"]:
        return err_in_modal(r["error"])
    return go("controls", toast=r.get("msg"), s=s)
@action("coStep")
def a_co_step(request, s, p):
    try:
        i = int(p.get("i"))
    except (TypeError, ValueError):
        return thin.refused("Unknown step.")
    r = run(request, s, "coStep", {"i": i, "checked": bool(p.get(f"step{i}"))})
    return thin.done(changed=True) if r["ok"] else thin.refused(r["error"])
ACTIONS["rollAdd"] = simple("rollAdd", toast=False, then=lambda request, s, r: m_rolls(request, s))
ACTIONS["rollOff"] = simple("rollOff", then=lambda request, s, r: thin.trigger(m_rolls(request, s), toast=r.get("msg")))


@action("rollRead")
def a_roll_read(request, s, p):
    """A picture of a roll tag, and what the tablet read from it: fill in the fields and keep the picture."""
    from . import photos, rolltag
    from .models import RollTagPhoto
    if not s.me:
        return login_needed(request, s, {"login": True})
    if not s.shift_on:
        return thin.refused("Start a shift before scanning rolls.")
    ocr = p.get("ocr") if isinstance(p.get("ocr"), dict) else {}
    res = rolltag.read(ocr.get("lines") or [], ocr.get("barcodes") or [])
    f = request.FILES.get("tagphoto")
    ph = None
    if f:
        try:
            if f.size > photos.MAX_BYTES:
                raise photos.PhotoError("That picture is too large (over 25 MB).")
            jpeg, thumb, w, h = photos.process(f.read())
            ph = RollTagPhoto.objects.create(t=now(), by=s.me.name, line=s.station, data=jpeg, thumb=thumb, size=len(jpeg), width=w, height=h,
                                             text=(res["text"] + ("\nBarcodes: " + ", ".join(res["barcodes"]) if res["barcodes"] else ""))[:8000],
                                             found=res["fields"])
        except photos.PhotoError as e:
            return err_in_modal(str(e))
    n = len(res["fields"])
    if ocr.get("error") and not n:
        read = {"ok": False, "text": f"This screen couldn't read the picture ({str(ocr['error'])[:120]}). The picture is kept with the roll; type the fields."}
    elif not n:
        read = {"ok": False, "text": "Nothing on the tag could be matched to a field. Type them in; the picture is kept with the roll. "
                                     "If your tags label things differently, a shift lead can add the labels in Quality › Traceability."}
    else:
        names = [lbl for k, lbl in rolltag.FIELDS if k in res["fields"]]
        read = {"ok": True, "text": f"Read {', '.join(names)} from the picture. Check them against the tag, then Add roll."}
    read["raw"] = res["text"]
    read["barcodes"] = res["barcodes"]
    return m_rolls(request, s, pre=res["fields"], photo=ph.pk if ph else None, how=res["how"], read=read)

ACTIONS["noteAdd"] = simple("noteAdd", toast=False, then=lambda request, s, r: m_notes(request, s))
ACTIONS["overrideSet"] = simple("overrideSet", close=True)
ACTIONS["overrideClear"] = simple("overrideClear", toast=False, close=True)
ACTIONS["ticketNote"] = simple("ticketNote")
# a request's status (start, waiting on parts, complete) is only changed on the Maintenance page (/maintenance/),
# never from the Operator Kiosk
def _ack_then(request, s, r):
    """Acknowledged from the Alarms dialog: draw the dialog again without that notice (and refresh the badge)."""
    if request.POST.get("from") == "alarms":
        return thin.trigger(m_alarms(request, s), changed=True)
    return thin.done()


ACTIONS["ackNotices"] = simple("ackNotices", toast=False, then=_ack_then)


@action("ackSensor")
def a_ack_sensor(request, s, p):
    """A sensor alarm acknowledged in the Alarms dialog: the dialog comes back without it."""
    from sensors.engine import ack
    who = s.actor.name if s.actor else f"{s.station or 'A'} screen (not logged in)"
    r = ack(p.get("id"), who)
    if not r["ok"]:
        return err_in_modal(r["error"])
    return thin.trigger(m_alarms(request, s), changed=True)
ACTIONS["settingSet"] = simple("settingSet", toast=False)
ACTIONS["userSet"] = simple("userSet", toast=False)
ACTIONS["paycomImport"] = simple("paycomImport")
ACTIONS["paycomPositionsSet"] = simple("paycomPositionsSet", close=True)
ACTIONS["extraBreakAdd"] = simple("extraBreakAdd", close=True)
ACTIONS["extraBreakDel"] = simple("extraBreakDel")


@action("userAdd")
def a_user_add(request, s, p):
    r = run(request, s, "userAdd", {k: p.get(k, "") for k in ("name", "pin", "role")})
    if not r["ok"]:
        return login_needed(request, s, r) if r.get("admin") or r.get("login") else err_in_modal(r["error"])
    return thin.done(toast=r.get("msg") or "Added", close=True)


@action("personSave")
def a_person_save(request, s, p):
    q = {"id": p.get("id"), "name": p.get("name", ""), "role": p.get("role", ""), "pin": p.get("pin", ""),
         "trained": (p.get("trained") or []) if p.get("hasTrained") else None}
    if p.get("hasActive"):
        q["active"] = bool(p.get("active"))
    if not p.get("role"):
        q.pop("role")
    r = run(request, s, "personSave", q)
    if not r["ok"]:
        return login_needed(request, s, r) if r.get("admin") or r.get("login") else err_in_modal(r["error"])
    return thin.done(toast=r.get("msg"), close=True)


@action("wiAck")
def a_wi_ack(request, s, p):
    """Read and understood: the Instructions page comes back with it ticked."""
    r = run(request, s, "wiAck", {"rev": p.get("rev")})
    if not r["ok"]:
        return login_needed(request, s, r) if r.get("login") else thin.refused(r["error"])
    s = screen(request)
    return render(request, "hmi/k/v_instructions.html", ctx_for(s, page="instructions", **view_data(s, "instructions", request)))


@action("train")
def a_train(request, s, p):
    r = run(request, s, "userSet", {"id": p.get("id"), "f": "train", "v": {"line": p.get("line"), "on": bool(p.get("on"))}})
    return thin.done() if r["ok"] else (login_needed(request, s, r) if r.get("admin") else thin.refused(r["error"]))


@action("qcSave")
def a_qc(request, s, p):
    r = run(request, s, "qcEdit" if p.get("id") else "qcSave", p)
    if not r["ok"]:
        return login_needed(request, s, r) if r.get("admin") else err_in_modal(r["error"])
    return thin.done(toast=r.get("msg"), close=True)


@action("qcReview")
def a_qc_review(request, s, p):
    r = run(request, s, "qcReview", {k: p.get(k, "") for k in ("id", "decision", "scrap")})
    if not r["ok"]:
        return login_needed(request, s, r) if r.get("admin") else (err_in_modal(r["error"]) if request.headers.get("X-In-Modal") else thin.refused(r["error"]))
    return thin.done(toast=r.get("msg"), close=True)


@action("qcField")
def a_qc_field(request, s, p):
    """As a measurement is typed: in or out of tolerance (the part's spec), shown on the field."""
    from . import qc
    k = p.get("k", "")
    pp = s.fresh_part(p.get("item") or s.current_item())
    it = next((x for x in qc.items_for(s.machine.stage if s.machine else "s1", s.cfg) if x["key"] == k), None)
    target = qc.target(it, pp) if it else None
    try:
        v = float(p.get(k) or "")
    except ValueError:
        v = None
    cls = "" if v is None or target is None else ("in" if qc.in_tol(v, target, qc.item_spec(it), pp, qc.plant_tols(s.cfg)) else "out")
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
    if p.get("from") == "info":   # Part Info: the page shows the new drawing as it refreshes
        return thin.done(toast=r.get("msg"))
    request.GET = request.GET.copy()
    request.GET["msg"] = r.get("msg", "")
    return m_drawing(request, screen(request))


@action("endShift")
def a_end(request, s, p):
    from . import photos
    try:
        pics = _roll_pics(request, "end")
    except photos.PhotoError as e:
        return err_in_modal(str(e))
    sh = s.shift
    r = run(request, s, "endShift", {"issues": p.get("issues", "")})
    if not r["ok"]:
        return err_in_modal(r["error"])
    if pics and sh:
        _keep_roll_pics(pics, sh, "end", s.me.name if s.me else "", now())
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
    from .models import Downtime
    before = Downtime.objects.filter(pk=p.get("id") or 0).values("reason", "notes").first()
    r = run(request, s, "downtimeSet", q)
    if not r["ok"]:
        if r.get("login"):
            return login_needed(request, s, r, next_modal="downPrompt")
        return thin.refused(r["error"])
    undo = None
    if before and "reason" in q and q["reason"] != before["reason"]:   # Undo puts the stop's reason back as it was
        undo = {"text": f"Reason set: {q['reason'] or 'cleared'}", "post": "/kiosk/do/downtimeSet",
                "vals": {"id": str(p.get("id")), "reason": before["reason"] or ""}}
    extra = {"suite:undo": undo} if undo else {}
    if p.get("back") == "downPrompt":
        s2 = screen(request)
        if not s2.need_reason():
            return thin.trigger(thin.done(toast="Thanks — every stop has a reason.", close=True), **extra)
        return thin.trigger(m_down_prompt(request, s2), changed=True, **extra)
    return thin.trigger(thin.done(changed=True), **extra)


@require_GET
def db_backup_file(request, name):
    """Download one of the database backups (Admin › Backup); only for people who manage backups."""
    from django.http import FileResponse

    from plant import dbbackup
    s = screen(request)
    if not s.can("backup.manage"):
        raise Http404("No such backup")
    p = dbbackup.path_of(name)
    if not p:
        raise Http404("No such backup")
    return FileResponse(open(p, "rb"), as_attachment=True, filename=p.name, content_type="application/gzip")


@action("ackStop")
def a_ack_stop(request, s, p):
    """The operator saw a stop pop-up: don't show that stop again on this screen."""
    key = str(p.get("key") or "")[:40]
    acked = [k for k in (request.session.get("stopAck") or []) if k != key][-50:] + ([key] if key else [])
    request.session["stopAck"] = acked
    return render(request, "hmi/k/_notibar.html", ctx_for(s, N=s.notibar(), page=p.get("page", "")))


@modal("changeop")
def m_changeop(request, s):
    """Change Operator: the next person logs in and takes the line over from now."""
    sh = s.shift
    return modal_response(request, "hmi/k/m_login.html", **ctx_for(s, people=kscreen.people_for_login(""),
                                                                   changeop={"before": sh.user_name if sh and sh.user_name else "Nobody",
                                                                             "mo": sh.mo.number if sh and sh.mo else ""}))


@action("changeOp")
def a_change_op(request, s, p):
    u, err = pin_login(request, p.get("pin"), p.get("user") or None, "", require_name=True)
    if not u:
        return err_in_modal(err)
    r = engine.execute("changeOperator", {"line": s.station}, engine.Ctx(user=u, line=s.station))
    if not r["ok"]:
        return err_in_modal(r["error"])
    return thin.done(toast=r["msg"], close=True, changed=True)


@action("carryOn")
def a_carry_on(request, s, p):
    r = engine.execute("carryOn", {"id": p.get("id"), "yes": p.get("yes") == "1"}, engine.Ctx(line=s.station))
    return thin.done(toast=r.get("msg") if r["ok"] else r["error"], changed=True)
