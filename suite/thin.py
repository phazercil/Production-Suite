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


SSE_SECONDS, SSE_KEEPALIVE = 50, 15


def sse_on():
    return bool((PlantSettings.load().settings or {}).get("liveUpdates"))


def events(request):
    """Live updates without polling (Admin › Network › Live updates, off by default): the same answer as watch, sent
    as soon as it changes. Each stream lasts at most 50 seconds and the screen reconnects, so a server worker is never
    held for long; screens fall back to asking every few seconds when this is off or the connection drops.
    Every open screen holds one web server thread while connected (waitress WEB_THREADS, default 12; under Apache
    the suite's own web service's threads), so switch it on only with fewer screens than that, or raise WEB_THREADS."""
    import time

    from django.db import close_old_connections
    from django.http import HttpResponseNotFound, StreamingHttpResponse
    if not sse_on():
        return HttpResponseNotFound("Live updates are off.")

    def stream():
        last, t0, sent = None, time.monotonic(), 0.0
        try:
            while time.monotonic() - t0 < SSE_SECONDS:
                v = PlantSettings.objects.filter(pk=1).values_list("version", flat=True).first() or 0
                cur = (v, config_sig())
                if cur != last:
                    last = cur
                    sent = time.monotonic()
                    yield f"data: {json.dumps({'data': v, 'config': cur[1], 'now': ms(now())})}\n\n"
                elif time.monotonic() - sent > SSE_KEEPALIVE:
                    sent = time.monotonic()
                    yield f"data: {json.dumps({'data': v, 'config': cur[1], 'now': ms(now())})}\n\n"   # also keeps the clock right
                close_old_connections()
                time.sleep(1)
            yield "retry: 1000\n\n"
        finally:
            close_old_connections()
    r = StreamingHttpResponse(stream(), content_type="text/event-stream")
    r["Cache-Control"] = "no-store"
    r["X-Accel-Buffering"] = "no"
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
        try:   # added to what the response already asks for, never in place of it
            had = json.loads(resp["HX-Trigger"]) if resp.has_header("HX-Trigger") else {}
        except ValueError:
            had = {}
        resp["HX-Trigger"] = json.dumps({**had, **t})
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
