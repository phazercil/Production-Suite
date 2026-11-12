"""Quality › Tolerances: how far under and over the drawing each measurement may be, the same for every filter."""
from hmi import qc
from hmi.engine import audit, cmd, need, settings_obj

PERM = "quality.tolerances"


def _num(v, what):
    s = str(v if v is not None else "").strip()
    if s == "":
        return None
    try:
        n = abs(float(s))
    except ValueError:
        need(False, f"Enter {what} in inches, e.g. 0.06.")
    need(n <= 10, f"{what[:1].upper() + what[1:]} looks too big ({n} in). Enter it in inches, e.g. 0.06.")
    return round(n, 4)


@cmd("qcTolSet", PERM)
def qc_tol_set(p, ctx):
    """p: {"<spec>_minus": in, "<spec>_plus": in} for each spec in qc.TOL_SPECS. Both blank: that measurement goes
    back to each part's own tolerance. One blank: the same both ways."""
    out, said = {}, []
    for k, label in qc.TOL_SPECS:
        lo, hi = _num(p.get(f"{k}_minus"), f"how far under for {label.lower()}"), _num(p.get(f"{k}_plus"), f"how far over for {label.lower()}")
        if lo is None and hi is None:
            said.append(f"{label}: each part's own")
            continue
        lo = hi if lo is None else lo
        hi = lo if hi is None else hi
        out[k] = {"minus": lo, "plus": hi}
        said.append(f"{label}: " + qc.fmt_tol(k, {}, out))
    st = settings_obj()
    cfg = dict(st.config)
    before = cfg.get("qcTol") or {}
    cfg["qcTol"] = out
    st.config = cfg
    st.save(update_fields=["config"])
    if out != before:
        audit(ctx, "QC tolerances: " + "; ".join(said))
    return {"msg": "Tolerances saved. New QC checks use them; past checks keep their result."}


@cmd("qualityIssuesSet", PERM)
def quality_issues_set(p, ctx):
    """The quality issues Process Out offers (Quality › QC checks), one per line. "Other" (with a description) is
    always kept at the end."""
    import re
    raw = p.get("issues")
    lines = raw if isinstance(raw, list) else str(raw or "").splitlines()
    out = []
    for x in lines:
        x = re.sub(r"\s+", " ", str(x)).strip()[:80]
        if x and x.lower() != "other" and x.lower() not in [o.lower() for o in out]:
            out.append(x)
    need(out, "List at least one quality issue.")
    need(len(out) <= 60, "That's a lot of issues: keep it to 60 or fewer.")
    out.append("Other")
    st = settings_obj()
    st.config = {**st.config, "qualityIssues": out}
    st.save(update_fields=["config"])
    audit(ctx, "Process Out quality issues: " + ", ".join(out))
    return {"msg": "Quality issues saved."}
