"""Data Kiosk: the wall screen. Built on the server; the page only shows it and re-loads its parts when the
plant changes (see suite/thin.py).

/data/ shows every pleating line; /data/<machine>/ shows one. Either with ?view=1 is the view-only screen: no menu
bar, no links into the rest of the suite, nothing to press, for a TV or a link sent to someone on the plant network.
"""
from urllib.parse import quote

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


def _view_only(request):
    return request.GET.get("view", "") in ("1", "only", "true")


def _q(focus, view):
    """The query the page's parts reload with, so they keep the same machine and mode."""
    q = []
    if focus:
        q.append("line=" + quote(focus.name))
    if view:
        q.append("view=1")
    return "&".join(q)


@require_GET
def board(request, line=None):
    focus = None
    if line is not None:
        focus = screen.machine_named(line)
        if not focus:
            raise Http404("No pleating line by that name")
    view = _view_only(request)
    path = f"/data/{quote(focus.name)}/" if focus else "/data/"
    ctx = {"mode": "data", "thin": True, "now_ms": ms(now()), "L": _bands(screen.layout(focus)), "view": view, "q": _q(focus, view),
           "share": request.build_absolute_uri(path + "?view=1")}
    if view:
        ctx["kiosk_lock"] = True   # no menu bar
    return render(request, "datakiosk/board.html", ctx)


@require_GET
def region(request, name):
    """One part of the page, for the screen to swap in."""
    focus = screen.machine_named(request.GET["line"]) if request.GET.get("line") else None
    view = _view_only(request)
    if name == "status":
        return render(request, "datakiosk/_status.html", {"L": screen.layout(focus), "view": view, "q": _q(focus, view)})
    if name == "board":
        return render(request, "datakiosk/_board.html", {"L": _bands(screen.layout(focus)), "view": view, "q": _q(focus, view)})
    if name in ("live", "chart"):
        c = screen.line_card(request.GET.get("line", ""))
        if not c:
            raise Http404("No such line")
        return render(request, f"datakiosk/_{name}.html", {"c": c, "view": view})
    raise Http404("No such part")


@json_api
@require_GET
def state(request):
    """Light snapshot for other systems: lines, live runs, Grafana settings. No PINs, no history tables."""
    return JsonResponse({"ok": True, "snap": snapshot.build(light=True), "server": snapshot.server_info()})
