"""Cameras (/cameras/): set up the camera connections and pick the machines whose Operator Kiosk shows each one
(Operator Kiosk › Cameras). GET /api/cameras/<id>/frame is the latest picture for the screens. Setting up needs
"Cameras" (Admin › Permissions); watching needs nothing but being on the plant network, like the kiosks."""
import time
from urllib.parse import urlencode

from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from hmi import engine
from hmi.views import current_person, touch
from plant import liveconfig, perms
from plant.models import Machine

from . import grab
from .models import Camera

PERM = "cameras.edit"


def _gate(request):
    me, note = current_person(request)
    if perms.can(me, PERM):
        touch(request)
        return me, None
    msg = note or (f"{me.name} doesn't have permission to set up cameras (Admin › Permissions › Cameras)." if me else None)
    return None, render(request, "supervisor/login.html", {"mode": "admin", "note": msg, "perm": PERM, "title": "Cameras",
                                                           "blurb": "The plant's cameras and the machines whose Operator Kiosk shows them."})


def _audit(me, what):
    engine.audit(engine.Ctx(user=me), what)


def machines():
    return list(Machine.objects.filter(active=True).order_by("sort", "name"))


@ensure_csrf_cookie
@require_GET
def page(request):
    me, denied = _gate(request)
    if denied:
        return denied
    q = request.GET
    cams = list(Camera.objects.prefetch_related("machines"))
    edit = q.get("edit")
    cur = next((c for c in cams if str(c.pk) == edit), None) if edit and edit != "new" else None
    return render(request, "cameras/page.html", {
        "mode": "admin", "me": me, "cams": cams, "machines": machines(), "kinds": Camera.KINDS,
        "edit": edit, "cur": cur, "curMachines": {m.pk for m in cur.machines.all()} if cur else set(),
        "msg": q.get("msg", ""), "err": q.get("err", ""), "ffmpeg": bool(grab.ffmpeg()), "rotations": (0, 90, 180, 270)})


def _back(**kw):
    return HttpResponseRedirect("/cameras/?" + urlencode({k: v for k, v in kw.items() if v}))


@require_POST
def do(request, name):
    me, denied = _gate(request)
    if denied:
        return denied
    p = request.POST
    if name == "save":
        pk = p.get("id") or ""
        cam = Camera.objects.filter(pk=pk).first() if pk.isdigit() else None
        if pk and not cam:
            return _back(err="That camera is no longer on file.")
        label = (p.get("name") or "").strip()[:80]
        kind = p.get("kind") if p.get("kind") in dict(Camera.KINDS) else "snapshot"
        err = "" if label else "Type the camera's name."
        url = ""
        if not err:
            try:
                url = grab.check_url(kind, p.get("url"))
            except grab.CamError as e:
                err = str(e)
        try:
            fps = max(0.2, min(5.0, float(p.get("fps") or 1)))
        except ValueError:
            fps = 1.0
        if err:
            return _back(err=err, edit=pk or "new")
        new = cam is None
        cam = cam or Camera()
        cam.name, cam.kind, cam.url, cam.fps = label, kind, url, fps
        cam.rotate = int(p.get("rotate")) if p.get("rotate") in ("0", "90", "180", "270") else 0
        cam.enabled = p.get("enabled") == "1"
        cam.sort = int(p.get("sort") or 0) if str(p.get("sort") or "0").lstrip("-").isdigit() else 0
        cam.updated, cam.updated_by = timezone.now(), me.name
        cam.save()
        cam.machines.set(Machine.objects.filter(pk__in=[x for x in p.getlist("machines") if x.isdigit()]))
        user, pw = (p.get("user") or "").strip(), p.get("password") or ""
        login_msg = ""
        if p.get("clear_login") == "1":
            liveconfig.save({cam.env_user: None, cam.env_password: None})
            cam.has_login = False
            login_msg = " Login removed."
        elif user or pw:
            if any(ch in user + pw for ch in "\r\n\""):
                return _back(err="The user name and password can't contain quotes or line breaks.", edit=str(cam.pk))
            changes = {cam.env_user: user}
            if pw:
                changes[cam.env_password] = pw
            liveconfig.save(changes)
            cam.has_login = True
        cam.save(update_fields=["has_login"])
        grab.forget(cam.pk)
        _audit(me, f"Camera {'added' if new else 'changed'}: {cam.name} ({cam.get_kind_display()}) on "
                            f"{', '.join(m.name for m in cam.machines.all()) or 'no machine'}")
        return _back(msg=f"{cam.name} saved.{login_msg}", test=str(cam.pk))
    cam = Camera.objects.filter(pk=p.get("id") or 0).first()
    if not cam:
        return _back(err="That camera is no longer on file.")
    if name == "delete":
        if cam.has_login:
            liveconfig.save({cam.env_user: None, cam.env_password: None})
        grab.forget(cam.pk)
        _audit(me, f"Camera removed: {cam.name}")
        cam.delete()
        return _back(msg=f"{cam.name} removed.")
    if name == "test":
        t0 = time.monotonic()
        try:
            img = grab.fetch(cam)
            return _back(msg=f"{cam.name}: picture received ({len(img) // 1024} KB in {time.monotonic() - t0:.1f} s).")
        except grab.CamError as e:
            return _back(err=f"{cam.name}: {e}", edit=str(cam.pk))
    raise Http404


@never_cache
@require_GET
def frame(request, pk):
    """The camera's latest picture (JPEG). 503 with the reason as text when there's none."""
    cam = Camera.objects.filter(pk=pk, enabled=True).first()
    if not cam:
        raise Http404
    f = grab.frame(cam)
    if not f["img"]:
        return HttpResponse(f["err"] or "No picture yet.", status=503, content_type="text/plain; charset=utf-8")
    r = HttpResponse(f["img"], content_type="image/jpeg")
    r["X-Frame-Age"] = str(max(0, round(time.time() - f["t"])))
    if f["err"]:
        r["X-Frame-Error"] = f["err"][:300].encode("ascii", "replace").decode()
    r["Cache-Control"] = "no-store"
    return r
