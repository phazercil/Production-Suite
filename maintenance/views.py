"""Maintenance: every pending request from the lines (with its status and the parts used on it), what was
completed, the inventory, and the parts used on each machine. People with "Open Maintenance" (Admin › Permissions)."""
from datetime import timedelta
from urllib.parse import urlencode

from django.db.models import Count, Max, Q, Sum
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from hmi import engine
from hmi.models import Ticket, TicketNote, TicketPhoto
from hmi.views import current_person, touch
from plant import perms

from .actions import machines
from .models import InventoryItem, InventoryMove

PERM = "section.maintenance"
TABS = [("requests", "Pending requests"), ("done", "Completed"), ("inventory", "Inventory"), ("machines", "Parts by machine")]
POSTS = {"ticketSet", "itemSave", "itemArchive", "itemStock", "itemUse", "itemUseUndo"}
PRIORITY = {"High": 0, "Medium": 1, "Low": 2}


def _gate(request):
    me, note = current_person(request)
    if perms.can(me, PERM):
        touch(request)
        return me, None
    msg = note or (f"{me.name} doesn't have permission to open Maintenance (Admin › Permissions › Open Maintenance)." if me else None)
    return None, render(request, "supervisor/login.html", {"mode": "maintenance", "note": msg, "perm": PERM, "title": "Maintenance",
                                                           "blurb": "Pending requests from the lines, their status and the parts used, and the inventory."})


def _tickets(qs, now):
    tickets = list(qs)
    notes, pics, used = {}, {}, {}
    for n in TicketNote.objects.filter(ticket__in=tickets):
        notes.setdefault(n.ticket_id, []).append(n)
    for p in TicketPhoto.objects.filter(ticket__in=tickets).values("id", "ticket_id"):
        pics.setdefault(p["ticket_id"], []).append(p["id"])
    for m in InventoryMove.objects.filter(ticket__in=tickets, kind="used").select_related("item"):
        used.setdefault(m.ticket_id, []).append(m)
    return [{"t": t, "notes": notes.get(t.id, []), "photos": pics.get(t.id, []), "used": used.get(t.id, []),
             "age": round((now - t.t).total_seconds() / 60),
             "took": round((t.updated - t.t).total_seconds() / 60) if t.status == "Closed" and t.updated else None} for t in tickets]


@ensure_csrf_cookie
@require_GET
def page(request):
    me, denied = _gate(request)
    if denied:
        return denied
    now = timezone.now()
    q = request.GET
    tab = q.get("tab") if q.get("tab") in dict(TABS) else "requests"
    mach, st = str(q.get("machine") or "")[:60], str(q.get("status") or "")[:20]
    ctx = {"mode": "maintenance", "me": me, "tab": tab, "tabs": TABS, "now": now, "msg": q.get("msg", ""), "err": q.get("err", ""),
           "fmachine": mach, "fstatus": st, "statuses": Ticket.STATUSES, "machines": machines(),
           "canWork": perms.can(me, "tickets.work"), "canStock": perms.can(me, "inventory.edit"),
           "here": request.get_full_path(),
           "items": list(InventoryItem.objects.filter(active=True)),
           "counts": {"open": Ticket.objects.filter(status="Open").count(),
                      "work": Ticket.objects.filter(status__in=("Reviewed", "In progress")).count(),
                      "parts": Ticket.objects.filter(status="Waiting on parts").count(),
                      "done": Ticket.objects.filter(status="Closed", updated__gte=now - timedelta(days=7)).count()}}
    if tab in ("requests", "done"):
        qs = Ticket.objects.exclude(status="Closed") if tab == "requests" else Ticket.objects.filter(status="Closed", updated__gte=now - timedelta(days=30))
        if mach:
            qs = qs.filter(machine=mach)
        if st and tab == "requests":
            qs = qs.filter(status=st)
        rows = _tickets(qs.order_by("-updated" if tab == "done" else "t")[:300], now)
        if tab == "requests":
            rows.sort(key=lambda r: (PRIORITY.get(r["t"].priority, 1), r["t"].t))
        ctx["rows"] = rows
        ctx["reqMachines"] = sorted(set(Ticket.objects.values_list("machine", flat=True)) | set(ctx["machines"]))
    elif tab == "inventory":
        month = now - timedelta(days=30)
        show_all = q.get("all") == "1"
        qs = InventoryItem.objects.all() if show_all else InventoryItem.objects.filter(active=True)
        s = str(q.get("q") or "").strip()[:60]
        if s:
            qs = qs.filter(Q(name__icontains=s) | Q(part_no__icontains=s) | Q(location__icontains=s))
        items = list(qs.annotate(used30=Sum("moves__qty", filter=Q(moves__kind="used", moves__t__gte=month))))
        on = {}
        for r in InventoryMove.objects.filter(kind="used", item__in=items).values("item_id", "machine").annotate(n=Sum("qty")).order_by("-n"):
            on.setdefault(r["item_id"], []).append((r["machine"], r["n"]))
        ctx.update(inv=[{"i": i, "on": on.get(i.pk, [])} for i in items], q=s, showAll=show_all,
                   low=sum(1 for i in items if i.active and i.low),
                   edit=int(q.get("edit")) if str(q.get("edit") or "").isdigit() else None,
                   moves=list(InventoryMove.objects.select_related("item", "ticket")[:60]),
                   pending=list(Ticket.objects.exclude(status="Closed").order_by("machine", "t")))
    else:   # parts by machine
        names = list(dict.fromkeys(ctx["machines"] + list(InventoryMove.objects.exclude(machine="").values_list("machine", flat=True).distinct())))
        sel = mach if mach in names else ""
        per = {r["machine"]: r for r in InventoryMove.objects.filter(kind="used").values("machine").annotate(n=Count("id"), last=Max("t"))}
        ctx["mrows"] = [{"m": m, "n": per.get(m, {}).get("n", 0), "last": per.get(m, {}).get("last"),
                         "open": Ticket.objects.filter(machine=m).exclude(status="Closed").count()} for m in names]
        if sel:
            ctx["sel"] = sel
            ctx["byItem"] = list(InventoryMove.objects.filter(kind="used", machine=sel).values("item__name", "item__part_no", "item__unit")
                                 .annotate(n=Sum("qty"), last=Max("t"), times=Count("id")).order_by("-last"))
            ctx["uses"] = list(InventoryMove.objects.filter(kind="used", machine=sel).select_related("item", "ticket")[:200])
    ctx["tabq"] = {k: urlencode({"tab": k}) for k, _ in TABS}
    return render(request, "maintenance/page.html", ctx)


@require_POST
def do(request, name):
    """A form on the Maintenance page: runs the command, then back to the page with what happened."""
    me, denied = _gate(request)
    if denied:
        return denied
    back = request.POST.get("back") or "/maintenance/"
    if not back.startswith("/maintenance/"):
        back = "/maintenance/"
    if name not in POSTS:
        r = {"ok": False, "error": "Unknown action."}
    else:
        p = {k: request.POST.get(k) for k in request.POST if k not in ("csrfmiddlewaretoken", "back")}
        r = engine.execute(name, p, engine.Ctx(user=me))
    base = back.split("#")[0]
    base = "&".join(x for x in base.split("&") if not x.startswith(("msg=", "err=", "edit="))).replace("?&", "?")
    sep = "&" if "?" in base else "?"
    msg = urlencode({"msg": r.get("msg") or "Saved."}) if r.get("ok") else urlencode({"err": r.get("error") or "That didn't save."})
    if not r.get("ok") and name == "itemSave" and request.POST.get("id"):
        msg += "&edit=" + str(request.POST.get("id"))
    return HttpResponseRedirect(f"{base}{sep}{msg}" + (f"#{request.POST.get('anchor')}" if request.POST.get("anchor") else ""))
