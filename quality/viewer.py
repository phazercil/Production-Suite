"""Quality › 3D viewer: the Pleated Filter 3D Viewer in the suite, its part library kept in the suite's database.

The viewer page (/quality/viewer/app/) is the viewer as it was built, with three.js and SheetJS served from the suite
(the plant network has no internet) and a small stand-in for the storage and downloads it asked its host for
(static/quality/fv-host.js). Anyone on the plant network may open it to look; saving to the part library needs the
"Quality settings" permission. With ?view=1 it is view only (the Operator Kiosk's 3D Model page), and with ?part=
or ?line= it opens that filter, or the one running on that line (following it through changeovers).
"""
import hashlib
import json

from django.db.models import Max
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from hmi.views import current_person
from plant import perms
from plant.api import BadRequest, json_api
from plant.models import FilterModel3D

EDIT_PERM = "quality.tolerances"
MAX_DOC = 1_000_000   # bytes: a part's document (the viewer's settings, info sheet and inkjets) is a few kB


def _version():
    a = FilterModel3D.objects.aggregate(n=Max("updated"))
    return hashlib.sha1(f"{FilterModel3D.objects.count()}|{a['n']}".encode()).hexdigest()[:16]


@ensure_csrf_cookie
@require_GET
def app(request):
    me, _ = current_person(request)
    view = request.GET.get("view") == "1"
    resp = render(request, "quality/fv_app.html", {
        "canEdit": (not view) and perms.can(me, EDIT_PERM), "view": view,
        "part": str(request.GET.get("part") or "")[:60], "line": str(request.GET.get("line") or "")[:60],
        "who": me.name if me else ""})
    # The viewer is one page with its script inline (as it was built), so this page allows inline script. It still
    # can't load or send anything outside the suite (default-src / connect-src 'self'), and only the suite frames it.
    resp["Content-Security-Policy"] = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                                       "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; frame-src 'none'; "
                                       "frame-ancestors 'self'; form-action 'self'; base-uri 'self'; object-src 'none'; worker-src 'self' blob:")
    return resp


@json_api
@require_GET
def parts(request):
    """The whole library: {v, docs: [{id, data}]}; with ?v= the same version, just {same: true}."""
    v = _version()
    if request.GET.get("v") == v:
        return JsonResponse({"ok": True, "v": v, "same": True})
    return JsonResponse({"ok": True, "v": v, "docs": [{"id": x.pid, "data": x.data} for x in FilterModel3D.objects.all()]})


def _editor(request):
    me, _ = current_person(request)
    if not perms.can(me, EDIT_PERM):
        return None, JsonResponse({"ok": False, "error": "Saving to the 3D part library needs the Quality settings permission. "
                                                         "Log in on the Quality page first."}, status=403)
    return me, None


def _body(request):
    try:
        return json.loads(request.body or b"{}")
    except ValueError:
        raise BadRequest("That isn't JSON.")


@json_api
@require_POST
def part_set(request):
    me, denied = _editor(request)
    if denied:
        return denied
    p = _body(request)
    pid = str(p.get("id") or "").strip()[:120]
    data = p.get("data")
    if not pid or not isinstance(data, dict):
        raise BadRequest("A part needs its id and its settings.")
    if len(request.body) > MAX_DOC:
        raise BadRequest("That part is too big to save.")
    FilterModel3D.objects.update_or_create(pid=pid, defaults={"part": str(data.get("part") or pid)[:120], "data": data, "by": me.name})
    return JsonResponse({"ok": True, "v": _version()})


@json_api
@require_POST
def part_delete(request):
    me, denied = _editor(request)
    if denied:
        return denied
    pid = str(_body(request).get("id") or "")[:120]
    FilterModel3D.objects.filter(pid=pid).delete()
    return JsonResponse({"ok": True, "v": _version()})


@json_api
@require_GET
def running(request):
    """The filter a line is making now (or setting up for), for the kiosk's 3D Model page."""
    from hmi.models import ShiftRun
    from plant.models import MO
    sh = ShiftRun.objects.filter(line=str(request.GET.get("line") or ""), active=True).select_related("mo").first()
    mo = sh.mo if sh else None
    setting_up = False
    if not mo and sh and (sh.co or {}).get("newMo"):
        mo, setting_up = MO.objects.filter(pk=sh.co["newMo"]).first(), True
    return JsonResponse({"ok": True, "part": mo.part if mo else "", "mo": mo.floor_number if mo else "", "settingUp": setting_up})


# ---------------------------------------------------------------------------------------------------------------
# Updates from the asset database: Admin › Connector builder › "3D filter data" (one row per part). Each filter is
# kept here; what the database returns replaces the stored values when the filter is opened (at most every
# "re-read drawings" minutes) or on Refresh. What it doesn't return stays as stored.
# ---------------------------------------------------------------------------------------------------------------
DB_FIELDS = ("ph", "pc", "pp", "fl", "mw", "mt", "fr")   # the viewer's own setting names, in mm (pc: a count)


def db_ready():
    from hmi import integrations
    from hmi.models import SavedQuery
    return integrations.assets_ready() and SavedQuery.objects.filter(name="filter_3d").exists()


def _mm(v, uom):
    u = str(uom or "mm").strip().lower().rstrip(".")
    return v * 25.4 if u in ("in", "inch", "inches", '"', "″") else v * 10 if u == "cm" else v


def refresh_from_db(x, ctx=None, force=False):
    """Bring one stored filter (a FilterModel3D) up to date from the asset database. Returns a sentence, or None when
    there's nothing to do (not set up, or read recently)."""
    from django.core.cache import cache

    from hmi import engine
    if not db_ready():
        return None
    key = "fv-db:" + x.pid
    every = float((engine.settings_obj().settings or {}).get("assetsEvery", 1) or 0)   # Admin › Connections › Pulls every (0: every time)
    if not force and every > 0 and cache.get(key):
        return None
    o = engine._first_row("filter_3d", engine.part_key(x.part), ctx or engine.Ctx())
    if every > 0:
        cache.set(key, 1, int(every * 60))
    if not o:
        return f"The asset database has no 3D data for {x.part}."
    vals = {}
    for f in DB_FIELDS:
        n = engine._spec_number(o.get(f))
        if n is not None and n > 0:
            vals[f] = round(n if f == "pc" else _mm(n, o.get(f + "_uom") or o.get("uom")), 3)
    if not vals:
        return f"The asset database has no 3D dimensions for {x.part}."
    data = dict(x.data or {})
    cfg = dict(data.get("config") or {})
    P = dict(cfg.get("P") or {})
    P.update(vals)
    if "pc" in P and "pp" in P and "fl" not in vals:
        P["fl"] = round(P["pc"] * P["pp"], 2)          # the viewer keeps length = count × pitch
    elif "fl" in vals and "pc" in P and "pp" not in vals and P["pc"]:
        P["pp"] = round(vals["fl"] / P["pc"], 3)
    cfg["P"] = P
    data["config"] = cfg
    from django.utils import timezone
    data["fromDb"] = {"fields": sorted(vals), "t": int(timezone.now().timestamp() * 1000)}
    if data != x.data:
        x.data = data
        x.by = "Asset database"
        x.save(update_fields=["data", "by", "updated"])
    return f"{x.part}: {', '.join(sorted(vals))} from the asset database."


def find(part):
    """The stored filter for a part number: its own, or the one listing it as a customer / OE number."""
    from plant.models import part_key
    p = str(part or "").strip().upper()
    if not p:
        return None
    pid = __import__("re").sub(r"[^A-Z0-9_\-.~:@+]+", "-", p)[:120]
    x = FilterModel3D.objects.filter(pid=pid).first() or FilterModel3D.objects.filter(pid=part_key(p)).first()
    if x:
        return x
    for y in FilterModel3D.objects.all().only("pid", "part", "data"):
        custs = [c.strip().upper() for c in __import__("re").split(r"[,;\n]+", str(((y.data or {}).get("info") or {}).get("cust") or "")) if c.strip()]
        if p in custs or part_key(p) in custs:
            return y
    return None


@json_api
@require_POST
def refresh(request):
    """Before a filter opens: bring it up to date from the asset database (when that's set up)."""
    from hmi import engine
    p = _body(request)
    x = find(p.get("part"))
    if not x:
        return JsonResponse({"ok": True, "msg": ""})
    try:
        msg = refresh_from_db(x, engine.Ctx(line=str(p.get("line") or "")), force=bool(p.get("force")))
    except Exception as e:  # noqa: BLE001 - the stored filter still opens
        return JsonResponse({"ok": True, "msg": f"The asset database couldn't be read ({e}); showing the stored filter."})
    return JsonResponse({"ok": True, "msg": msg or "", "v": _version()})


@require_POST
def refresh_all(request):
    """Quality › 3D viewer › Refresh from the asset database: every stored filter, now."""
    from urllib.parse import urlencode

    from django.http import HttpResponseRedirect

    from hmi import engine
    me, _ = current_person(request)
    if not perms.can(me, EDIT_PERM):
        msg = "Refreshing the 3D library needs the Quality settings permission."
    elif not db_ready():
        msg = ("Nothing to refresh from: set up \"3D filter data\" in Admin › Connector builder (asset database) first. "
               "Until then each filter is kept as saved here.")
    else:
        n = bad = 0
        for x in FilterModel3D.objects.all():
            try:
                if refresh_from_db(x, engine.Ctx(user=me), force=True):
                    n += 1
            except Exception:  # noqa: BLE001
                bad += 1
        engine.audit(engine.Ctx(user=me), f"Refreshed the 3D filter library from the asset database ({n} read)")
        msg = f"Read {n} filter{'s' if n != 1 else ''} from the asset database." + (f" {bad} couldn't be read." if bad else "")
    return HttpResponseRedirect("/quality/?" + urlencode({"tab": "viewer", "msg": msg}))
