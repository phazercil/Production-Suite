"""Quality: the quality report (what went wrong on the floor: QC checks out of spec, machine vision fails, quality
holds) and Quality › Tolerances (how far under and over the drawing each measurement may be, for every filter).
People with "Open Quality" (Admin › Permissions) only; changing its settings needs "Quality settings"."""
import csv
from collections import Counter
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from django.db.models import Count, Q, Sum
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from hmi import qc
from hmi.models import AutoQC, Downtime, QCCheck
from hmi.views import current_person, touch
from plant import perms
from plant.models import Machine, PlantSettings, part_key
from plant.util import local, part_spec

PERM = "section.quality"
TOL_PERM = "quality.tolerances"
TABS = [("report", "Quality report"), ("checks", "QC checks"), ("parts", "Parts & specs"), ("tol", "Tolerances")]
ACTIONS = {"qcTolSet": "tol", "qcItemAdd": "checks", "qcItemDel": "checks", "qcItemMove": "checks", "qualityIssuesSet": "checks",
           "partsSave": "parts", "partsImport": "parts", "partAdd": "parts", "partDel": "parts", "assetUnitsSet": "parts", "partsRefresh": "parts"}
PART_FIELDS = [("pleats", "Pleats/unit", "1"), ("len", "Length", ".001"), ("wid", "Width", ".001"), ("ph", "Pleat ht", ".001"),
               ("sb", "Side band", ".001"), ("tol", "Tol ±", ".001")]
KINDS = [("", "All problems"), ("qc", "QC checks out of spec"), ("vision", "Machine vision fails"), ("hold", "Quality holds")]
MAX_ROWS = 500
VISION = [("len", "Length", "len"), ("wid", "Width", "wid"), ("ph", "Pleat height", "ph"), ("sbl", "Side band L", "sb"), ("sbr", "Side band R", "sb")]


def _gate(request):
    me, note = current_person(request)
    if perms.can(me, PERM):
        touch(request)
        return me, None
    msg = note or (f"{me.name} doesn't have permission to open Quality (Admin › Permissions › Open Quality)." if me else None)
    return None, render(request, "supervisor/login.html", {"mode": "quality", "note": msg, "title": "Quality", "perm": PERM,
                                                           "blurb": "Quality problems from the floor, and the tolerances every filter is checked against."})


def _day(v, default):
    try:
        return date.fromisoformat(str(v))
    except (TypeError, ValueError):
        return default


def filters(q):
    today = timezone.localdate()
    b = _day(q.get("to"), today)
    a = _day(q.get("from"), b - timedelta(days=6))
    if a > b:
        a, b = b, a
    a = max(a, b - timedelta(days=366))
    return {"tab": q.get("tab") if q.get("tab") in dict(TABS) else "report", "line": str(q.get("line") or "")[:60],
            "kind": q.get("kind") if q.get("kind") in dict(KINDS) else "", "part": str(q.get("part") or "").strip()[:40],
            "from": a, "to": b, "A": timezone.make_aware(datetime(a.year, a.month, a.day)),
            "B": timezone.make_aware(datetime(b.year, b.month, b.day)) + timedelta(days=1)}


def _query(f, **change):
    keep = {"tab": f["tab"], "line": f["line"], "kind": f["kind"], "part": f["part"], "from": f["from"].isoformat(), "to": f["to"].isoformat()}
    keep.update(change)
    return urlencode({k: v for k, v in keep.items() if v not in ("", None)})


def _labels():
    lab = {k: v["label"] for k, v in qc.BUILTIN.items()}
    for items in (PlantSettings.load().config.get("qcItems") or {}).values():
        for it in items if isinstance(items, list) else []:
            if isinstance(it, dict) and it.get("key") and it.get("label"):
                lab.setdefault(it["key"], it["label"])
    return lab


def _fmt(v):
    return f"{v:.3f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)


def report(f):
    """The quality problems in the filters, newest first, and what they add up to."""
    labels, tols, specs = _labels(), qc.plant_tols(), {}

    def spec(item):
        k = part_key(item)
        if k not in specs:
            specs[k] = part_spec(k)
        return specs[k]

    rows, by_what, by_part, by_issue = [], Counter(), Counter(), Counter()
    checks = QCCheck.objects.filter(t__gte=f["A"], t__lt=f["B"]).select_related("recheck")
    vision = AutoQC.objects.filter(t__gte=f["A"], t__lt=f["B"])
    holds = Downtime.objects.filter(start_time__gte=f["A"], start_time__lt=f["B"], reason__icontains="quality")
    if f["line"]:
        checks, vision, holds = checks.filter(line=f["line"]), vision.filter(line=f["line"]), holds.filter(line=f["line"])
    if f["part"]:
        checks = checks.filter(Q(item__icontains=f["part"]) | Q(mo__icontains=f["part"]))
        vision = vision.filter(Q(item__icontains=f["part"]) | Q(mo__icontains=f["part"]))
        holds = holds.filter(mo__icontains=f["part"])
    agg = checks.aggregate(n=Count("id"), fail=Count("id", filter=Q(passed=False)), pend=Count("id", filter=Q(review="pending")),
                           scrap=Sum("scrap", filter=Q(passed=False)), taken=Sum("scrapped"))
    vagg = vision.aggregate(n=Count("id"), fail=Count("id", filter=Q(passed=False)))
    hold_min = holds.aggregate(m=Sum("minutes"))["m"] or 0
    if f["kind"] in ("", "qc"):
        for q in checks.filter(passed=False).select_related("shift").order_by("-t")[:MAX_ROWS]:
            sp = spec(q.item)
            vals = q.values or {k: getattr(q, k) for k in ("len", "wid", "ph", "sbl", "sbr", "glue", "cut") if getattr(q, k) not in (None, "")}
            detail = []
            for k in q.fails or []:
                v, it = vals.get(k), qc.BUILTIN.get(k)
                note = ""
                if it and it["type"] == "measure" and isinstance(v, (int, float)) and sp.get(it["spec"]) is not None:
                    t = sp[it["spec"]]
                    note = f"drawing {_fmt(float(t))} {qc.fmt_tol(it['spec'], sp, tols)} · {'over' if v > t else 'under'} by {_fmt(round(abs(v - t), 4))}"
                detail.append({"k": labels.get(k, k), "v": _fmt(v) if v is not None else "", "note": note})
                by_what[labels.get(k, k)] += 1
            by_part[part_key(q.item)] += 1
            if q.issue:
                by_issue[q.issue] += q.scrapped or 0
            rows.append({"t": q.t, "line": q.line, "kind": "QC check", "mo": q.mo, "item": q.item, "who": q.by, "detail": detail,
                         "scrap": q.scrap, "status": q.review or "", "reviewedBy": q.reviewed_by, "scrapped": q.scrapped,
                         "issue": q.issue, "issueNote": q.issue_note, "recheck": q.recheck_status(), "late": q.late_reason, "shift": q.shift_id,
                         "edit": f"/supervisor/edit/qc/{q.pk}/", "processOut": f"/supervisor/processout/{q.pk}/"})
    if f["kind"] in ("", "vision"):
        for a in vision.filter(passed=False).order_by("-t")[:MAX_ROWS]:
            sp = spec(a.item) if a.item else {}
            detail = []
            for k, label, s_ in VISION:
                v = getattr(a, k)
                if v is None:
                    continue
                t = sp.get(s_)
                bad = t is not None and not qc.in_tol(v, t, s_, sp, tols)
                if bad:
                    by_what[label] += 1
                detail.append({"k": label, "v": _fmt(v), "note": f"drawing {_fmt(float(t))}" if bad else "", "ok": not bad})
            by_part[part_key(a.item)] += 1
            rows.append({"t": a.t, "line": a.line, "kind": "Machine vision", "mo": a.mo, "item": a.item, "who": a.source_name, "detail": detail,
                         "status": "", "shift": a.shift_id})
    if f["kind"] in ("", "hold"):
        for d in holds.select_related("shift").order_by("-start_time")[:MAX_ROWS]:
            rows.append({"t": d.start_time, "line": d.line, "kind": "Quality hold", "mo": d.mo, "item": "",
                         "who": (d.shift.user_name if d.shift else "") or d.by or "", "detail": [{"k": d.reason, "v": f"{d.minutes} min", "note": d.notes}],
                         "status": "", "shift": d.shift_id, "edit": f"/supervisor/edit/downtime/{d.pk}/"})
            by_what[d.reason] += 1
    rows.sort(key=lambda r: r["t"], reverse=True)
    n = agg["n"] or 0
    kpis = [("QC checks", n), ("Out of spec", agg["fail"] or 0), ("Fail rate", f"{round(100 * (agg['fail'] or 0) / n, 1)}%" if n else "—"),
            ("Waiting for review", agg["pend"] or 0), ("Scrap on failed checks", agg["scrap"] or 0),
            ("Machine vision fails", f"{vagg['fail'] or 0} of {vagg['n'] or 0}"), ("Quality hold", f"{hold_min} min")]
    po = checks.filter(passed=False).exclude(issue="").aggregate(n=Sum("scrapped"))["n"] or 0
    kpis.insert(5, ("Processed out", po))
    return {"rows": rows[:MAX_ROWS], "more": len(rows) > MAX_ROWS, "kpis": kpis, "byWhat": by_what.most_common(8), "byPart": by_part.most_common(8),
            "byIssue": by_issue.most_common(8)}


def csv_rows(rows):
    yield ["Time", "Line", "Kind", "MO", "Item", "By", "Problem", "Scrap", "Review", "Processed out", "Quality issue"]
    for r in rows:
        yield [local(r["t"]).strftime("%Y-%m-%d %H:%M"), r["line"], r["kind"], r["mo"], r["item"], r["who"],
               "; ".join(f"{d['k']} {d['v']}" + (f" ({d['note']})" if d.get("note") else "") for d in r["detail"] if not d.get("ok")),
               r.get("scrap", ""), r.get("status", ""), r.get("scrapped") if r.get("issue") else "",
               (r.get("issue") or "") + (f": {r['issueNote']}" if r.get("issueNote") else "")]


def tol_rows(cfg=None):
    t = qc.plant_tols(cfg)
    return [{"k": k, "label": label, "minus": t[k]["minus"] if k in t else "", "plus": t[k]["plus"] if k in t else "", "set": k in t}
            for k, label in qc.TOL_SPECS]


@ensure_csrf_cookie
@require_http_methods(["GET", "POST"])
def page(request):
    from hmi import engine
    me, denied = _gate(request)
    if denied:
        return denied
    f = filters(request.GET if request.method == "GET" else request.POST)
    can_tol = perms.can(me, TOL_PERM)
    err = ""
    if request.method == "POST":
        a = request.POST.get("action") or "qcTolSet"
        a = a if a in ACTIONS else "qcTolSet"
        p = {k: request.POST.get(k) for k in request.POST if k not in ("csrfmiddlewaretoken", "action")}
        if a == "partsSave":   # the whole table: one list per column, in row order
            cols = {f: request.POST.getlist(f) for f in ["k", *(x[0] for x in PART_FIELDS), "rev", "media"]}
            p = {"rows": [{f: (v[i] if i < len(v) else "") for f, v in cols.items()} for i in range(len(cols["k"]))]}
        r = engine.execute(a, p, engine.Ctx(user=me))
        if r.get("ok"):
            return HttpResponseRedirect("/quality/?" + urlencode({"tab": ACTIONS[a], "msg": r.get("msg") or "Saved."}))
        err = r.get("error") or "That didn't save."
        f["tab"] = ACTIONS[a]
    ctx = {"mode": "quality", "me": me, "f": f, "tabs": TABS, "kinds": KINDS, "canTol": can_tol, "err": err,
           "msg": request.GET.get("msg", "")[:200], "here": _query(f), "tabq": {k: _query(f, tab=k) for k, _ in TABS},
           "canEdit": perms.can(me, "history.edit"), "canSup": perms.can(me, "section.supervisor"), "canOut": perms.can(me, "qc.approve")}
    if f["tab"] == "checks":
        from plant.models import STAGES
        cfg = PlantSettings.load().config
        ctx["stages"] = [{"k": k, "label": lbl, "items": qc.items_for(k, cfg),
                          "free": [(b, d["label"]) for b, d in qc.BUILTIN.items() if b not in {i["key"] for i in qc.items_for(k, cfg)}]}
                         for k, lbl in STAGES]
        ctx["types"], ctx["qcLate"] = qc.TYPES, (cfg.get("rules") or {}).get("qcLate", 10)
        ctx["issues"] = "\n".join(x for x in cfg.get("qualityIssues") or [] if x != "Other")
        return render(request, "quality/page.html", ctx)
    if f["tab"] == "parts":
        from hmi import integrations
        from hmi.models import SavedQuery
        from plant.models import PartSpec
        cfg = PlantSettings.load().config
        q = request.GET.get("q", "").strip().upper()[:40]
        parts = PartSpec.objects.order_by("key")
        if q:
            parts = parts.filter(key__icontains=q)
        n = parts.count()
        ctx.update(parts=[{"p": x, "vals": [(f, lbl, st, getattr(x, f)) for f, lbl, st in PART_FIELDS]} for x in parts[:500]], nParts=n, q=q,
                   fields=PART_FIELDS, units=cfg.get("assetUnits") or {}, unitSpecs=[(k, lbl) for k, lbl in qc.TOL_SPECS] + [("tol", "Tolerance")],
                   canImport=integrations.assets_ready() and SavedQuery.objects.filter(name__in=("part_spec", "part_drawing")).exists(),
                   uomWired="AS uom" in (getattr(SavedQuery.objects.filter(name="part_spec").first(), "sql", "") or ""),
                   pasted=request.POST.get("parts", "") if err else "")
        return render(request, "quality/page.html", ctx)
    if f["tab"] == "tol":
        ctx["tols"] = tol_rows()
        if err:   # what was typed, so it can be fixed
            for t in ctx["tols"]:
                t["minus"], t["plus"] = request.POST.get(f"{t['k']}_minus", ""), request.POST.get(f"{t['k']}_plus", "")
        return render(request, "quality/page.html", ctx)
    R = report(f)
    if request.GET.get("csv") == "1":
        resp = HttpResponse(content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="quality-{f["from"]}-to-{f["to"]}.csv"'
        w = csv.writer(resp)
        for row in csv_rows(R["rows"]):
            w.writerow(row)
        return resp
    ctx.update(R=R, csv=_query(f, csv="1"), lines=sorted({m.name for m in Machine.objects.all()} | ({f["line"]} if f["line"] else set())))
    return render(request, "quality/page.html", ctx)
