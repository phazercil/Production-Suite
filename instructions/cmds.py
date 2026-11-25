"""Work-instruction commands (registered with the engine: checked, logged and rolled back like every other), and
which instructions a machine and part get (for the kiosk)."""
import fnmatch

from django.db.models import Max

from hmi.engine import audit, cmd, need, notify
from plant import perms
from plant.models import Machine

from .models import Ack, Instruction, Media, Revision

MAX_STEPS = 80
MAX_PARAMS = 40


def _instruction(p, lock=False):
    qs = Instruction.objects.select_for_update() if lock else Instruction.objects
    x = qs.filter(pk=p.get("id") or p.get("instruction") or 0).first()
    need(x, "That work instruction is no longer on file.")
    return x


def _meta(x, p):
    title = " ".join(str(p.get("title") or "").split())[:160]
    need(title, "Give it a title, e.g. P7 pleaters changeover for MG filters.")
    x.title = title
    x.kind = p.get("kind") if p.get("kind") in dict(Instruction.KINDS) else "changeover"
    pats = [s.strip().upper() for s in str(p.get("parts") or "").replace("\n", ",").split(",") if s.strip()]
    need(all(len(s) <= 40 for s in pats), "Part numbers: 40 characters each at most.")
    x.parts = ", ".join(dict.fromkeys(pats))[:400]
    if "condition" in p:   # Show when: a condition on the filter's data (checked here, worked out per part)
        from .formula import FormulaError, check
        cond = " ".join(str(p.get("condition") or "").split())[:400]
        if cond:
            try:
                check(cond)
            except FormulaError as e:
                need(False, f"Show when: {e}")
        x.condition = cond
    if "exclude" in p:   # Not for filters where: the same kind of condition, the other way
        from .formula import FormulaError, check
        exc = " ".join(str(p.get("exclude") or "").split())[:400]
        if exc:
            try:
                check(exc)
            except FormulaError as e:
                need(False, f"Not for filters where: {e}")
        x.exclude = exc
    if "except_parts" in p:
        ex = [s.strip().upper() for s in str(p.get("except_parts") or "").replace("\n", ",").split(",") if s.strip()]
        need(all(len(s) <= 40 for s in ex), "Except parts: 40 characters each at most.")
        x.except_parts = ", ".join(dict.fromkeys(ex))[:400]
    if "see_also" in p:
        docs = [d.strip().upper() for d in str(p.get("see_also") or "").replace(";", ",").replace("\n", ",").split(",") if d.strip()]
        docs = list(dict.fromkeys(d for d in docs if d != x.doc_no))
        unknown = [d for d in docs if not Instruction.objects.filter(doc_no=d).exists()]
        need(not unknown, f"Also read: there's no {', '.join(unknown)} (use the WI numbers on the list, e.g. WI-004).")
        x.see_also = ", ".join(docs)[:200]
    need(x.kind != "note" or x.condition or x.parts, "A special note card needs a Show when condition (or the parts it's for), "
         "or it would show on every changeover.")


def _machines(x, p):
    ids = p.get("machines")
    ids = ids if isinstance(ids, list) else [i for i in str(ids or "").split(",") if i]
    ms = list(Machine.objects.filter(pk__in=[int(i) for i in ids if str(i).isdigit()]))
    x.machines.set(ms)
    return ms


@cmd("wiCreate", "wi.edit")
def wi_create(p, ctx):
    n = (Instruction.objects.count() or 0) + 1
    while Instruction.objects.filter(doc_no=f"WI-{n:03d}").exists():
        n += 1
    x = Instruction(doc_no=f"WI-{n:03d}", created=ctx.now, created_by=ctx.by)
    _meta(x, p)
    x.save()
    _machines(x, p)
    Revision.objects.create(instruction=x, rev=1, author=ctx.by, created=ctx.now, updated=ctx.now, change_note="First issue")
    audit(ctx, f"Work instructions: {x} created (draft rev 1)")
    return {"msg": f"{x.doc_no} created as a draft. Add its steps, then have it approved.", "data": {"id": x.pk}}


@cmd("wiMeta", "wi.edit")
def wi_meta(p, ctx):
    """Title, kind, machines and parts it's for, and whether it's in use. Kept in the audit log."""
    x = _instruction(p, lock=True)
    before = (f"{x.title} · {x.kind} · {x.parts or 'every part'} · {', '.join(m.name for m in x.machines.all()) or 'every machine'}"
              f"{' · when ' + x.condition if x.condition else ''}{' · not when ' + x.exclude if x.exclude else ''}"
              f"{' · except ' + x.except_parts if x.except_parts else ''}{' · also read ' + x.see_also if x.see_also else ''}")
    _meta(x, p)
    if "active" in p:
        x.active = str(p.get("active")) in ("1", "true", "on")
    x.save()
    ms = _machines(x, p)
    after = (f"{x.title} · {x.kind} · {x.parts or 'every part'} · {', '.join(m.name for m in ms) or 'every machine'}"
             f"{' · when ' + x.condition if x.condition else ''}{' · not when ' + x.exclude if x.exclude else ''}"
             f"{' · except ' + x.except_parts if x.except_parts else ''}{' · also read ' + x.see_also if x.see_also else ''}")
    audit(ctx, f"Work instructions: {x.doc_no} details changed: {before} → {after}" + ("" if x.active else " (withdrawn)"))
    return {"msg": "Saved."}


@cmd("wiDraft", "wi.edit")
def wi_draft(p, ctx):
    """Start a new revision from the approved one (its steps, photos and parameters), to change."""
    x = _instruction(p, lock=True)
    need(not x.draft(), f"{x.doc_no} already has a draft (rev {x.draft().rev if x.draft() else ''}): change that one.")
    cur = x.current()
    rev = (x.revisions.aggregate(n=Max("rev"))["n"] or 0) + 1
    Revision.objects.create(instruction=x, rev=rev, author=ctx.by, created=ctx.now, updated=ctx.now,
                            steps=list(cur.steps) if cur else [], params=list(cur.params) if cur else [])
    audit(ctx, f"Work instructions: {x.doc_no} draft rev {rev} started")
    return {"msg": f"Draft rev {rev} started from rev {cur.rev if cur else '—'}."}


def _clean_steps(raw, x, param_names=None):
    """Each step: its text, caution and photos. A {…} in the text must be one of the parameters (or a formula)."""
    from .formula import FormulaError, check_text
    ok_media = set(Media.objects.filter(instruction=x).values_list("pk", flat=True))
    out = []
    for s in raw if isinstance(raw, list) else []:
        text = str((s or {}).get("text") or "").strip()[:4000]
        caution = str((s or {}).get("caution") or "").strip()[:500]
        media = [int(m) for m in ((s or {}).get("media") or []) if str(m).isdigit() and int(m) in ok_media]
        if text or media:
            if param_names is not None:
                try:
                    check_text(text, param_names)
                    check_text(caution, param_names)
                except FormulaError as e:
                    need(False, f"Step {len(out) + 1}: {e}")
            out.append({"text": text, "caution": caution, "media": media})
    need(len(out) <= MAX_STEPS, f"{MAX_STEPS} steps at most.")
    return out


def _clean_params(raw):
    """Each parameter: a name, what it is in words (desc), and target / min / max as numbers or formulas using the
    filter's data ([Pleat height (mm)] + 1; instructions/formula.py)."""
    from .formula import FormulaError, check, is_formula
    out = []
    for r in raw if isinstance(raw, list) else []:
        name = str((r or {}).get("name") or "").strip()[:80]
        if not name:
            continue
        row = {"name": name, "unit": str(r.get("unit") or "").strip()[:20], "desc": " ".join(str(r.get("desc") or "").split())[:200]}
        for k in ("target", "min", "max"):
            v = str(r.get(k) or "").strip()[:200]
            if v and is_formula(v):
                try:
                    check(v)
                except FormulaError as e:
                    need(False, f"{name}, {k}: {e}")
            row[k] = v
        num = {k: float(row[k]) for k in ("target", "min", "max") if row.get(k) and not is_formula(row[k])}
        if "min" in num and "max" in num:
            need(num["min"] <= num["max"], f"{name}: the minimum is above the maximum.")
        if "target" in num and "min" in num:
            need(num["target"] >= num["min"], f"{name}: the target is below the minimum.")
        if "target" in num and "max" in num:
            need(num["target"] <= num["max"], f"{name}: the target is above the maximum.")
        out.append(row)
    need(len(out) <= MAX_PARAMS, f"{MAX_PARAMS} parameters at most.")
    return out


@cmd("wiSave", "wi.edit")
def wi_save(p, ctx):
    """Save the draft's steps, parameters and change note. Only a draft can change."""
    x = _instruction(p, lock=True)
    r = x.draft()
    need(r, f"{x.doc_no} has no draft. Start one (New revision) to change it.")
    r.params = _clean_params(p.get("params"))
    r.steps = _clean_steps(p.get("steps"), x, [q["name"] for q in r.params])
    r.change_note = str(p.get("change_note") or "").strip()[:4000]
    r.updated = ctx.now
    if ctx.by not in r.author.split(", "):
        r.author = f"{r.author}, {ctx.by}"[:120]
    r.save()
    return {"msg": f"Draft rev {r.rev} saved."}


@cmd("wiSaveAll", "wi.edit")
def wi_save_all(p, ctx):
    """The editor's one Save: its details (title, kind, machines, parts, conditions) and the draft together, both or
    neither (one command: an error in either changes nothing)."""
    wi_meta(p, ctx)
    r = wi_save(p, ctx)
    return {"msg": r.get("msg") or "Saved."}


@cmd("wiDiscard", "wi.edit")
def wi_discard(p, ctx):
    x = _instruction(p, lock=True)
    r = x.draft()
    need(r, "There's no draft to discard.")
    need(x.revisions.count() > 1, "This is its only revision: withdraw the instruction instead (Details › In use).")
    audit(ctx, f"Work instructions: {x.doc_no} draft rev {r.rev} discarded")
    r.delete()
    return {"msg": "Draft discarded."}


@cmd("wiApprove", "wi.approve")
def wi_approve(p, ctx):
    """Approve the draft: it's the one the kiosks show from now on, and the one before it becomes obsolete."""
    x = _instruction(p, lock=True)
    r = x.draft()
    need(r, "There's no draft to approve.")
    need(r.steps, "Add the steps first.")
    need(r.change_note.strip(), "Say what changed and why (the change note) before approving.")
    authors = [a.strip() for a in r.author.split(",")]
    need(ctx.by not in authors or (ctx.user and ctx.user.role == "admin"),
         "Someone other than its author approves a revision (an Admin can approve their own).")
    old = list(x.revisions.filter(status="approved"))
    for o in old:
        o.status, o.obsolete_at = "obsolete", ctx.now
        o.save(update_fields=["status", "obsolete_at"])
    r.status, r.approved_by, r.approved_at = "approved", ctx.by, ctx.now
    r.save(update_fields=["status", "approved_by", "approved_at"])
    lines = [m.name for m in x.machines.all()] or None
    notify(f"Work instruction {x.doc_no} {x.title} rev {r.rev} is in effect: read it on the Instructions page.", "quality",
           **({"lines": lines} if lines else {}), go="instructions")
    audit(ctx, f"Work instructions: {x.doc_no} rev {r.rev} approved" + (f" (rev {', '.join(str(o.rev) for o in old)} obsolete)" if old else ""))
    return {"msg": f"{x.doc_no} rev {r.rev} approved: the kiosks show it now."}


@cmd("wiAck")
def wi_ack(p, ctx):
    """Read and understood, by the person signed in."""
    need(ctx.user, "Sign in first.")
    r = Revision.objects.filter(pk=p.get("rev") or 0, status="approved").select_related("instruction").first()
    need(r, "That revision isn't the approved one any more: open it again.")
    Ack.objects.get_or_create(revision=r, who=ctx.user.name, defaults={"person": ctx.user, "machine": (ctx.line or "")[:60], "t": ctx.now})
    return {"msg": f"{r.label}: read and understood."}


def matches(x, machine_id, part):
    """Is this instruction for this machine and part?"""
    ms = {m.pk for m in x.machines.all()}
    if ms and machine_id not in ms:
        return False
    p = (part or "").upper()
    if p and any(fnmatch.fnmatchcase(p, pat) for pat in x.except_patterns()):
        return False
    pats = x.part_patterns()
    if not pats:
        return True
    return bool(p) and any(fnmatch.fnmatchcase(p, pat) for pat in pats)


class _Vals:
    """The filter's data for conditions, read once per part and only when an instruction has a condition."""
    def __init__(self, part):
        self.part, self._v = part, None

    def get(self):
        if self._v is None:
            from .formula import values_for
            self._v = values_for(self.part) if self.part else {}
        return self._v


def applies(x, part, vals):
    """Its Show when and Not for filters where conditions for this filter: (True / False / None when it can't be
    checked, what's missing). An exclusion that can't be checked doesn't hide it: it shows, marked unchecked."""
    if not (x.condition or x.exclude):
        return True, []
    from .formula import condition
    ok, missing, err = condition(x.condition, vals.get()) if x.condition else (True, [], "")
    if ok is False:
        return False, []
    missing = list(missing or ([err] if err else []))
    if x.exclude:
        ex, exmiss, exerr = condition(x.exclude, vals.get())
        if ex is True:
            return False, []
        if ex is None:
            missing += [m for m in (exmiss or [exerr]) if m and m not in missing]
            return None, missing
    return ok, missing


def for_station(machine, part, who=None):
    """The approved instructions for a machine and part, each with whether this person has read its revision. One
    whose Show when condition can't be checked for the filter (its data isn't on file) is listed, marked unchecked."""
    out = []
    vals = _Vals(part)
    for x in Instruction.objects.filter(active=True).prefetch_related("machines"):
        if not matches(x, machine.pk if machine else None, part):
            continue
        ok, missing = applies(x, part, vals)
        if ok is False:
            continue
        r = x.current()
        if not r:
            continue
        read = bool(who) and Ack.objects.filter(revision=r, who=who).exists()
        out.append({"x": x, "r": r, "read": read, "unchecked": missing if ok is None else []})
    order = {k: i for i, (k, _) in enumerate(Instruction.KINDS)}
    return sorted(out, key=lambda d: (d["read"], order.get(d["x"].kind, 9), d["x"].doc_no))


@cmd("wiImportSaved", "wi.edit")
def wi_import_saved(p, ctx):
    """The changeover steps and settings last read from the asset database (kept for when it was unreachable), as
    draft Changeover instructions, one per machine and part, to check and approve. Skips any already imported."""
    from hmi.models import ChangeoverCache
    from plant.models import Machine as M
    n = skipped = 0
    for c in ChangeoverCache.objects.order_by("machine", "part"):
        title = f"Changeover: {c.part} on {c.machine}"[:160]
        if Instruction.objects.filter(title=title).exists() or not (c.steps or c.settings):
            skipped += 1
            continue
        k = (Instruction.objects.count() or 0) + 1
        while Instruction.objects.filter(doc_no=f"WI-{k:03d}").exists():
            k += 1
        x = Instruction.objects.create(doc_no=f"WI-{k:03d}", title=title, kind="changeover", parts=c.part[:400], created=ctx.now, created_by=ctx.by)
        m = M.objects.filter(name=c.machine).first()
        if m:
            x.machines.set([m])
        steps = []
        for s in c.steps or []:
            text = str(s.get("text") or "").strip()
            extra = " ".join(t for t in (f"{s.get('setting') or ''} {s.get('value') or ''}{s.get('unit') or ''}".strip(), str(s.get("asset") or "").strip()) if t)
            detail = str(s.get("detail") or "").strip()
            body = "\n".join(t for t in (text + (f" ({extra})" if extra else ""), detail) if t)
            if body:
                steps.append({"text": body[:4000], "caution": "", "media": []})
        params = []
        for s in c.settings or []:
            val = str(s.get("value") or "").strip()
            try:
                float(val)
                tgt, desc = val, str(s.get("asset") or "")
            except ValueError:   # text settings (a blade, a die) go in the description
                tgt, desc = "", " ".join(t for t in (val, str(s.get("asset") or "")) if t)
            if str(s.get("name") or "").strip():
                params.append({"name": str(s["name"])[:80], "desc": desc[:200], "target": tgt, "min": "", "max": "", "unit": str(s.get("unit") or "")[:20]})
        Revision.objects.create(instruction=x, rev=1, author=ctx.by, created=ctx.now, updated=ctx.now, steps=steps[:MAX_STEPS], params=params[:MAX_PARAMS],
                                change_note="Imported from the steps last read from the asset database")
        n += 1
    audit(ctx, f"Work instructions: {n} changeover draft(s) imported from the saved asset-database steps")
    return {"msg": f"{n} draft changeover instruction{'s' if n != 1 else ''} made" + (f" ({skipped} already there or empty)" if skipped else "")
            + ". Check each one and have it approved: until then the kiosks use the standard checklist."}


CO_KINDS = {False: ("changeover",), True: ("inkjet",)}


def _specific(x, part):
    """How closely an instruction is aimed at a machine and part, highest first: the exact part number, then the
    longest matching pattern (MG4062* before MG*), then a named machine before every machine."""
    p = (part or "").upper()
    pats = x.part_patterns()
    exact = p in pats
    longest = max((len(pat.replace("*", "").replace("?", "")) for pat in pats if fnmatch.fnmatchcase(p, pat)), default=0)
    return (1 if exact else 0, longest, 1 if x.machines.all() else 0)


def changeover_for(machine, part, inkjet=False):
    """The approved changeover instruction for changing this machine over to this part (Inkjet change instructions
    for an inkjet-only change): the one aimed most closely at them, which replaces the more general ones (they are
    not combined). None if none."""
    kinds = CO_KINDS[bool(inkjet)]
    best = None
    vals = _Vals(part)
    for x in Instruction.objects.filter(active=True, kind__in=kinds).prefetch_related("machines"):
        if not matches(x, machine.pk if machine else None, part):
            continue
        ok, missing = applies(x, part, vals)
        if ok is False or (ok is None and x.condition and not _exclusion_only(missing, x, vals)):
            continue   # a conditional checklist only when its Show when holds; an unchecked exclusion doesn't hide it
        r = x.current()
        if not r or not r.steps:
            continue
        key = (tuple(-n for n in _specific(x, part)), kinds.index(x.kind), x.doc_no)
        if best is None or key < best[0]:
            best = (key, x, r)
    return (best[1], best[2]) if best else (None, None)


def _exclusion_only(missing, x, vals):
    """True when only the exclusion couldn't be checked (its Show when held)."""
    from .formula import condition
    return condition(x.condition, vals.get())[0] is True


def notes_for(machine, part):
    """Special note cards for a changeover to this part: approved, for this machine, and the filter meets the Show
    when condition. One whose condition can't be checked (the filter's data isn't on file) is shown too, marked, so a
    note is never missed for want of data. [(x, r, missing)]."""
    out = []
    vals = _Vals(part)
    for x in Instruction.objects.filter(active=True, kind="note").prefetch_related("machines"):
        if not matches(x, machine.pk if machine else None, part):
            continue
        ok, missing = applies(x, part, vals)
        r = x.current()
        if ok is False or not r or not r.steps:
            continue
        out.append((x, r, missing if ok is None else []))
    return sorted(out, key=lambda t: t[0].doc_no)


def note_cards(machine, part):
    """The special note cards for a changeover to this part, as checklist items to tick (kept with the changeover)."""
    from .formula import fill_steps
    out = []
    for x, r, missing in notes_for(machine, part):
        filled, _ = fill_steps(r.steps, r.params, part)
        out.append({"text": f"Special note: {x.title}", "media": [m for s in filled for m in s.get("media") or []],
                    "note": {"id": x.pk, "doc": x.doc_no, "title": x.title, "rev": r.pk, "unchecked": missing,
                             "lines": [{"text": s.get("text") or "", "caution": s.get("caution") or ""} for s in filled]}})
    return out


ALSO_KINDS = ("safety", "quality", "operation")


def also_read(machine, part, chosen=None):
    """Instructions to read with a changeover to this part: the ones the changeover instruction names (Also read),
    then the approved Safety, Quality check and Operation instructions for this machine and part.
    [{"id", "doc", "title", "kind", "rev"}]."""
    out, seen = [], set()
    named = chosen.see_also_list() if chosen else []
    for d in named:
        x = Instruction.objects.filter(doc_no=d, active=True).first()
        r = x.current() if x else None
        if x and r and x.pk not in seen:
            seen.add(x.pk)
            out.append({"id": x.pk, "doc": x.doc_no, "title": x.title, "kind": x.get_kind_display(), "rev": r.pk})
    for d in for_station(machine, part):
        x = d["x"]
        if x.kind in ALSO_KINDS and x.pk not in seen:
            seen.add(x.pk)
            out.append({"id": x.pk, "doc": x.doc_no, "title": x.title, "kind": x.get_kind_display(), "rev": d["r"].pk})
    return out


def changeover_checklist(x, r, part=""):
    """An approved revision as the kiosk's changeover steps and settings, its formulas worked out for the part being
    changed to (kept with the changeover, so a revision approved part-way through doesn't change what's ticked off)."""
    from .formula import fill_steps
    filled, resolved = fill_steps(r.steps, r.params, part)
    steps = [{"text": s.get("text") or "", "caution": s.get("caution") or "", "media": list(s.get("media") or [])} for s in filled]
    sets = []
    for p in resolved:
        lo, hi = p["min"], p["max"]
        rng = f"{lo}–{hi}" if lo and hi else f"≥ {lo}" if lo else f"≤ {hi}" if hi else ""
        note = p["desc"]
        if p["missing"]:
            note = (note + " · " if note else "") + "Not on file for this part: " + ", ".join(p["missing"])
        elif p["err"]:
            note = (note + " · " if note else "") + p["err"]
        sets.append({"name": p["name"], "value": p["target"], "range": rng, "unit": p["unit"], "desc": note,
                     "warn": bool(p["missing"] or p["err"]), "formula": " · ".join(f for f in (p["f_target"], p["f_min"], p["f_max"]) if f)})
    return steps, sets, {"id": x.pk, "rev": r.pk, "label": r.label, "title": x.title}


def ack_from_changeover(co, person, machine, t):
    """Finishing a changeover from an instruction counts as reading that revision, and the special note cards
    ticked in it."""
    if not person:
        return
    revs = [((co or {}).get("wi") or {}).get("rev")] + [s["note"]["rev"] for s in (co or {}).get("steps") or [] if s.get("note")]
    for r in Revision.objects.filter(pk__in=[x for x in revs if x], status="approved"):
        Ack.objects.get_or_create(revision=r, who=person.name, defaults={"person": person, "machine": (machine or "")[:60], "t": t})


def can_edit(me):
    return perms.can(me, "wi.edit")
