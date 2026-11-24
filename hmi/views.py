from plant.api import BadRequest, json_api, to_int
import hmac
import json
import time

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
    if u.role in Person.LEADS and timeout and timezone.now().timestamp() - last > timeout * 60:
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


PICK_NAME = "Pick your name, then enter your PIN."


def pin_login(request, pin, user_id=None, perm="", require_name=False):
    """Check a PIN and log the person in on this screen. Returns (person, None) or (None, why not).
    require_name: switching people (someone else is signed in, or taking over a line) needs the name picked as well."""
    if require_name and not user_id:
        return None, PICK_NAME
    u, err = check_pin(request, pin, user_id, perm)
    if not u:
        return None, err
    request.session.cycle_key()  # new session id at every login
    request.session["pid"] = u.id
    request.session.pop("sup", None)
    touch(request)
    return u, None


_LOCAL_FAILS = {}   # wrong PINs per address in this process: used when the shared cache can't be read or written


def _local_fails(ip_key, t, add=False):
    lst = [x for x in _LOCAL_FAILS.get(ip_key, []) if t - x < 60] + ([t] if add else [])
    if len(_LOCAL_FAILS) > 5000:
        _LOCAL_FAILS.clear()
    _LOCAL_FAILS[ip_key] = lst
    return lst


def check_pin(request, pin, user_id=None, perm=""):
    """Whose PIN this is (and that they have permission `perm`), without logging them in.
    Slows down PIN guessing: after 5 wrong PINs from one screen (or 20 from one device address), wait 60 seconds."""
    t = timezone.now().timestamp()
    ip_key = "pin_fails:" + (request.META.get("REMOTE_ADDR") or "?")
    fails = [x for x in request.session.get("pin_fails", []) if t - x < 60]
    try:
        ip_fails = [x for x in (cache.get(ip_key) or []) if t - x < 60]
    except Exception:  # the shared store is down: this process's own count still limits guessing
        log.exception("PIN limit store unavailable")
        ip_fails = _local_fails(ip_key, t)
    # 5 per screen; 20 per address, so screens behind one proxy address don't lock each other out over a few typos
    for lst, limit in ((fails, 5), (ip_fails, 20)):
        if len(lst) >= limit:
            log.warning("PIN attempts paused for %s", request.META.get("REMOTE_ADDR"))
            return None, f"Too many wrong PINs. Wait {int(60 - (t - lst[-limit])) + 1} seconds and try again."
    pin = str(pin or "").strip()
    qs = Person.objects.filter(active=True, pin=pin).exclude(pin="")
    if user_id:
        qs = qs.filter(pk=to_int(user_id, "person"))
    from plant import perms
    if perm is True:   # older screens asking for "a shift lead"
        perm = "section.admin"
    matches = [u for u in qs if u.pin == pin and (not perm or perms.can(u, perm))]  # exact PIN match even where the database ignores letter case
    u = matches[0] if pin and matches else None
    if not u:
        request.session["pin_fails"] = fails + [t]
        from plant import health
        health.count("pin")
        try:
            cache.set(ip_key, ip_fails + [t], 120)
        except Exception:   # kept in this process instead
            log.exception("PIN limit store unavailable")
            _local_fails(ip_key, t, add=True)
        return None, (f"That PIN can't do this ({perms.PERMS[perm]['label']})." if perm in perms.PERMS
                      else "That PIN does not match. Try again or ask your shift lead.")
    request.session["pin_fails"] = []
    return u, None


@require_GET
def people(request):
    """The names to pick from when switching person from the top bar (then the PIN is checked)."""
    from plant import perms
    roles = dict(perms.ROLES)
    return JsonResponse({"ok": True, "people": [{"id": u.pk, "name": u.name, "role": roles.get(u.role, u.role)}
                                                for u in Person.objects.filter(active=True).exclude(pin="").order_by("name")]})


@json_api
@require_POST
def login(request):
    p = body(request)
    from plant import perms
    perm = p.get("perm") if p.get("perm") in perms.PERMS else bool(p.get("adminOnly"))
    u, err = pin_login(request, p.get("pin"), p.get("userId"), perm, require_name=bool(p.get("switch")))
    if not u:
        return JsonResponse({"ok": False, "error": err})
    return state_response(request)


@json_api
@require_POST
def logout(request):
    request.session.pop("pid", None)
    request.session.pop("sup", None)
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


def _too_big(request, limit):
    try:
        n = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        n = 0
    return n > limit


def _rate_ok(key, per_minute):
    """At most per_minute requests a minute for this key (shared by every server process through the cache)."""
    k = f"rate:{key}:{int(time.time() // 60)}"
    try:
        cache.add(k, 0, 120)
        return cache.incr(k) <= per_minute
    except Exception:  # noqa: BLE001 - the cache being down never stops machine data
        return True


def _depth(x, d=0):
    if d > 12:
        return d
    if isinstance(x, dict):
        return max([_depth(v, d + 1) for v in x.values()] or [d + 1])
    if isinstance(x, list):
        return max([_depth(v, d + 1) for v in x] or [d + 1])
    return d


FEED_MAX_BYTES, INGEST_MAX_BYTES = 64 * 1024, 2 * 1024 * 1024   # (Django refuses bodies over 2.5 MB anyway)
FEED_PER_MIN, INGEST_PER_MIN = 240, 240


@json_api
@csrf_exempt
@require_POST
def feed_push(request, machine):
    """Machines (Node-RED, a PLC gateway) push {"ppm": 190, "count": 48211, "recipe": "MG-24", "run": 1} with header
    X-Feed-Key. ppm is required; the recipe and run-signal field names are set per machine in Admin > Machine data."""
    from plant import liveconfig
    liveconfig.refresh()
    key = settings.SUITE["FEED_KEY"]
    if not key or not hmac.compare_digest(str(request.headers.get("X-Feed-Key") or "").encode(), key.encode()):
        from plant import health
        health.count("feedkey")
        return JsonResponse({"ok": False, "error": "Wrong or missing X-Feed-Key (set it in Operator Kiosk > Admin > Connections)."}, status=403)
    if _too_big(request, FEED_MAX_BYTES):
        return JsonResponse({"ok": False, "error": "A machine reading can be at most 64 KB."}, status=413)
    m = Machine.objects.filter(name=machine).first()
    if not m:
        return JsonResponse({"ok": False, "error": "Unknown machine."}, status=404)
    if not _rate_ok(f"feed:{m.pk}", FEED_PER_MIN):
        return JsonResponse({"ok": False, "error": f"Too many readings: at most {FEED_PER_MIN} a minute per machine."}, status=429)
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
        from plant import health
        health.count("ingestkey")
        return JsonResponse({"ok": False, "error": "Wrong or missing X-Source-Key."}, status=403)
    if not src.enabled:
        return JsonResponse({"ok": False, "error": "This data source is turned off."}, status=409)
    if _too_big(request, INGEST_MAX_BYTES):
        return JsonResponse({"ok": False, "error": "Send at most 2 MB at a time (split the records into smaller batches)."}, status=413)
    if not _rate_ok(f"ingest:{src.pk}", INGEST_PER_MIN):
        return JsonResponse({"ok": False, "error": f"Too many requests: at most {INGEST_PER_MIN} a minute per data source."}, status=429)
    try:
        payload = json.loads(request.body or b"null")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "The body must be JSON: one object, or a list of objects."}, status=400)
    if not isinstance(payload, (dict, list)):
        return JsonResponse({"ok": False, "error": "The body must be JSON: one object, or a list of objects."}, status=400)
    if _depth(payload) > 12:
        return JsonResponse({"ok": False, "error": "The records are nested too deeply (at most 12 levels)."}, status=400)
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


@require_GET
def roll_photo(request, pk):
    """The picture of a roll tag kept with the roll (Roll tags, Quality › Traceability)."""
    from django.http import HttpResponse, HttpResponseNotFound

    from .models import RollTagPhoto
    thumb = request.GET.get("size") == "thumb"
    ph = RollTagPhoto.objects.filter(pk=pk).only("thumb" if thumb else "data").first()
    if not ph:
        return HttpResponseNotFound("No such picture.")
    r = HttpResponse(bytes(ph.thumb) if thumb and ph.thumb else bytes(ph.data), content_type="image/jpeg")
    r["Cache-Control"] = "private, max-age=86400"
    r["X-Content-Type-Options"] = "nosniff"
    r["Content-Disposition"] = f'inline; filename="roll-tag-{ph.pk}.jpg"'
    r["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"
    return r


@json_api
@require_POST
def update_upload(request):
    """A shift lead uploads an update file (Admin > Updates). It is checked, then waits in updates/ready."""
    from plant import updates
    me, _ = current_person(request)
    from plant import perms
    if not perms.can(me, "updates.install"):
        return JsonResponse({"ok": False, "error": "Log in as someone allowed to install updates."}, status=403)
    touch(request)
    f = request.FILES.get("file")
    if not f:
        return JsonResponse({"ok": False, "error": "Choose the update file."}, status=400)
    if f.size > 300 * 1024 * 1024:
        return JsonResponse({"ok": False, "error": "That file is too large to be an update file."}, status=400)
    import uuid
    tmp = updates.root() / "run" / f"upload-{uuid.uuid4().hex}.tmp"   # two uploads at once never share a file
    tmp.parent.mkdir(parents=True, exist_ok=True)
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
