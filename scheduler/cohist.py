"""Changeovers as they really went: the Changeover report (Supervisor › Changeovers) and historical changeover times
for the Scheduler.

A changeover can be worked in pieces (a shift change, a pass-over): each piece is a ChangeoverPiece with who worked
it, for how long and which steps they checked off (and, from 1.9.10, when each step was checked off). A changeover is
all pieces with the same id; it took from its first piece's start to its last piece's end. The time in pieces with
nobody logged in is the handoff wait.

Historical estimates (Scheduler › Lines & Shifts › "Use how long changeovers really took"): for each line and each
part-to-part change with at least MIN_N finished changeovers in the last DAYS days, the median of how long they took
is used instead of the line's flat changeover time.
"""
from collections import defaultdict
from datetime import timedelta
from statistics import median

from django.utils import timezone

MIN_N, DAYS = 3, 180


def changeovers(a, b, line=""):
    """One row per changeover started in [a, b): line, from MO/part, to MO/part, start, end, minutes, wait, pieces, people."""
    from hmi.models import ChangeoverPiece
    from plant.models import MO
    qs = ChangeoverPiece.objects.filter(start__gte=a - timedelta(days=2), start__lt=b)
    if line:
        qs = qs.filter(line=line)
    by = defaultdict(list)
    for p in qs.order_by("start", "id"):
        by[p.co_id].append(p)
    nums = {p.old_mo for ps in by.values() for p in ps} | {p.new_mo for ps in by.values() for p in ps}
    part = dict(MO.objects.filter(number__in=[n for n in nums if n]).values_list("number", "part"))
    out = []
    for cid, ps in by.items():
        s, e = ps[0].start, max(p.end for p in ps)
        if not (a <= s < b):
            continue
        old = next((p.old_mo for p in ps if p.old_mo), "")
        new = next((p.new_mo for p in reversed(ps) if p.new_mo), "")
        out.append({"id": cid, "line": ps[0].line, "oldMo": old, "newMo": new, "from": part.get(old, ""), "to": part.get(new, ""),
                    "start": s, "end": e, "min": sum(p.minutes for p in ps), "wait": sum(p.minutes for p in ps if not p.by),
                    "pieces": len(ps), "people": sorted({p.by for p in ps if p.by}), "finished": any(p.ended == "finished" for p in ps),
                    "steps": [st for p in ps for st in (p.step_t or [])]})
    return out


def report(a, b, line=""):
    """Changeover report: per line actual against its target (Admin › Machines › changeover time), the handoff wait,
    the slowest steps, and part-to-part averages."""
    from plant.models import Machine
    target = {m.name: float(m.changeover_min or 0) for m in Machine.objects.filter(stage="s1")}
    cos = changeovers(a, b, line)
    lines = defaultdict(list)
    for c in cos:
        lines[c["line"]].append(c)
    per = []
    for ln, cs in sorted(lines.items()):
        done = [c for c in cs if c["finished"]]
        mins = [c["min"] for c in done]
        t = target.get(ln) or 0
        per.append({"line": ln, "n": len(cs), "done": len(done), "avg": round(sum(mins) / len(mins), 1) if mins else None,
                    "median": median(mins) if mins else None, "target": t, "over": sum(1 for m in mins if t and m > t),
                    "wait": sum(c["wait"] for c in cs), "longest": max(mins) if mins else None})
    # slowest steps: the time from the step before (or the start) to each step's check-off
    gaps = defaultdict(list)
    for c in cos:
        prev = c["start"].timestamp() * 1000
        for text, t in sorted(c["steps"], key=lambda x: x[1]):
            if t >= prev:
                gaps[(c["line"], text)].append((t - prev) / 60000)
            prev = max(prev, t)
    steps = sorted(({"line": ln, "step": text, "n": len(v), "avg": round(sum(v) / len(v), 1), "max": round(max(v), 1)}
                    for (ln, text), v in gaps.items()), key=lambda x: -x["avg"])[:15]
    pairs = defaultdict(list)
    for c in cos:
        if c["finished"] and c["from"] and c["to"]:
            pairs[(c["line"], c["from"], c["to"])].append(c["min"])
    trans = sorted(({"line": k[0], "from": k[1], "to": k[2], "n": len(v), "avg": round(sum(v) / len(v), 1), "median": median(v)}
                    for k, v in pairs.items()), key=lambda x: (-x["n"], x["line"]))[:30]
    return {"lines": per, "steps": steps, "pairs": trans, "list": cos}


def estimates(now=None):
    """{line: {(FROM_PART, TO_PART): minutes}} from the last DAYS days (median, at least MIN_N finished changeovers)."""
    now = now or timezone.now()
    pairs = defaultdict(list)
    for c in changeovers(now - timedelta(days=DAYS), now):
        if c["finished"] and c["from"] and c["to"]:
            pairs[(c["line"], c["from"].upper(), c["to"].upper())].append(c["min"])
    out = defaultdict(dict)
    for (ln, a, b), v in pairs.items():
        if len(v) >= MIN_N:
            out[ln][(a, b)] = float(median(v))
    return dict(out)
