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

from .models import LaborPlan, LaborPlanVersion

PAGE = Path(__file__).resolve().parent / "page" / "labor.html"
MAX_BYTES = 4 * 1024 * 1024
KEEP_VERSIONS = 200
_page_cache = {}


def _lead(request):
    me, note = current_person(request)
    if me and me.is_lead:
        return me, None
    return None, note


def _json_for_html(obj):
    """JSON that can sit inside <script> without ending it early."""
    return json.dumps(obj).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _suitebar(me):
    return ('<header class="suitebar no-print"><a class="suitebrand" href="/" aria-label="Production Suite home">'
            '<svg viewBox="0 0 34 20" aria-hidden="true"><polyline points="1,17 5,3 9,17 13,3 17,17 21,3 25,17 29,3 33,17" fill="none" '
            'stroke="currentColor" stroke-width="2.4" stroke-linejoin="round"/></svg><span>Production Suite</span></a>'
            '<nav class="suitetabs" aria-label="Modes"><a href="/operator/">Operator Kiosk</a><a href="/scheduler/">Scheduler</a>'
            '<a href="/data/">Data Kiosk</a><a href="/reports/">Reports</a><a href="/labor/" aria-current="page">Labor Budget</a></nav>'
            '<span class="suitesync"><span id="labor-sync" class="suitesync-dot" aria-live="polite">Saved</span>'
            f'<span class="suitewho">{_esc(me.name)}</span><button type="button" id="labor-logout" class="suitebtn">Log out</button></span></header>'
            f'<div class="suite-rev" style="position:fixed;right:6px;bottom:3px;z-index:2147483000;font:500 10px/1.2 ui-monospace,Consolas,monospace;'
            f'color:#64748b;opacity:.6;pointer-events:none">v{_esc(settings.SUITE["VERSION"])} · rev {_esc(settings.SUITE["REVISION"])}</div>')


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


@ensure_csrf_cookie
def page(request):
    me, note = _lead(request)
    if not me:
        return render(request, "labor/login.html", {"mode": "labor", "note": note})
    touch(request)
    html = _page_cache.get("html")
    if html is None:
        html = PAGE.read_text(encoding="utf-8")
        _page_cache["html"] = html
    plan = LaborPlan.load()
    boot = {"version": plan.version, "data": plan.data if plan.version else None, "user": me.name,
            "updatedBy": plan.updated_by, "updatedAt": plan.updated_at.isoformat() if plan.updated_at else None,
            "timeoutMin": PlantSettings.load().config["rules"].get("adminTimeout", 10)}
    out = (html.replace("__LABOR_CSS__", static("labor/labor.css"))
               .replace("__SUITEBAR_CSS__", static("labor/suitebar.css"))
               .replace("__LABOR_SYNC_JS__", static("labor/sync.js"))
               .replace("__SUITEBAR__", _suitebar(me))
               .replace("__LABOR_BOOT__", _json_for_html(boot)))
    resp = HttpResponse(out, content_type="text/html; charset=utf-8")
    # The calculator wires its buttons with inline handlers, so this one page allows inline script.
    # It still can't load or send anything outside the suite (default-src/connect-src 'self').
    resp["Content-Security-Policy"] = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                                       "img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; frame-src 'none'; "
                                       "frame-ancestors 'self'; form-action 'self'; base-uri 'self'; object-src 'none'")
    resp["Cache-Control"] = "no-store"
    return resp


def _need_lead(request):
    me, note = _lead(request)
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
    plan = LaborPlan.load()
    return JsonResponse({"ok": True, "version": plan.version, "data": plan.data if plan.version else None,
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
    me, denied = _need_lead(request)
    if denied:
        return denied
    p = body(request)
    data = validate_plan(p.get("data"))
    base = to_int(p.get("version"), "plan version")
    force = bool(p.get("force"))
    with transaction.atomic():
        LaborPlan.load()
        plan = LaborPlan.objects.select_for_update().get(pk=1)
        if plan.version != base and not force:
            return JsonResponse({"ok": False, "conflict": True, "version": plan.version, "updatedBy": plan.updated_by,
                                 "updatedAt": plan.updated_at.isoformat() if plan.updated_at else None,
                                 "error": f"{plan.updated_by or 'Someone'} changed the labor plan on another screen."}, status=409)
        plan.data, plan.version = data, plan.version + 1
        plan.updated_by, plan.updated_at = me.name[:120], timezone.now()
        plan.save()
        LaborPlanVersion.objects.create(version=plan.version, data=data, saved_by=plan.updated_by, saved_at=plan.updated_at)
        old = list(LaborPlanVersion.objects.values_list("id", flat=True)[KEEP_VERSIONS:])
        if old:
            LaborPlanVersion.objects.filter(id__in=old).delete()
    PlantSettings.bump()  # the Scheduler uses the plan: let its screens refresh
    return JsonResponse({"ok": True, "version": plan.version, "updatedAt": plan.updated_at.isoformat()})


@json_api
@require_GET
def history(request):
    me, denied = _need_lead(request)
    if denied:
        return denied
    rows = LaborPlanVersion.objects.values("id", "version", "saved_by", "saved_at")[:50]
    return JsonResponse({"ok": True, "items": [{"id": r["id"], "version": r["version"], "by": r["saved_by"],
                                                "at": r["saved_at"].isoformat()} for r in rows]})


@json_api
@require_POST
def restore(request):
    me, denied = _need_lead(request)
    if denied:
        return denied
    vid = to_int(body(request).get("id"), "saved version")
    old = LaborPlanVersion.objects.filter(pk=vid).first()
    if not old:
        raise BadRequest("That saved version no longer exists.")
    with transaction.atomic():
        plan = LaborPlan.objects.select_for_update().get(pk=1)
        plan.data, plan.version = old.data, plan.version + 1
        plan.updated_by, plan.updated_at = f"{me.name} (restored v{old.version})"[:120], timezone.now()
        plan.save()
        LaborPlanVersion.objects.create(version=plan.version, data=old.data, saved_by=plan.updated_by, saved_at=plan.updated_at)
    PlantSettings.bump()
    return JsonResponse({"ok": True, "version": plan.version})
