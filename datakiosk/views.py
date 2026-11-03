"""Data Kiosk: the wall screen. Built on the server; the page only shows it and re-loads its parts when the
plant changes (see suite/thin.py)."""
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from hmi import snapshot
from plant.api import json_api
from plant.util import ms, now

from . import screen


def _bands(L):
    for b in L["bands"]:
        b["card"] = screen.line_card(b["name"])
    L["bands"] = [b for b in L["bands"] if b["card"]]
    return L


@require_GET
def board(request):
    return render(request, "datakiosk/board.html", {"mode": "data", "thin": True, "now_ms": ms(now()), "L": _bands(screen.layout())})


@require_GET
def region(request, name):
    """One part of the page, for the screen to swap in."""
    if name == "status":
        return render(request, "datakiosk/_status.html", {"L": screen.layout()})
    if name == "board":
        return render(request, "datakiosk/_board.html", {"L": _bands(screen.layout())})
    if name in ("live", "chart"):
        c = screen.line_card(request.GET.get("line", ""))
        if not c:
            raise Http404("No such line")
        return render(request, f"datakiosk/_{name}.html", {"c": c})
    raise Http404("No such part")


@json_api
@require_GET
def state(request):
    """Light snapshot for other systems: lines, live runs, Grafana settings. No PINs, no history tables."""
    return JsonResponse({"ok": True, "snap": snapshot.build(light=True), "server": snapshot.server_info()})
