"""Packaging Kiosk: pallet tags.

  New tag     part number (or pick the MO), manufacturing line, pallet number (filled in with the next one),
              quantity and description. The person logged in is the packer and signs it by making it.
  Sign-offs   a second person verifies the pallet, and a shift lead signs as supervisor, each with their PIN.
  Print       a full-page tag (landscape letter) for the pallet, with the names of whoever signed in the suite
              and a blank line to sign by hand for anyone who hasn't.

Open with "Open Packaging Kiosk" (Admin › Permissions). Rules are in packkiosk/tags.py."""
from datetime import timedelta
from urllib.parse import urlencode

from django.db.models import Q
from django.http import Http404, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from hmi.views import check_pin, current_person, touch
from plant import perms

from . import tags
from .models import PalletTag

PERM = "section.packaging"
RANGES = [("today", "Today"), ("week", "Last 7 days"), ("month", "Last 30 days")]


def _gate(request):
    me, note = current_person(request)
    if perms.can(me, PERM):
        touch(request)
        return me, None
    msg = note or (f"{me.name} doesn't have permission to open the Packaging Kiosk (Admin › Permissions › Open Packaging Kiosk)." if me else None)
    return None, render(request, "supervisor/login.html", {"mode": "packaging", "note": msg, "perm": PERM, "title": "Packaging Kiosk",
                                                           "blurb": "Pallet tags for finished filters: part number, line, pallet number, "
                                                                    "quantity and who packed, verified and signed off each pallet."})


@ensure_csrf_cookie
@require_GET
def page(request):
    me, denied = _gate(request)
    if denied:
        return denied
    q = request.GET
    rng = q.get("range") if q.get("range") in dict(RANGES) else "today"
    find = tags.clean(q.get("q"), 40)
    now = timezone.now()
    qs = PalletTag.objects.all()
    if find:
        qs = qs.filter(Q(part__icontains=find) | Q(mo__icontains=find) | Q(line__icontains=find) | Q(packed_by__icontains=find))
    else:
        start = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
        qs = qs.filter(t__gte=start if rng == "today" else now - timedelta(days=7 if rng == "week" else 30))
    if q.get("need") == "verify":       # quick filter: what still needs a second person
        qs = qs.filter(verified_by="", voided=False)
    if q.get("line"):
        qs = qs.filter(line=q.get("line"))
    rows = list(qs[:300])
    day0 = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    last = PalletTag.objects.filter(packed_by_id=me.pk, voided=False, t__gte=day0).order_by("-t", "-id").first()   # Same as last (+1)
    live = [r for r in rows if not r.voided]
    form = {k: q.get(k, "") for k in ("part", "mo", "line", "pallet_no", "qty", "description")} if q.get("err") else {}
    ctx = {"mode": "packaging", "me": me, "now": now, "msg": q.get("msg", ""), "err": q.get("err", ""), "rows": rows, "range": rng,
           "ranges": RANGES, "find": find, "lines": tags.lines(), "mos": tags.todays_mos(form.get("mo", "")), "form": form,
           "parts": sorted(set(PalletTag.objects.values_list("part", flat=True)[:500])),
           "printId": q.get("print", ""),
           "kpi": {"pallets": len(live), "pcs": sum(r.qty for r in live), "unverified": sum(1 for r in live if not r.verified_by)},
           "canSupervise": perms.can(me, tags.SUPERVISE), "last": last, "lastNext": tags.next_pallet(last.mo, last.part) if last else None,
           "need": q.get("need", ""), "fline": q.get("line", "")}
    return render(request, "packkiosk/page.html", ctx)


@require_GET
def suggest(request):
    me, denied = _gate(request)
    if denied:
        return JsonResponse({"ok": False, "error": "Log in first."}, status=403)
    return JsonResponse({"ok": True, **tags.suggest(request.GET.get("mo"), request.GET.get("part"))})


def _back(r, extra=None, form=None):
    p = {"msg": r.get("msg") or "Saved."} if r.get("ok") else {"err": r.get("error") or "That didn't save.", **(form or {})}
    return HttpResponseRedirect("/packaging/?" + urlencode({**p, **(extra or {})}))


@require_POST
def do(request, name):
    me, denied = _gate(request)
    if denied:
        return denied
    p = request.POST
    if name == "create":
        r = tags.create(p, me)
        form = {k: p.get(k, "") for k in ("part", "mo", "line", "pallet_no", "qty", "description")}
        return _back(r, {"print": r["id"]} if r.get("ok") and p.get("andPrint") == "1" else None, form)
    tag = PalletTag.objects.filter(pk=tags.to_int(p.get("id") or request.GET.get("id"))).first()
    if not tag:
        return _back({"ok": False, "error": "That tag isn't on file."})
    if name == "sign":
        kind = p.get("kind")
        signer, err = check_pin(request, p.get("pin"), perm=tags.VERIFY if kind == "verify" else tags.SUPERVISE)
        r = tags.sign(tag, signer, kind) if signer else {"ok": False, "error": err}
    elif name == "void":
        r = tags.void(tag, me, p.get("reason"))
        if r.get("ok"):   # an Undo for 8 seconds, in case it was the wrong row
            return HttpResponseRedirect("/packaging/?" + urlencode({"msg": r["msg"], "undo": f"/packaging/do/unvoid?id={tag.pk}",
                                                                   "undoText": f"{tag.tag_no} voided"}))
    elif name == "unvoid":
        r = tags.unvoid(tag, me)
    else:
        raise Http404
    return _back(r)


@require_GET
def print_tag(request, pk):
    me, denied = _gate(request)
    if denied:
        return denied
    tag = get_object_or_404(PalletTag, pk=pk)
    if tag.voided:
        return _back({"ok": False, "error": f"{tag.tag_no} was voided ({tag.void_reason}); it can't be printed."})
    tag.prints += 1
    tag.printed_t = timezone.now()
    tag.save(update_fields=["prints", "printed_t"])
    return render(request, "packkiosk/tag.html", {"tag": tag, "auto": request.GET.get("auto") == "1"})
