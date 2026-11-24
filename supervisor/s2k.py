"""Supervisor › S2K posting: what the floor finished, next to what S2K says, ready to key in or import.

S2K is read-only to the suite: nothing is ever sent to it. This page lines up, per MO with activity in the dates,
the MO runs that finished (good and scrap) and the pallets packed against S2K's own order quantity and what S2K last
said was still to make, and flags where they don't agree. The CSV holds the rows not yet marked as posted; after
entering or importing them in S2K, press "Mark these as posted" so they don't go in twice.
"""
from django.db.models import Sum

from hmi.models import MoRun
from packkiosk.models import PalletTag
from plant.models import MO

from .models import S2KPost


def rows(a, b, line="", only_open=False):
    runs = MoRun.objects.filter(end_time__gte=a, end_time__lt=b)
    pallets = PalletTag.objects.filter(t__gte=a, t__lt=b, voided=False).exclude(mo="")
    if line:
        runs, pallets = runs.filter(line=line), pallets.filter(line=line)
    posted = {(k, r) for k, r in S2KPost.objects.values_list("kind", "ref")}
    by = {}
    for r in runs.order_by("end_time"):
        x = by.setdefault(r.mo, {"mo": r.mo, "part": r.item, "runs": [], "pallets": []})
        x["runs"].append({"id": r.pk, "line": r.line, "end": r.end_time, "qty": r.qty, "scrap": r.scrap, "posted": ("run", r.pk) in posted})
    for p in pallets.order_by("t"):
        x = by.setdefault(p.mo, {"mo": p.mo, "part": p.part, "runs": [], "pallets": []})
        x["pallets"].append({"id": p.pk, "no": p.pallet_no, "line": p.line, "t": p.t, "qty": p.qty, "posted": ("pallet", p.pk) in posted})
    mos = {m.number: m for m in MO.objects.filter(number__in=list(by))}
    out = []
    for num, x in sorted(by.items()):
        m = mos.get(num)
        good = sum(r["qty"] - r["scrap"] for r in x["runs"])
        packed_all = PalletTag.objects.filter(mo=num, voided=False).aggregate(q=Sum("qty"))["q"] or 0
        flags = []
        if m:
            net = max(0, m.produced - m.scrap)
            if packed_all > net:
                flags.append(f"{packed_all - net:,} more packed than the line made good")
            if m.remaining is not None:
                s2k_done = max(0, m.qty - m.remaining)
                if abs(net - s2k_done) >= 1:
                    flags.append(f"S2K shows {s2k_done:,} made, the floor {net:,} ({net - s2k_done:+,} to post)")
            if m.status == "CLOSE" and m.remaining:
                flags.append("closed on the floor but S2K still has some to make")
        else:
            flags.append("not an MO the suite has from S2K")
        x.update(m=m, good=good, scrapSum=sum(r["scrap"] for r in x["runs"]), packed=sum(p["qty"] for p in x["pallets"]), packedAll=packed_all,
                 flags=flags, unposted=sum(1 for r in x["runs"] if not r["posted"]) + sum(1 for p in x["pallets"] if not p["posted"]))
        if only_open and not x["unposted"]:
            continue
        out.append(x)
    return out


def csv_rows(R):
    yield ["Type", "MO", "Part", "Line", "When", "Good qty", "Scrap", "Pallet #", "Suite id"]
    for x in R:
        for r in x["runs"]:
            if not r["posted"]:
                yield ["MO run", x["mo"], x["part"], r["line"], r["end"].isoformat(timespec="minutes"), r["qty"] - r["scrap"], r["scrap"], "", f"run-{r['id']}"]
        for p in x["pallets"]:
            if not p["posted"]:
                yield ["Pallet", x["mo"], x["part"], p["line"], p["t"].isoformat(timespec="minutes"), p["qty"], "", p["no"], f"pallet-{p['id']}"]


def mark(R, by, now):
    n = 0
    for x in R:
        for r in x["runs"]:
            if not r["posted"]:
                _, made = S2KPost.objects.get_or_create(kind="run", ref=r["id"], defaults={"mo": x["mo"], "qty": r["qty"] - r["scrap"], "t": now, "by": by})
                n += made
        for p in x["pallets"]:
            if not p["posted"]:
                _, made = S2KPost.objects.get_or_create(kind="pallet", ref=p["id"], defaults={"mo": x["mo"], "qty": p["qty"], "t": now, "by": by})
                n += made
    return n
