"""Quality › SPC: measurements over time against the drawing, process capability, and drift warnings.

The exact measurements are already kept: operator QC checks (length, width, pleat height, side band L and R, and the
measurements added in Quality › Tolerances) and machine vision. Here they become an individuals chart (I-MR: the
centre line, control limits at the mean ± 2.66 × the average moving range, and the drawing's limits), Cp and Cpk per
machine and part (from the within-process spread, average moving range / 1.128) with Pp and Ppk (from the overall
standard deviation) beside them, and warnings that catch a line drifting before it makes scrap:

- near the limit: a measurement still in spec but past 80% of the tolerance on its side
- trending: 3 measurements in a row each further from the drawing, on the same side
- one side: 7 measurements in a row on the same side of the drawing
- out of control: a measurement outside the control limits (the process changed, even if still in spec)

The warnings are checked each time a measurement comes in (a QC check is saved, or machine vision sends one) and
show on that line's kiosk and in Notices; each kind is told at most once an hour per line, part and measurement.
"""
import math
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from hmi import qc

MEASURES = [("len", "Length", "len"), ("wid", "Width", "wid"), ("ph", "Pleat height", "ph"), ("sbl", "Side band L", "sb"), ("sbr", "Side band R", "sb")]
NEAR = 0.8
TREND, RUN = 3, 7
D2 = 2.66   # 3 / d2 (1.128) for a moving range of 2: individuals chart limits


def measures(cfg=None):
    return MEASURES + [(f["key"], f["label"], f["key"]) for f in qc.features(cfg)]


def _spec_of(key):
    return next((s for k, _, s in measures() if k == key), None)


def series(line, part, key, a, b, limit=500):
    """[(t, value, source)], oldest first: operator QC checks and machine vision for this line and part."""
    from hmi.models import AutoQC, QCCheck
    from plant.models import part_key
    out = []
    pk = part_key(part)
    builtin = key in {k for k, _, _ in MEASURES}
    qs = QCCheck.objects.filter(t__gte=a, t__lt=b, line=line)
    for c in qs.order_by("-t")[:limit * 2]:
        if part_key(c.item) != pk:
            continue
        v = getattr(c, key) if builtin else (c.values or {}).get(key)
        if isinstance(v, (int, float)):
            out.append((c.t, float(v), "QC"))
    if builtin:
        for c in AutoQC.objects.filter(t__gte=a, t__lt=b, line=line).order_by("-t")[:limit * 2]:
            v = getattr(c, key)
            if part_key(c.item) == pk and isinstance(v, (int, float)):
                out.append((c.t, float(v), "Vision"))
    out.sort(key=lambda x: x[0])
    return out[-limit:]


def limits(part, key):
    """(target, LSL, USL) from the part's drawing and the tolerances, or None when the drawing has no value."""
    from plant.util import part_spec
    spec = _spec_of(key)
    p = part_spec(part)
    t = p.get(spec) if spec in p else (p.get("extra") or {}).get(spec)
    if t is None:
        return None
    under, over = qc.tol_for(spec, p)
    if not under and not over:
        return None
    return float(t), float(t) - under, float(t) + over


D2_MR = 1.128    # d2 for moving ranges of two: sigma within = average moving range / 1.128
CAP_MIN_N = 25   # fewer measurements than this: capability is shown as preliminary

def stats(vals, lim):
    n = len(vals)
    if n < 2:
        return {"n": n}
    mean = sum(vals) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1))
    mr = [abs(b - a) for a, b in zip(vals, vals[1:])]
    mrbar = sum(mr) / len(mr) if mr else 0
    out = {"n": n, "mean": mean, "sd": sd, "ucl": mean + D2 * mrbar, "lcl": mean - D2 * mrbar, "mr": mrbar,
           "min": min(vals), "max": max(vals)}
    # Cp / Cpk use the within-process spread, the average moving range / d2 (1.128 for ranges of 2): what the process
    # can do when steady. Pp / Ppk use the overall standard deviation: what it actually did, drift included.
    within = mrbar / D2_MR if mrbar else 0
    out["sdWithin"] = within
    out["preliminary"] = n < CAP_MIN_N
    if lim and within > 0:
        _, lsl, usl = lim
        out["cp"] = (usl - lsl) / (6 * within)
        out["cpk"] = min(usl - mean, mean - lsl) / (3 * within)
    if lim and sd > 0:
        _, lsl, usl = lim
        out["pp"] = (usl - lsl) / (6 * sd)
        out["ppk"] = min(usl - mean, mean - lsl) / (3 * sd)
    if lim:
        out["out"] = sum(1 for v in vals if v < lim[1] - qc.EPS or v > lim[2] + qc.EPS)
    return out


def warnings(vals, lim, st=None):
    """The drift rules that the newest measurement sets off: [(rule, sentence)]."""
    out = []
    if not vals or not lim:
        return out
    t, lsl, usl = lim
    v = vals[-1]
    side = 1 if v > t else -1 if v < t else 0
    tol = (usl - t) if side > 0 else (t - lsl)
    if side and lsl - qc.EPS <= v <= usl + qc.EPS and tol > 0 and abs(v - t) > NEAR * tol:
        out.append(("near", f"{v:.3f} is past {round(NEAR * 100)}% of the tolerance ({'over' if side > 0 else 'under'} the drawing)"))
    if len(vals) >= TREND:
        last = vals[-TREND:]
        d = [x - t for x in last]
        if all(x > 0 for x in d) or all(x < 0 for x in d):
            if all(abs(d[i + 1]) > abs(d[i]) for i in range(TREND - 1)):
                out.append(("trend", f"{TREND} measurements in a row each further {'over' if d[-1] > 0 else 'under'} the drawing"))
    if len(vals) >= RUN:
        d = [x - t for x in vals[-RUN:]]
        if all(x > 0 for x in d) or all(x < 0 for x in d):
            out.append(("side", f"{RUN} measurements in a row {'over' if d[-1] > 0 else 'under'} the drawing"))
    st = st or stats(vals[:-1], lim)
    if st.get("n", 0) >= 10 and st.get("mr") and (v > st["ucl"] or v < st["lcl"]):
        out.append(("control", f"{v:.3f} is outside the control limits ({st['lcl']:.3f} – {st['ucl']:.3f}): the process changed"))
    return out


def check_new(line, part, keys, now=None):
    """Called after a measurement is stored: tell the line about drift. Never raises."""
    from hmi.engine import notify
    now = now or timezone.now()
    told = []
    try:
        for key in keys:
            lim = limits(part, key)
            if not lim:
                continue
            vals = [v for _, v, _ in series(line, part, key, now - timedelta(days=14), now + timedelta(minutes=1), 60)]
            label = dict((k, lb) for k, lb, _ in measures()).get(key, key)
            for rule, text in warnings(vals, lim):
                ck = f"spc:{line}:{part}:{key}:{rule}"
                if cache.add(ck, 1, 3600):
                    notify(f"{line} · {part} · {label}: {text}. Adjust before it goes out of spec.", "qc", lines=[line], go="qc-log")
                    told.append((key, rule))
    except Exception:  # noqa: BLE001 - a warning never stops a check from saving
        import logging
        logging.getLogger("plant").exception("SPC check")
    return told


def capability(a, b, key, line=""):
    """Cp / Cpk per machine and part for one measurement (at least 5 measurements)."""
    from hmi.models import AutoQC, QCCheck
    from plant.models import part_key
    pairs = set()
    builtin = key in {k for k, _, _ in MEASURES}
    for model in (QCCheck, AutoQC) if builtin else (QCCheck,):
        qs = model.objects.filter(t__gte=a, t__lt=b)
        if line:
            qs = qs.filter(line=line)
        pairs |= {(ln, part_key(it)) for ln, it in qs.values_list("line", "item").distinct()[:400] if it}
    rows = []
    for ln, part in sorted(pairs):
        lim = limits(part, key)
        vals = [v for _, v, _ in series(ln, part, key, a, b, 1000)]
        if len(vals) < 5:
            continue
        st = stats(vals, lim)
        rows.append({"line": ln, "part": part, "lim": lim, **st,
                     "grade": None if st.get("cpk") is None else "ok" if st["cpk"] >= 1.33 else "warn" if st["cpk"] >= 1.0 else "bad"})
    return rows


def chart_svg(pts, lim, st, w=900, h=260):
    """The individuals chart as an SVG: the measurements, the centre line, the control limits and the drawing's limits."""
    if not pts:
        return ""
    vals = [v for _, v, _ in pts]
    ys = vals + ([lim[1], lim[2]] if lim else []) + ([st["ucl"], st["lcl"]] if st.get("ucl") is not None else [])
    lo, hi = min(ys), max(ys)
    pad = (hi - lo) * 0.08 or 0.01
    lo, hi = lo - pad, hi + pad
    L, R, T, B = 56, 12, 10, 24

    def X(i):
        return L + (w - L - R) * (i / max(1, len(pts) - 1))

    def Y(v):
        return T + (h - T - B) * (1 - (v - lo) / (hi - lo))
    parts = [f'<svg class="spc" viewBox="0 0 {w} {h}" role="img" aria-label="Individuals chart">']

    def hline(v, cls, label):
        y = Y(v)
        parts.append(f'<line class="{cls}" x1="{L}" x2="{w - R}" y1="{y:.1f}" y2="{y:.1f}"/><text class="{cls}" x="{L - 4}" y="{y + 4:.1f}" text-anchor="end">{label} {v:.3f}</text>')
    if lim:
        hline(lim[2], "spec", "USL")
        hline(lim[1], "spec", "LSL")
        hline(lim[0], "tgt", "Dwg")
    if st.get("ucl") is not None and st.get("mr"):
        hline(st["ucl"], "ctl", "UCL")
        hline(st["lcl"], "ctl", "LCL")
    path = " ".join(f"{'M' if i == 0 else 'L'}{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(vals))
    parts.append(f'<path class="line" d="{path}"/>')
    for i, (t, v, src) in enumerate(pts):
        bad = lim and (v < lim[1] - qc.EPS or v > lim[2] + qc.EPS)
        parts.append(f'<circle class="pt {"bad" if bad else ""} {"vis" if src == "Vision" else ""}" cx="{X(i):.1f}" cy="{Y(v):.1f}" r="3.5">'
                     f'<title>{timezone.localtime(t):%m/%d %I:%M %p} · {src} · {v:.3f}</title></circle>')
    parts.append("</svg>")
    return "".join(parts)
