"""Scheduler › Suggest order: a better run order for one Stage 1 line, shown before anything changes.

Runs of the same part go together (no changeover), then runs of the same base part (an inkjet-only changeover,
Lines & Shifts › Same base part), while due dates still decide which group goes first: groups go in order of their
earliest due date, and inside a group the earliest due goes first. The run on the machine now stays first.

The suggestion is worked out on the real schedule (the same calendar, shifts, breaks and speeds as the board) and
shows how many changeover minutes it saves and how many runs would finish late before and after. It is only offered
when it saves time without making more runs late. Nothing changes until the planner presses Apply.
"""
from plant.models import inkjet_only

from .engine import compute


def _line_jobs(world, sch, mid):
    out = []
    for k, j in world["jobs"].items():
        if j.get("op") != "s1" or j.get("machineId") != mid or int(j.get("part") or 1) != 1:
            continue
        r = sch["res"].get(k)
        if not r or r.get("unplanned"):
            continue
        out.append((k, j, r))
    out.sort(key=lambda x: x[2]["start"])
    return out


def _cost(sch, keys):
    co = sum(float((sch["res"].get(k) or {}).get("coMin") or 0) for k in keys)
    late = sum(1 for k in keys if (sch["res"].get(k) or {}).get("late"))
    end = max([(sch["res"].get(k) or {}).get("end") or 0 for k in keys] or [0])
    return co, late, end


def suggest(world, mid):
    sch = compute(world)
    rows = _line_jobs(world, sch, mid)
    if len(rows) < 3:
        return {"ok": False, "why": "Fewer than 3 runs on this line: nothing to reorder."}
    mos, bases = world["mos"], world["cfg"].get("inkjetBases")
    fixed = [x for x in rows if x[2].get("running")]
    free = [x for x in rows if not x[2].get("running")]
    big = 10 ** 15

    def due(x):
        return (mos.get(x[1]["moId"]) or {}).get("due") or big

    def part(x):
        return str((mos.get(x[1]["moId"]) or {}).get("part") or "").upper()
    groups = []
    for x in sorted(free, key=due):
        g = next((g for g in groups if part(g[0]) == part(x) or inkjet_only(part(g[0]), part(x), bases)), None)
        if g is None:
            groups.append([x])
        else:
            g.append(x)
    groups.sort(key=lambda g: min(due(x) for x in g))
    order = []
    for g in groups:
        by_part = {}
        for x in g:
            by_part.setdefault(part(x), []).append(x)
        for p in sorted(by_part, key=lambda p: min(due(x) for x in by_part[p])):
            order += sorted(by_part[p], key=due)
    keys_now = [k for k, _, _ in rows]
    keys_new = [k for k, _, _ in fixed] + [k for k, _, _ in order]
    if keys_new == keys_now:
        return {"ok": False, "why": "This line is already in the best order the suggestion knows."}
    base = min(r["start"] for _, _, r in free)
    jobs = {k: dict(v) for k, v in world["jobs"].items()}
    for i, (k, _, _) in enumerate(order):
        jobs[k]["reqStart"] = base + i     # one after another from the first free run's start
    new = compute(world, jobs)
    co0, late0, end0 = _cost(sch, keys_now)
    co1, late1, end1 = _cost(new, keys_new)
    rowjson = lambda k, s: {"key": k, "mo": (mos.get(world["jobs"][k]["moId"]) or {}).get("mo"),   # noqa: E731
                            "part": (mos.get(world["jobs"][k]["moId"]) or {}).get("part"), "due": (mos.get(world["jobs"][k]["moId"]) or {}).get("due"),
                            "start": (s["res"].get(k) or {}).get("start"), "coMin": (s["res"].get(k) or {}).get("coMin"),
                            "late": bool((s["res"].get(k) or {}).get("late")), "running": bool((s["res"].get(k) or {}).get("running"))}
    out = {"ok": True, "now": [rowjson(k, sch) for k in keys_now], "new": [rowjson(k, new) for k in keys_new],
           "coBefore": round(co0), "coAfter": round(co1), "saved": round(co0 - co1), "lateBefore": late0, "lateAfter": late1,
           "endBefore": end0, "endAfter": end1, "base": base, "keys": [k for k, _, _ in order]}
    if co1 >= co0 - 0.5:
        out.update(ok=False, why="Reordering wouldn't save changeover time on this line.")
    elif late1 > late0:
        out.update(ok=False, why=f"Grouping the parts would save {round(co0 - co1)} min of changeovers but make {late1 - late0} more run(s) late, so it isn't suggested.")
    return out
