"""Labor Budget mode: the Labor Calculator, with its plan kept on the server. Shift leads only (it shows wages)."""
import json
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.templatetags.static import static
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from hmi.views import body, current_person, touch
from plant.api import BadRequest, json_api, to_int
from plant.models import PlantSettings

from .models import BUDGETS, LaborPlan, LaborPlanVersion

PAGE = Path(__file__).resolve().parent / "page" / "labor.html"
MAX_BYTES = 4 * 1024 * 1024
KEEP_VERSIONS = 200
_page_cache = {}


def _lead(request, perm="labor.view"):
    from plant import perms
    me, note = current_person(request)
    if perms.can(me, perm):
        return me, None
    return None, note or (f"{me.name} doesn't have permission to open the Labor Budget." if me else None)


def _json_for_html(obj):
    """JSON that can sit inside <script> without ending it early."""
    return json.dumps(obj).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _suitebar(me, only=False):
    """The suite's top bar. On the Labor Budget's own address (/budget/) it has no other sections."""
    from plant.context import MODES
    nav = "" if only else ('<nav class="suitetabs" aria-label="Modes">' + "".join(
        f'<a href="{href}"{" aria-current=\"page\"" if k == "labor" else ""}>{_esc(label)}</a>' for k, href, label in MODES) + "</nav>")
    return ('<header class="suitebar no-print"><a class="suitebrand" href="' + ("/budget/" if only else "/") + '" aria-label="Production Suite home">'
            '<svg viewBox="0 0 34 20" aria-hidden="true"><polyline points="1,17 5,3 9,17 13,3 17,17 21,3 25,17 29,3 33,17" fill="none" '
            'stroke="currentColor" stroke-width="2.4" stroke-linejoin="round"/></svg><span>' + ("Labor Budget" if only else "Production Suite") + '</span></a>'
            + nav +
            '<span class="suitesync"><span id="labor-sync" class="suitesync-dot" aria-live="polite">Saved</span>'
            f'<span class="suitewho">{_esc(me.name)}</span><button type="button" id="labor-logout" class="suitebtn">Log out</button></span></header>'
            f'<div class="suite-rev" style="position:fixed;right:6px;bottom:3px;z-index:2147483000;font:500 10px/1.2 ui-monospace,Consolas,monospace;'
            f'color:#64748b;opacity:.6;pointer-events:none">v{_esc(settings.SUITE["VERSION"])} · rev {_esc(settings.SUITE["REVISION"])}</div>')


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def budget_only(request):
    return page(request, only=True)


@ensure_csrf_cookie
def page(request, only=False):
    """The Labor Budget. `only`: its own address, /budget/, which shows nothing else of the suite (like a machine's
    own screen), for people who may only see the budget."""
    me, note = _lead(request)
    if not me:
        return render(request, "labor/login.html", {"mode": "labor", "note": note, "kiosk_lock": only, "home": "/budget/" if only else "/labor/"})
    touch(request)
    from .calcupdate import page_path
    src = page_path()   # a calculator installed from GitHub (Admin › Updates), else the one that came with the suite
    try:
        key = (str(src), src.stat().st_mtime_ns)
    except OSError:
        src, key = PAGE, (str(PAGE), 0)
    if _page_cache.get("key") != key:
        _page_cache.update(key=key, html=src.read_text(encoding="utf-8"))
    html = _page_cache["html"]
    from .sync import merge, suite_view
    view = suite_view()
    budgets, notes = {}, []
    for b in BUDGETS:   # A: the plant's plan (the Scheduler uses it); B: a second budget to compare
        plan = LaborPlan.load(b)
        data, n = merge(plan.data, view) if plan.version else (None, [])
        notes += n if b == "A" else []
        budgets[b] = {"version": plan.version, "data": data, "updatedBy": plan.updated_by,
                      "updatedAt": plan.updated_at.isoformat() if plan.updated_at else None}
    # a plant with no plan yet starts from its own lines and shifts
    starter = merge({"lines": [], "schedules": [], "slotAssignments": {}}, view)[0]
    boot = {"budgets": budgets, "starter": starter, "syncNotes": notes, "user": me.name,
            "timeoutMin": PlantSettings.load().config["rules"].get("adminTimeout", 10)}
    out = (html.replace("__LABOR_CSS__", static("labor/labor.css"))
               .replace("__SUITEBAR_CSS__", static("labor/suitebar.css"))
               .replace("__LABOR_SYNC_JS__", static("labor/sync.js"))
               .replace("__SUITEBAR__", _suitebar(me, only))
               .replace("__LABOR_BOOT__", _json_for_html(boot)))
    resp = HttpResponse(out, content_type="text/html; charset=utf-8")
    # The calculator wires its buttons with inline handlers, so this one page allows inline script.
    # It still can't load or send anything outside the suite (default-src/connect-src 'self').
    resp["Content-Security-Policy"] = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                                       "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; frame-src 'none'; "
                                       "frame-ancestors 'self'; form-action 'self'; base-uri 'self'; object-src 'none'")
    resp["Cache-Control"] = "no-store"
    return resp


def _budget(v):
    return v if v in BUDGETS else "A"


def _need_lead(request, perm="labor.view"):
    me, note = _lead(request, perm)
    if not me:
        return None, JsonResponse({"ok": False, "login": True, "error": note or "Log in as a shift lead to see the labor budget."}, status=403)
    touch(request)
    return me, None


@json_api
@require_GET
def state(request):
    me, denied = _need_lead(request)
    if denied:
        return denied
    from .sync import merge
    plan = LaborPlan.load(_budget(request.GET.get("budget")))
    return JsonResponse({"ok": True, "version": plan.version, "data": merge(plan.data)[0] if plan.version else None,
                         "updatedBy": plan.updated_by, "updatedAt": plan.updated_at.isoformat() if plan.updated_at else None})


def validate_plan(data):
    if not isinstance(data, dict):
        raise BadRequest("The plan must be an object.")
    for key in ("lines", "schedules"):
        if not isinstance(data.get(key), list):
            raise BadRequest(f"The plan is missing its {key} list.")
    if len(json.dumps(data)) > MAX_BYTES:
        raise BadRequest("The plan is too large to save (over 4 MB).")
    return data


@json_api
@require_POST
def save(request):
    me, denied = _need_lead(request, "labor.edit")
    if denied:
        return denied
    p = body(request)
    from .sync import apply_to_suite, merge
    data = validate_plan(p.get("data"))
    sent = json.dumps({k: v for k, v in data.items() if k != "suite"}, sort_keys=True, default=str)
    base = to_int(p.get("version"), "plan version")
    force = bool(p.get("force"))
    budget = _budget(p.get("budget"))
    with transaction.atomic():
        LaborPlan.load(budget)
        plan = LaborPlan.objects.select_for_update().get(pk=BUDGETS[budget])
        if plan.version != base and not force:
            return JsonResponse({"ok": False, "conflict": True, "version": plan.version, "updatedBy": plan.updated_by,
                                 "updatedAt": plan.updated_at.isoformat() if plan.updated_at else None,
                                 "error": f"{plan.updated_by or 'Someone'} changed the labor plan on another screen."}, status=409)
        if budget == "A":   # budget A starts new lines in the suite (it never changes existing ones)
            data, changes = apply_to_suite(data, old=plan.data if plan.version else None)
            if changes:
                from hmi.engine import Ctx, audit, refresh_schedule
                audit(Ctx(user=me), "Labor Budget: " + "; ".join(changes)[:900])
                refresh_schedule(f"{me.name} (Labor Budget)")
        data, _ = merge(data)   # speeds, colors and anything added in Admin come from the suite
        changed = json.dumps({k: v for k, v in data.items() if k != "suite"}, sort_keys=True, default=str) != sent
        plan.data, plan.version = data, plan.version + 1
        plan.updated_by, plan.updated_at = me.name[:120], timezone.now()
        plan.save()
        LaborPlanVersion.objects.create(budget=budget, version=plan.version, data=data, saved_by=plan.updated_by, saved_at=plan.updated_at)
        old = list(LaborPlanVersion.objects.filter(budget=budget).values_list("id", flat=True)[KEEP_VERSIONS:])
        if old:
            LaborPlanVersion.objects.filter(id__in=old).delete()
    PlantSettings.bump()  # the Scheduler uses the plan: let its screens refresh
    return JsonResponse({"ok": True, "version": plan.version, "updatedAt": plan.updated_at.isoformat(), "suiteChanged": changed})


@json_api
@require_GET
def history(request):
    me, denied = _need_lead(request)
    if denied:
        return denied
    rows = LaborPlanVersion.objects.filter(budget=_budget(request.GET.get("budget"))).values("id", "version", "saved_by", "saved_at")[:50]
    return JsonResponse({"ok": True, "items": [{"id": r["id"], "version": r["version"], "by": r["saved_by"],
                                                "at": r["saved_at"].isoformat()} for r in rows]})


@json_api
@require_POST
def restore(request):
    me, denied = _need_lead(request, "labor.edit")
    if denied:
        return denied
    vid = to_int(body(request).get("id"), "saved version")
    old = LaborPlanVersion.objects.filter(pk=vid).first()
    if not old:
        raise BadRequest("That saved version no longer exists.")
    from .sync import merge
    with transaction.atomic():
        LaborPlan.load(old.budget)
        plan = LaborPlan.objects.select_for_update().get(pk=BUDGETS[old.budget])
        plan.data, plan.version = merge(old.data)[0], plan.version + 1
        plan.updated_by, plan.updated_at = f"{me.name} (restored v{old.version})"[:120], timezone.now()
        plan.save()
        LaborPlanVersion.objects.create(budget=old.budget, version=plan.version, data=plan.data, saved_by=plan.updated_by, saved_at=plan.updated_at)
    PlantSettings.bump()
    return JsonResponse({"ok": True, "version": plan.version})
