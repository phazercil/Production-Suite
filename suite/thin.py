"""Server side of the thin-client screens.

Every screen is a Django page whose parts ("regions") re-load themselves from the server when something changes.
static/suite/thin.js asks /thin/watch every few seconds for two numbers:
  data   - goes up whenever plant records change (PlantSettings.version: readings, commands, imports);
  config - changes when settings that reshape a screen change (Grafana, kiosk layout, machines).
When one changes, the screen's regions fetch their new HTML (htmx). Actions post to the server, which answers
with the HTML to show and/or an HX-Trigger header: a message (toast), "refresh now", or "close the dialog".
"""
import hashlib
import json

from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_GET

from plant.models import Machine, PlantSettings
from plant.util import ms, now


def config_sig():
    st = PlantSettings.objects.filter(pk=1).values("settings", "config").first() or {}
    s = st.get("settings") or {}
    lines = list(Machine.objects.filter(stage="s1").order_by("sort", "id").values_list("name", "active", "color", "ppm", "feed_type"))
    raw = json.dumps([s.get("grafana"), s.get("kiosk"), lines], sort_keys=True, default=str)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


@require_GET
def watch(request):
    """Cheap: two small queries. Screens call it every few seconds."""
    v = PlantSettings.objects.filter(pk=1).values_list("version", flat=True).first() or 0
    r = JsonResponse({"data": v, "config": config_sig(), "now": ms(now())})
    r["Cache-Control"] = "no-store"
    return r


def trigger(resp, toast=None, kind=None, changed=False, close=False, **extra):
    """Add the screen instructions to a response: show a message, refresh now, close the dialog."""
    t = {}
    if toast:
        t["suite:toast"] = {"text": str(toast), "kind": kind or ""}
    if changed:
        t["suite:changed-now"] = True
    if close:
        t["suite:closeModal"] = True
    t.update(extra)
    if t:
        resp["HX-Trigger"] = json.dumps(t)
    return resp


def done(toast=None, kind=None, changed=True, close=False, html="", status=200):
    """The answer to an action that changes nothing on the page by itself (the regions refresh)."""
    resp = HttpResponse(html, status=status)
    if not html:
        resp["HX-Reswap"] = "none"
    return trigger(resp, toast=toast, kind=kind, changed=changed, close=close)


def refused(msg):
    """An action the server turned down: say why, change nothing on the page."""
    return done(toast=msg, kind="bad", changed=False)
