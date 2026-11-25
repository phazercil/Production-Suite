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
from django.utils import timezone
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
    return JsonResponse({"ok": True, "v": v, "docs": [{"id": x.pid, "data": x.data, "flat": bool(x.flat)} for x in FilterModel3D.objects.all()]})


def clean_flat(f):
    """A flat row from the viewer, kept to the batch sheet's columns (text, at most 200 characters each)."""
    from hmi.snapshot import fv_columns
    names = {c["db"] for c in fv_columns()}
    if not isinstance(f, dict):
        return {}
    return {k: (v if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)[:200])
            for k, v in f.items() if k in names and v is not None and str(v).strip() != ""}


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
    d = {"part": str(data.get("part") or pid)[:120], "data": data, "by": me.name}
    if "flat" in p:
        d["flat"] = clean_flat(p.get("flat"))
    FilterModel3D.objects.update_or_create(pid=pid, defaults=d)
    return JsonResponse({"ok": True, "v": _version()})


@json_api
@require_POST
def part_flat(request):
    """Fill in the flat rows of parts saved before the viewer sent them ({rows: {id: row}}); the settings stay as they are."""
    me, denied = _editor(request)
    if denied:
        return denied
    rows = _body(request).get("rows")
    if not isinstance(rows, dict):
        raise BadRequest("Send the rows.")
    n = 0
    for pid, f in list(rows.items())[:2000]:
        n += FilterModel3D.objects.filter(pid=str(pid)[:120]).update(flat=clean_flat(f), updated=timezone.now())
    return JsonResponse({"ok": True, "n": n})


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
def db_ready():
    from hmi import integrations
    from hmi.models import SavedQuery
    return integrations.assets_ready() and SavedQuery.objects.filter(name="filter_3d").exists()


def refresh_from_db(x, ctx=None, force=False):
    """Read one stored filter's row from the asset database ("3D filter data") and keep it with the filter: the
    viewer lays it over the saved settings when it opens the filter (its own batch-sheet rules: mm unless the unit
    column says inches, Side/End B copying A, band heights to offsets). Columns it doesn't return stay as saved.
    Returns a sentence, or None when there's nothing to do (not set up, or read recently)."""
    from django.core.cache import cache
    from django.utils import timezone

    from hmi import engine
    from hmi.snapshot import fv_columns
    if not db_ready():
        return None
    key = "fv-db:" + x.pid
    every = float((engine.settings_obj().settings or {}).get("assetsEvery", 1) or 0)   # Admin › Connections › Pulls every (0: every time)
    if not force and every > 0 and cache.get(key):
        return None
    o = engine._first_row("filter_3d", engine.part_key(x.part), ctx or engine.Ctx())
    if every > 0:
        cache.set(key, 1, int(every * 60))
    known = {c["db"] for c in fv_columns()} | {"uom"}
    row = {k: (v if isinstance(v, (int, float)) else str(v)[:500]) for k, v in (o or {}).items()
           if k in known and v is not None and str(v).strip() != ""}
    data = dict(x.data or {})
    if not [k for k in row if k != "uom"]:
        if data.pop("db", None) is not None:
            x.data = data
            x.save(update_fields=["data", "updated"])
        return f"The asset database has no 3D data for {x.part}."
    db = {"row": row, "t": int(timezone.now().timestamp() * 1000)}
    if (data.get("db") or {}).get("row") != row:
        data["db"] = db
        x.data = data
        x.save(update_fields=["data", "updated"])
    n = len([k for k in row if k != "uom"])
    return f"{x.part}: {n} setting{'s' if n != 1 else ''} from the asset database."


def _n(v):
    if v is None or isinstance(v, bool) or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def dims(x):
    """A library filter's size in mm, worked out as the viewer works it out (dimsOf in fv_app.html): pack length
    (pleats × pitch), overall length (plus end bands and foam), width (media width plus side bands and foam),
    height, pleat height, pleats and side band height. A value is None when anything it's built from wasn't
    entered: missing from the filter, or still flagged in the viewer's "needs review" list (a default, not a
    real value). Only the filter as saved in the library is used, nothing from outside the suite."""
    data = x.data or {}
    cfg = data.get("config") or {}
    P = cfg.get("P") or {}
    ui = cfg.get("ui") or {}
    needs = set(data.get("needs") or [])
    side_on, end_on = ui.get("side_c_on") is not False, bool(ui.get("end_c_on"))

    def bands(g):
        bb = list(((cfg.get("groups") or {}).get(g) or {}).get("bands") or [])[:2]
        return bb + [{}] * (2 - len(bb))
    SB, EB = bands("side"), bands("end")
    foams = [f for f in (cfg.get("foams") or []) if isinstance(f, dict)]
    foam_ok = not any(k.startswith("foam.") and k.endswith(".t") for k in needs)

    def fT(w):
        ts = [(_n(f.get("t")) or 0) for f in foams if f.get("where") in ("all", w)]
        return max([0] + ts)

    def ok(*keys):
        return not (needs & set(keys))

    def val(d, k, key):
        return _n(d.get(k)) if ok(key) else None

    pc, pp, mw, ph = val(P, "pc", "P.pc"), val(P, "pp", "P.pp"), val(P, "mw", "P.mw"), val(P, "ph", "P.ph")
    out = {"pleats": pc, "ph": ph, "len": None, "olen": None, "wid": None, "hgt": None,
           "sb": val(SB[0], "sb", "band.side.0.sb") if side_on else None, "side_on": side_on, "end_on": end_on}
    if pc is not None and pp is not None:
        L = pc * pp
        out["len"] = L
        ebt = [val(EB[i], "bt", f"band.end.{i}.bt") for i in (0, 1)] if end_on else [0, 0]
        if None not in ebt and foam_ok:
            out["olen"] = L + sum(ebt) + fT("end0") + fT("end1")
    if mw is not None:
        sbt = [val(SB[i], "bt", f"band.side.{i}.bt") for i in (0, 1)] if side_on else [0, 0]
        if None not in sbt and foam_ok:
            out["wid"] = mw + sum(sbt) + fT("side0") + fT("side1")
    if ph is not None:
        act = [(g, i, b) for g, bb, on in (("side", SB, side_on), ("end", EB, end_on)) if on for i, b in enumerate(bb)]
        offs = [(val(b, "ot", f"band.{g}.{i}.ot"), val(b, "ob", f"band.{g}.{i}.ob")) for g, i, b in act]
        if all(o is not None for pair in offs for o in pair):
            out["hgt"] = max([ph] + [ph + ot for ot, _ in offs]) - min([0] + [-ob for _, ob in offs])
    return out


_TOL_NUM = r"(\d+(?:\.\d+)?|\.\d+)"


def parse_tol(text):
    """A tolerance as typed on the filter information sheet, as (under, over) in mm, or None if it can't be read:
    "+1 / -2 mm" → (2, 1), "±0.5 mm" (or "+/-0.5", "+-0.5") → (0.5, 0.5), "1 mm" → (1, 1), "+0.04 / -0.06 in"
    → in mm. A number with no unit is mm, as everything on the sheet is."""
    import re
    s = str(text or "").strip().lower().replace("−", "-").replace("–", "-").replace(",", ".")
    if not s:
        return None
    units = set(re.findall(r'mm|cm|inches|inch|in\b|"', s))
    units = {"in" if u in ("inches", "inch", '"') else u for u in units}
    if len(units) > 1:   # mm and inches in one tolerance: can't tell which is meant
        return None
    unit = {"in": 25.4, "cm": 10.0}.get(next(iter(units), "mm"), 1.0)
    body = re.sub(r'mm|cm|inches|inch|in\b|"', "", s).strip()
    m = re.fullmatch(rf"(?:±|\+\s*/\s*-|\+-|-\s*/\s*\+)\s*{_TOL_NUM}", body)
    if m:
        v = float(m.group(1)) * unit
        return v, v
    m = re.fullmatch(_TOL_NUM, body)
    if m:
        v = float(m.group(1)) * unit
        return v, v
    plus = re.search(rf"\+\s*{_TOL_NUM}", body)
    minus = re.search(rf"-\s*{_TOL_NUM}", body)
    rest = re.sub(rf"[+-]\s*{_TOL_NUM}|/|\s", "", body)
    if plus and minus and not rest:
        return float(minus.group(1)) * unit, float(plus.group(1)) * unit
    return None


SHEET_TOLS = {"len": "tol_len", "wid": "tol_wid", "ph": "tol_ph"}


def sheet_tols(x):
    """The filter's tolerances from its information sheet: {spec: {"minus", "plus"}} in inches, for each one that's
    filled in and readable (the others fall back to Quality › Tolerances)."""
    info = (x.data or {}).get("info") or {}
    out = {}
    for spec, k in SHEET_TOLS.items():
        t = parse_tol(info.get(k))
        if t:
            out[spec] = {"minus": round(t[0] / 25.4, 5), "plus": round(t[1] / 25.4, 5)}
    return out


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
