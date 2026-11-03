from plant.api import BadRequest, json_api, to_int
import json

from django.conf import settings
from django.core.cache import cache
import logging
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from plant.models import Machine, Person, PlantSettings
from plant.util import now

from . import engine, snapshot
from .models import LineState

log = logging.getLogger("plant")


def body(request):
    """The JSON object a screen sent; anything else (bad JSON, a list, a number) counts as empty."""
    try:
        data = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def current_person(request):
    """The person logged in on this screen. Shift leads drop out of admin mode after the idle time in Rules."""
    pid = request.session.get("pid")
    if not pid:
        return None, None
    u = Person.objects.filter(pk=pid, active=True).first()
    if not u:
        request.session.pop("pid", None)
        return None, None
    note = None
    timeout = PlantSettings.load().config["rules"].get("adminTimeout", 10)
    last = request.session.get("last_act", 0)
    if u.role == "admin" and timeout and timezone.now().timestamp() - last > timeout * 60:
        request.session.pop("pid", None)
        return None, f"Shift lead logged out after {timeout} min idle"
    return u, note


def touch(request):
    request.session["last_act"] = timezone.now().timestamp()


def state_response(request, extra=None, note=None):
    me, timeout_note = current_person(request)
    station = request.GET.get("line") or (extra or {}).pop("line", None)
    snap = snapshot.build(me, station)
    out = {"ok": True, "me": {"id": me.id, "name": me.name, "role": me.role, "trained": list(me.trained.values_list("name", flat=True))} if me else None,
           "server": snapshot.server_info(), "snap": snap, "note": note or timeout_note,
           "clientIp": request.META.get("REMOTE_ADDR", "")}
    if extra:
        out.update(extra)
    return JsonResponse(out)


@json_api
@require_GET
def state(request):
    since = request.GET.get("v")
    st = PlantSettings.objects.filter(pk=1).values_list("version", flat=True).first()
    if since and st is not None and str(st) == since and request.GET.get("force") != "1":
        me, note = current_person(request)
        if not note:
            return JsonResponse({"ok": True, "same": True})
    return state_response(request)


def pin_login(request, pin, user_id=None, admin_only=False):
    """Check a PIN and log the person in on this screen. Returns (person, None) or (None, why not).
    Slows down PIN guessing: after 5 wrong PINs from one screen (or 20 from one device address), wait 60 seconds."""
    t = timezone.now().timestamp()
    ip_key = "pin_fails:" + (request.META.get("REMOTE_ADDR") or "?")
    fails = [x for x in request.session.get("pin_fails", []) if t - x < 60]
    try:
        ip_fails = [x for x in (cache.get(ip_key) or []) if t - x < 60]
    except Exception:  # the limit is a safeguard; never let it block a login
        log.exception("PIN limit store unavailable")
        ip_fails = []
    # 5 per screen; 20 per address, so screens behind one proxy address don't lock each other out over a few typos
    for lst, limit in ((fails, 5), (ip_fails, 20)):
        if len(lst) >= limit:
            log.warning("PIN attempts paused for %s", request.META.get("REMOTE_ADDR"))
            return None, f"Too many wrong PINs. Wait {int(60 - (t - lst[-limit])) + 1} seconds and try again."
    pin = str(pin or "").strip()
    qs = Person.objects.filter(active=True, pin=pin).exclude(pin="")
    if user_id:
        qs = qs.filter(pk=to_int(user_id, "person"))
    if admin_only:
        qs = qs.filter(role="admin")
    matches = [u for u in qs if u.pin == pin]  # exact match even where the database ignores letter case
    u = matches[0] if pin and matches else None
    if not u:
        request.session["pin_fails"] = fails + [t]
        try:
            cache.set(ip_key, ip_fails + [t], 120)
        except Exception:
            log.exception("PIN limit store unavailable")
        return None, ("That is not a shift lead PIN." if admin_only else "That PIN does not match. Try again or ask your shift lead.")
    request.session.cycle_key()  # new session id at every login
    request.session["pid"] = u.id
    request.session["pin_fails"] = []
    touch(request)
    return u, None


@json_api
@require_POST
def login(request):
    p = body(request)
    u, err = pin_login(request, p.get("pin"), p.get("userId"), bool(p.get("adminOnly")))
    if not u:
        return JsonResponse({"ok": False, "error": err})
    return state_response(request)


@json_api
@require_POST
def logout(request):
    request.session.pop("pid", None)
    return state_response(request)


@json_api
@require_POST
def ping(request):
    touch(request)
    return JsonResponse({"ok": True})


@json_api
@require_POST
def command(request):
    p = body(request)
    me, note = current_person(request)
    touch(request)
    params = p.get("p") if isinstance(p.get("p"), dict) else {}
    line = p.get("line") if isinstance(p.get("line"), str) else None
    r = engine.execute(str(p.get("name") or ""), params, engine.Ctx(user=me, line=line, client_ip=request.META.get("REMOTE_ADDR", "")))
    if r.pop("relogin", False):
        request.session.pop("pid", None)
    return state_response(request, extra=r, note=note) if r.get("ok") else JsonResponse({**r, "note": note})


@json_api
@csrf_exempt
@require_POST
def feed_push(request, machine):
    """Machines (Node-RED, a PLC gateway) push {"ppm": 190, "count": 48211, "recipe": "MG-24", "run": 1} with header
    X-Feed-Key. ppm is required; the recipe and run-signal field names are set per machine in Admin > Machine data."""
    from plant import liveconfig
    liveconfig.refresh()
    key = settings.SUITE["FEED_KEY"]
    if not key or request.headers.get("X-Feed-Key") != key:
        return JsonResponse({"ok": False, "error": "Wrong or missing X-Feed-Key (set it in Operator Kiosk > Admin > Connections)."}, status=403)
    m = Machine.objects.filter(name=machine).first()
    if not m:
        return JsonResponse({"ok": False, "error": "Unknown machine."}, status=404)
    p = body(request)
    if not isinstance(p, dict) or engine.to_number(p.get("ppm")) is None or (p.get("count") not in (None, "") and engine.to_number(p.get("count")) is None):
        return JsonResponse({"ok": False, "error": 'Send {"ppm": number} and optionally "count": number.'}, status=400)
    ls, _ = LineState.objects.get_or_create(machine=m)
    keep = {k: v for k, v in list(p.items())[:40] if isinstance(k, str) and len(k) <= 80
            and (v is None or isinstance(v, (bool, int, float)) or (isinstance(v, str) and len(v) <= 200) or isinstance(v, dict))}
    if len(json.dumps(keep, default=str)) > 8000:
        keep = {k: v for k, v in keep.items() if not isinstance(v, dict)}
    ls.pushed, ls.pushed_at = {**keep, "ppm": p.get("ppm"), "count": p.get("count")}, now()
    ls.save(update_fields=["pushed", "pushed_at"])
    return JsonResponse({"ok": True})


@json_api
@csrf_exempt
@require_POST
def ingest(request, name):
    """Other systems (for example machine vision QC) send data here. See hmi/inbound.py."""
    from . import inbound
    from .models import DataSource
    src = DataSource.objects.filter(name__iexact=name).first()
    if not src or src.mode != "push":
        return JsonResponse({"ok": False, "error": f"No data source named {name[:60]} takes pushed data. Set it up in Operator Kiosk > Admin > Connections."}, status=404)
    if not inbound.key_ok(src, request.headers.get("X-Source-Key")):
        return JsonResponse({"ok": False, "error": "Wrong or missing X-Source-Key."}, status=403)
    if not src.enabled:
        return JsonResponse({"ok": False, "error": "This data source is turned off."}, status=409)
    try:
        payload = json.loads(request.body or b"null")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "The body must be JSON: one object, or a list of objects."}, status=400)
    if not isinstance(payload, (dict, list)):
        return JsonResponse({"ok": False, "error": "The body must be JSON: one object, or a list of objects."}, status=400)
    try:
        r = inbound.ingest(src, payload)
    except inbound.InboundError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    if r["stored"]:
        try:  # screens refresh their vision QC summary at most every 30 seconds, however often data arrives
            if cache.add("ingest_bump", 1, 30):
                PlantSettings.bump()
        except Exception:
            pass
    return JsonResponse({"ok": True, **r})


def version(request):
    """Which version is running. Used by the updater to check the suite came back, and by support."""
    return JsonResponse({"version": settings.SUITE["VERSION"], "revision": settings.SUITE["REVISION"]})


@json_api
@require_POST
def ticket_photo_upload(request):
    """A picture of what is wrong, for a maintenance ticket. Anyone logged in on the screen can add one."""
    from django.db import transaction

    from . import photos
    from .models import Ticket
    me, _ = current_person(request)
    if not me:
        return JsonResponse({"ok": False, "error": "Log in to add pictures."}, status=403)
    touch(request)
    f = request.FILES.get("photo")
    if not f:
        return JsonResponse({"ok": False, "error": "Take or choose a picture."}, status=400)
    if f.size > photos.MAX_BYTES:
        return JsonResponse({"ok": False, "error": "That picture is too large (over 25 MB)."}, status=400)
    with transaction.atomic():
        t = Ticket.objects.select_for_update().filter(pk=to_int(request.POST.get("ticket"), "ticket")).first()
        if not t:
            return JsonResponse({"ok": False, "error": "That ticket is no longer on file."}, status=404)
        try:
            ph = photos.add(t, f.read(), me.name, now())
        except photos.PhotoError as e:
            return JsonResponse({"ok": False, "error": str(e)}, status=400)
        engine.event("maintenance_photo", {"id": t.id, "photo": ph.id, "machine": t.machine, "by": me.name})
    PlantSettings.bump()
    return JsonResponse({"ok": True, "id": ph.id})


@require_GET
def ticket_photo(request, pk):
    """One ticket picture, for the screens on the plant network (like the tickets themselves)."""
    from django.http import HttpResponse, HttpResponseNotFound

    from .models import TicketPhoto
    thumb = request.GET.get("size") == "thumb"
    ph = TicketPhoto.objects.filter(pk=pk).only("thumb" if thumb else "data", "content_type").first()
    if not ph:
        return HttpResponseNotFound("No such picture.")
    data = bytes(ph.thumb) if thumb and ph.thumb else bytes(ph.data)
    r = HttpResponse(data, content_type=ph.content_type if ph.content_type in ("image/jpeg", "image/png") else "application/octet-stream")
    r["Cache-Control"] = "private, max-age=86400"
    r["X-Content-Type-Options"] = "nosniff"
    r["Content-Disposition"] = f'inline; filename="ticket-photo-{ph.pk}.{"png" if ph.content_type == "image/png" else "jpg"}"'
    r["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"
    return r


@require_POST
def update_upload(request):
    """A shift lead uploads an update file (Admin > Updates). It is checked, then waits in updates/ready."""
    from plant import updates
    me, _ = current_person(request)
    if not me or me.role != "admin":
        return JsonResponse({"ok": False, "error": "Log in as a shift lead to install updates."}, status=403)
    touch(request)
    f = request.FILES.get("file")
    if not f:
        return JsonResponse({"ok": False, "error": "Choose the update file."}, status=400)
    if f.size > 300 * 1024 * 1024:
        return JsonResponse({"ok": False, "error": "That file is too large to be an update file."}, status=400)
    tmp = updates.root() / "run" / "upload.tmp"
    with open(tmp, "wb") as out:
        for chunk in f.chunks():
            out.write(chunk)
    try:
        m = updates.receive(tmp, f.name, me.name, request.POST.get("when") or "now", request.POST.get("at") or "")
    except updates.UpdError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    engine.audit(engine.Ctx(user=me), f"Update v{m['version']} rev {m['revision']} uploaded ({f.name})")
    return JsonResponse({"ok": True, "data": updates.info(), "msg": f"v{m['version']} rev {m['revision']} checked and ready."})
