"""Quality › Work instructions: the list, one instruction (its approved revision, the draft being written, and every
revision before), the photos and videos, and the form posts. Writing needs "Write work instructions"; approving
needs "Approve work instructions"; anyone who may open Quality can read them (and every operator on the kiosk)."""
import json
import mimetypes
import os
import uuid
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from hmi import engine
from hmi.views import current_person, touch
from plant import perms
from plant.models import Machine

from .cmds import for_station
from .models import Ack, Instruction, Media, Revision

PERM = "section.quality"
VIDEO_MAX = 300 * 1024 * 1024
VIDEO_TYPES = {"video/mp4", "video/webm", "video/quicktime", "video/ogg"}
POSTS = {"wiCreate", "wiMeta", "wiDraft", "wiSave", "wiDiscard", "wiApprove"}


def media_dir():
    d = Path(settings.BASE_DIR) / "media" / "instructions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _gate(request, need_section=True):
    me, note = current_person(request)
    if me and (not need_section or perms.can(me, PERM)):
        touch(request)
        return me, None
    return None, render(request, "supervisor/login.html", {"mode": "quality", "note": note or (f"{me.name} can't open Quality." if me else None),
                                                           "perm": PERM, "title": "Work instructions",
                                                           "blurb": "Setup, changeover and operating instructions, revision-controlled."})


def _ctx(me, **kw):
    from quality.views import TABS
    return {"mode": "quality", "me": me, "tabs": TABS, "canEdit": perms.can(me, "wi.edit"), "canApprove": perms.can(me, "wi.approve"),
            "kinds": Instruction.KINDS, "machines": list(Machine.objects.order_by("sort", "id")), **kw}


def index(request):
    me, denied = _gate(request)
    if denied:
        return denied
    q = request.GET
    qs = Instruction.objects.prefetch_related("machines", "revisions")
    if q.get("kind") in dict(Instruction.KINDS):
        qs = qs.filter(kind=q["kind"])
    if str(q.get("machine") or "").isdigit():
        qs = qs.filter(machines__pk=int(q["machine"])) | qs.filter(machines__isnull=True)
    s = str(q.get("q") or "").strip()[:60]
    if s:
        qs = qs.filter(title__icontains=s) | qs.filter(doc_no__icontains=s) | qs.filter(parts__icontains=s)
    rows = []
    for x in qs.distinct().order_by("doc_no"):
        revs = list(x.revisions.all())
        cur = next((r for r in revs if r.status == "approved"), None)
        rows.append({"x": x, "cur": cur, "draft": next((r for r in revs if r.status == "draft"), None),
                     "acks": cur.acks.count() if cur else 0})
    return render(request, "instructions/index.html", _ctx(me, rows=rows, f=q, msg=q.get("msg", ""), err=q.get("err", "")))


def detail(request, pk, rev=None):
    me, denied = _gate(request)
    if denied:
        return denied
    x = Instruction.objects.filter(pk=pk).prefetch_related("machines").first()
    if not x:
        raise Http404
    revs = list(x.revisions.all())
    shown = next((r for r in revs if r.rev == rev), None) if rev else None
    if rev and not shown:
        raise Http404
    cur = next((r for r in revs if r.status == "approved"), None)
    draft = next((r for r in revs if r.status == "draft"), None)
    media = {m.pk: m for m in Media.objects.filter(instruction=x).defer("data", "thumb")}
    edit = draft and perms.can(me, "wi.edit") and not rev and request.GET.get("view") != "1"
    r = shown or (draft if edit else cur or draft)
    steps = [{**s, "m": [media[i] for i in s.get("media", []) if i in media]} for s in (r.steps if r else [])]
    if edit:   # room to add: two blank steps and three blank parameters
        steps += [{"text": "", "caution": "", "media": [], "m": []} for _ in range(2)]
    params = list(r.params if r else []) + ([{} for _ in range(3)] if edit else [])
    acks = list(cur.acks.all()[:200]) if cur else []
    return render(request, "instructions/detail.html", _ctx(
        me, x=x, revs=revs, r=r, cur=cur, draft=draft, edit=edit, steps=steps, params=params, media=list(media.values()),
        acks=acks, mids=[m.pk for m in x.machines.all()], msg=request.GET.get("msg", ""), err=request.GET.get("err", ""),
        mine=bool(draft and me and me.name in [a.strip() for a in draft.author.split(",")])))


@require_POST
def do(request, name):
    me, denied = _gate(request)
    if denied:
        return denied
    if name not in POSTS:
        raise Http404
    P = request.POST
    p = {k: P.get(k) for k in P if k not in ("csrfmiddlewaretoken",)}
    p["machines"] = P.getlist("machines")
    if name == "wiSave":   # the editor: one list per field, in row order
        texts, cautions = P.getlist("s_text"), P.getlist("s_caution")
        p["steps"] = [{"text": texts[i], "caution": cautions[i] if i < len(cautions) else "",
                       "media": P.getlist(f"s_media_{i}")} for i in range(len(texts))]
        cols = {k: P.getlist(f"p_{k}") for k in ("name", "target", "min", "max", "unit")}
        p["params"] = [{k: (v[i] if i < len(v) else "") for k, v in cols.items()} for i in range(len(cols["name"]))]
    r = engine.execute(name, p, engine.Ctx(user=me))
    pk = (r.get("data") or {}).get("id") or p.get("id")
    base = f"/instructions/{pk}/" if pk else "/instructions/"
    from urllib.parse import urlencode
    return HttpResponseRedirect(base + "?" + urlencode({"msg": r.get("msg") or "Saved."} if r.get("ok") else {"err": r.get("error") or "That didn't save."}))


@require_POST
def upload(request, pk):
    """A photo or video for an instruction's steps (on the draft's editor)."""
    me, denied = _gate(request)
    if denied:
        return denied
    x = Instruction.objects.filter(pk=pk).first()
    if not x:
        raise Http404
    back = f"/instructions/{pk}/"
    if not perms.can(me, "wi.edit"):
        return HttpResponseRedirect(back + "?err=Adding+photos+and+videos+needs+Write+work+instructions.")
    n = 0
    for f in request.FILES.getlist("files")[:20]:
        ctype = (f.content_type or mimetypes.guess_type(f.name)[0] or "").lower()
        if ctype.startswith("image/"):
            from hmi import photos
            if f.size > photos.MAX_BYTES:
                continue
            try:
                jpeg, thumb, w, h = photos.process(f.read())
            except photos.PhotoError:
                continue
            Media.objects.create(instruction=x, kind="image", content_type="image/jpeg", data=jpeg, thumb=thumb, size=len(jpeg),
                                 name=f.name[:200], t=timezone.now(), by=me.name)
            n += 1
        elif ctype in VIDEO_TYPES and f.size <= VIDEO_MAX:
            ext = {"video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov", "video/ogg": ".ogv"}[ctype]
            fn = f"{x.pk}-{uuid.uuid4().hex}{ext}"
            with open(media_dir() / fn, "wb") as out:
                for chunk in f.chunks():
                    out.write(chunk)
            Media.objects.create(instruction=x, kind="video", content_type=ctype, file=fn, size=f.size, name=f.name[:200],
                                 t=timezone.now(), by=me.name)
            n += 1
    engine.audit(engine.Ctx(user=me), f"Work instructions: {x.doc_no}: {n} photo(s)/video(s) added")
    msg = f"{n} added: pick them on the steps they belong to." if n else "Nothing added: photos (JPEG, PNG…) and videos (MP4, WebM, MOV, 300 MB at most)."
    return HttpResponseRedirect(back + "?" + ("msg=" if n else "err=") + msg.replace(" ", "+"))


@require_GET
def media(request, mid):
    """A photo (thumb with ?size=thumb) or video. Anyone signed in on a screen may see them (the kiosk shows them)."""
    me, _ = current_person(request)
    if not me and not request.session.get("station"):   # someone signed in, or a kiosk screen set to its machine
        return HttpResponse(status=403)
    m = Media.objects.filter(pk=mid).first()
    if not m:
        raise Http404
    if m.kind == "image":
        data = bytes(m.thumb) if request.GET.get("size") == "thumb" and m.thumb else bytes(m.data)
        r = HttpResponse(data, content_type="image/jpeg")
    else:
        path = media_dir() / os.path.basename(m.file)
        if not path.is_file():
            raise Http404("The video file is missing from media/instructions/")
        r = FileResponse(open(path, "rb"), content_type=m.content_type)
    r["Cache-Control"] = "private, max-age=86400"
    return r


def kiosk_data(s):
    """The kiosk's Instructions page: what applies to this machine and the part it runs (or changes over to)."""
    from plant.models import MO
    sh = s.shift
    part = sh.mo.part if sh and sh.mo else ""
    nxt = ""
    if sh and sh.co and sh.co.get("newMo"):
        m = MO.objects.filter(pk=sh.co["newMo"]).first()
        nxt = m.part if m else ""
    who = s.me.name if s.me else ""
    items = for_station(s.machine, nxt or part, who)
    for d in items:
        media = {m.pk: m for m in Media.objects.filter(instruction=d["x"]).defer("data", "thumb")}
        d["steps"] = [{**st, "m": [media[i] for i in st.get("media", []) if i in media]} for st in d["r"].steps]
    return {"wi": items, "wiPart": nxt or part, "wiNext": bool(nxt), "wiJson": json.dumps([d["r"].pk for d in items])}


def unread(s):
    """Approved instructions for this machine and part the person signed in hasn't read (for the kiosk's banner)."""
    if not s.me or not s.machine:
        return []
    sh = s.shift
    part = sh.mo.part if sh and sh.mo else ""
    return [d for d in for_station(s.machine, part, s.me.name) if not d["read"]]


def acks_csv(request, pk):
    import csv
    me, denied = _gate(request)
    if denied:
        return denied
    x = Instruction.objects.filter(pk=pk).first()
    if not x:
        raise Http404
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{x.doc_no}-read-by.csv"'
    w = csv.writer(resp)
    w.writerow(["Document", "Revision", "Status", "Read by", "Machine", "When"])
    for a in Ack.objects.filter(revision__instruction=x).select_related("revision").order_by("revision__rev", "t"):
        w.writerow([x.doc_no, a.revision.rev, a.revision.status, a.who, a.machine, timezone.localtime(a.t).strftime("%Y-%m-%d %H:%M")])
    return resp


__all__ = ["index", "detail", "do", "upload", "media", "kiosk_data", "unread", "acks_csv", "Revision"]
