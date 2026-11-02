from plant.api import BadRequest, json_api, to_int
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from hmi import snapshot


def board(request):
    return render(request, "datakiosk/board.html", {"mode": "data"})


@json_api
@require_GET
def state(request):
    """Light snapshot for the wall screen: lines, live runs, Grafana settings. No PINs, no history tables."""
    return JsonResponse({"ok": True, "snap": snapshot.build(light=True), "server": snapshot.server_info()})
