from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET

from hmi.views import current_person
from plant.api import json_api
from plant.models import PlantSettings

from . import engine, grafana


@ensure_csrf_cookie
def board(request):
    return render(request, "reports/board.html", {"mode": "reports"})


def _lead(request):
    me, _ = current_person(request)
    return bool(me and me.role == "admin")


@json_api
@require_GET
def report(request):
    today = timezone.localdate()
    scope = request.GET.get("scope") or "plant"
    period = request.GET.get("period") or f"month:{today.year}-{today.month:02d}"
    try:
        data = engine.report(scope, period, lead=_lead(request))
    except engine.BadPeriod as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    g = PlantSettings.load().settings.get("grafana") or {}
    data["grafana"] = {"ready": grafana.ready(), "panels": g.get("report") or [], "lineVar": g.get("lineVar") or "line"}
    data["lead"] = _lead(request)
    return JsonResponse({"ok": True, **data})


@json_api
@require_GET
def grafana_panels(request):
    if not _lead(request):
        return JsonResponse({"ok": False, "login": True, "error": "Log in as a shift lead to choose Grafana panels."}, status=403)
    try:
        return JsonResponse({"ok": True, "panels": [{"id": p["id"], "title": p["title"], "type": p["type"]} for p in grafana.panels()]})
    except grafana.GrafanaError as e:
        return JsonResponse({"ok": False, "error": str(e)})


@json_api
@require_GET
def grafana_data(request):
    """The chosen Grafana panels' data for the report's period, for one line (or the whole plant)."""
    g = PlantSettings.load().settings.get("grafana") or {}
    chosen = [int(x) for x in (g.get("report") or []) if str(x).isdigit()]
    if not chosen:
        return JsonResponse({"ok": True, "panels": []})
    try:
        per = engine.parse_period(request.GET.get("period") or "")
        sc = engine.parse_scope(request.GET.get("scope") or "plant")
    except engine.BadPeriod as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    variables = {g.get("lineVar") or "line": sc.get("line") or "All"}
    try:
        ps = {p["id"]: p for p in grafana.panels()}
    except grafana.GrafanaError as e:
        return JsonResponse({"ok": False, "error": str(e)})
    out = []
    for pid in chosen:
        p = ps.get(pid)
        if not p:
            out.append({"id": pid, "title": f"Panel {pid}", "error": "This panel is no longer on the dashboard."})
            continue
        try:
            out.append({"id": pid, "title": p["title"], "series": grafana.panel_data(p, per["start"].timestamp() * 1000,
                                                                                         min(per["end"], timezone.now()).timestamp() * 1000, variables)})
        except grafana.GrafanaError as e:
            out.append({"id": pid, "title": p["title"], "error": str(e)})
    return JsonResponse({"ok": True, "panels": out})
