"""Supervisor › S2K posting: what the floor finished, next to what S2K says, ready to key in or import.

S2K is read-only to the suite: nothing is ever sent to it. This page lines up, per MO with activity in the dates,
what the floor did against the S2K MO it is posted to. S2K keeps one MO per stage of an order (1MG… Stage 1
pleating, 2MG… Stage 2 end bands, the finished part for packaging; plant/mofamily.py), so:

* a finished MO run goes to the stage MO of the machine's stage (a pleater's run to the order's 1MG… MO);
* a completed End Band or foam Glue Bead station job (and an MO run on such a line) goes to the order's 2MG… MO;
* a packed pallet goes to the order's finished-part (packaging) MO, whatever number was typed on the tag.

Each stage MO is set against S2K's own quantity for it and what S2K last said was still to make, and flagged where
they don't agree. The CSV holds the rows not yet marked as posted; after
entering or importing them in S2K, press "Mark these as posted" so they don't go in twice.
"""
from hmi.models import MoRun
from packkiosk.models import PalletTag
from plant.models import MO, Machine

from .models import S2KPost

STAGE_LABEL = {"s1": "Stage 1", "eb": "Stage 2", "pk": "Packaging"}
# the S2K MO a machine's work is posted to, by the machine's stage: pleaters (Barbara, Salvatore…) to the 1MG/1AQ MO,
# End Band and foam Glue Bead lines to the 2MG/2AQ MO, packaging lines to the finished MG/AQ MO. Arts & Crafts has no
# S2K MO of its own (its filters are already counted at Stage 1 and Stage 2).
POSTS_TO = {"s1": "s1", "eb": "eb", "gb": "eb", "pk": "pk"}


def _order(m):
    return m.parent if m and m.parent_id else m


def _stage_mo(m, stage):
    """The S2K MO of this order for one stage (its own MO when the order has no stage MOs)."""
    o = _order(m)
    if not o:
        return None
    fam = [o, *o.stage_mos.all()]
    if not any(x.stage for x in fam):
        return m
    return next((x for x in fam if x.stage == stage), None)


def own_qty(m):
    """S2K's quantity for this MO itself (an order MO's qty is what Stage 1 makes; its own is kept in stage_qty)."""
    if not m.parent_id and m.stage and m.stage in (m.stage_qty or {}):
        return m.stage_qty[m.stage]
    return m.qty


class _Lookup:
    def __init__(self):
        self.mos, self.stages = {}, {x.name: x.stage for x in Machine.objects.all()}

    def mo(self, number):
        n = str(number or "").strip()
        if n not in self.mos:
            self.mos[n] = (MO.objects.filter(number=n).select_related("parent").first()
                           or MO.objects.filter(base=n.split("-")[0]).select_related("parent").order_by("-rev").first() if n else None)
        return self.mos[n]

    def run_target(self, number, line):
        stage = POSTS_TO.get(self.stages.get(line, "s1"))
        if not stage:
            return None, None   # a line with no S2K MO of its own
        m = self.mo(number)
        return (_stage_mo(m, stage) or m) if m else None, stage

    def pallet_target(self, number):
        m = self.mo(number)
        return (_stage_mo(m, "pk") or m) if m else None


def _family_numbers(m):
    o = _order(m)
    return {x.number for x in [o, *o.stage_mos.all()]} if o else set()


def floor_total(target, stage, look):
    """What the floor has done for this stage MO so far (all dates): good filters made on it, or packed on it."""
    nums = _family_numbers(target)
    if stage == "pk":
        return sum(p.qty for p in PalletTag.objects.filter(mo__in=nums, voided=False) if look.pallet_target(p.mo) == target)
    tot = sum(r.qty - r.scrap for r in MoRun.objects.filter(mo__in=nums) if look.run_target(r.mo, r.line)[0] == target)
    if stage == "eb":
        from scheduler.models import Job
        tot += sum(int(j.wip_qty or 0) for j in Job.objects.filter(mo__number__in=nums, op__in=("eb", "gb"), wip_done__isnull=False))
    return tot


def rows(a, b, line="", only_open=False):
    from scheduler.models import Job
    look = _Lookup()
    runs = MoRun.objects.filter(end_time__gte=a, end_time__lt=b)
    pallets = PalletTag.objects.filter(t__gte=a, t__lt=b, voided=False).exclude(mo="")
    jobs = Job.objects.filter(op__in=("eb", "gb"), wip_done__gte=a, wip_done__lt=b, wip_qty__gt=0).select_related("mo", "machine")
    if line:
        runs, pallets, jobs = runs.filter(line=line), pallets.filter(line=line), jobs.filter(machine__name=line)
    posted = {(k, r) for k, r in S2KPost.objects.values_list("kind", "ref")}
    by = {}

    def group(target, typed, part, stage):
        key = target.pk if target else f"?{typed}"
        if key not in by:
            st = target.stage if target and target.stage else stage
            by[key] = {"mo": target.s2k_number if target else typed, "part": target.part if target else part, "m": target, "stage": st,
                       "stageLabel": STAGE_LABEL.get(st, ""), "entries": []}
        return by[key]

    for r in runs.order_by("end_time"):
        target, stage = look.run_target(r.mo, r.line)
        if not stage:
            continue
        x = group(target, r.mo, r.item, stage)
        x["entries"].append({"kind": "run", "label": "MO run", "id": r.pk, "line": r.line, "t": r.end_time, "qty": r.qty - r.scrap, "scrap": r.scrap,
                             "no": "", "typed": r.mo, "posted": ("run", r.pk) in posted})
    for j in jobs.order_by("wip_done"):
        target = _stage_mo(j.mo, "eb") or j.mo
        x = group(target, j.mo.number, j.mo.part, "eb")
        x["entries"].append({"kind": "eb", "label": "Glue bead" if j.op == "gb" else "End bands", "id": j.pk, "line": j.machine.name, "t": j.wip_done, "qty": int(j.wip_qty), "scrap": 0,
                             "no": "", "typed": j.mo.number, "posted": ("eb", j.pk) in posted})
    for p in pallets.order_by("t"):
        target = look.pallet_target(p.mo)
        x = group(target, p.mo, p.part, "pk")
        x["entries"].append({"kind": "pallet", "label": "Pallet", "id": p.pk, "line": p.line, "t": p.t, "qty": p.qty, "scrap": 0,
                             "no": p.pallet_no, "typed": p.mo, "posted": ("pallet", p.pk) in posted})
    out = []
    for x in sorted(by.values(), key=lambda v: (str(v["mo"]), v["stage"])):
        m, flags = x["m"], []
        x["inDates"] = sum(e["qty"] for e in x["entries"])
        x["scrapSum"] = sum(e["scrap"] for e in x["entries"])
        x["floor"] = None
        if m:
            x["own"] = own_qty(m)
            x["floor"] = floor_total(m, x["stage"], look)
            if m.remaining is not None:
                s2k_done = max(0, x["own"] - m.remaining)
                if x["floor"] != s2k_done:
                    flags.append(f"S2K shows {s2k_done:,} done, the floor {x['floor']:,} ({x['floor'] - s2k_done:+,} to post)")
            if x["stage"] == "pk":
                o = _order(m)
                s1 = _stage_mo(m, "s1")
                if s1 and s1 != m:
                    q = o.stage_qty or {}
                    ratio = (q.get("pk") / q.get("s1")) if q.get("pk") and q.get("s1") else 1
                    made = floor_total(s1, "s1", look)
                    if x["floor"] > made * ratio + 0.5:
                        flags.append(f"{x['floor'] - round(made * ratio):,} more packed than Stage 1 made good ({made:,} on {s1.s2k_number})")
            if _order(m).status == "CLOSE" and m.remaining:
                flags.append("closed on the floor but S2K still has some to make")
            odd = sorted({e["typed"] for e in x["entries"] if e["kind"] == "pallet"} - {m.number, m.s2k_number, m.base})
            if odd:
                flags.append(f"pallet tags say MO {', '.join(odd)}: posted here, to the packaging MO")
        else:
            flags.append("not an MO the suite has from S2K")
        x["flags"] = flags
        x["unposted"] = sum(1 for e in x["entries"] if not e["posted"])
        if only_open and not x["unposted"]:
            continue
        out.append(x)
    return out


def csv_rows(R):
    yield ["Type", "Stage", "S2K MO", "Part", "Line", "When", "Good qty", "Scrap", "Pallet #", "Suite id"]
    for x in R:
        for e in x["entries"]:
            if not e["posted"]:
                yield [e["label"], x["stageLabel"], x["mo"], x["part"], e["line"], e["t"].isoformat(timespec="minutes"), e["qty"],
                       e["scrap"] if e["kind"] == "run" else "", e["no"], f"{e['kind']}-{e['id']}"]


def mark(R, by, now):
    n = 0
    for x in R:
        for e in x["entries"]:
            if not e["posted"]:
                _, made = S2KPost.objects.get_or_create(kind=e["kind"], ref=e["id"], defaults={"mo": x["mo"], "qty": e["qty"], "t": now, "by": by})
                n += made
    return n
