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
    for k, label in qc.tol_specs():
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


@cmd("specFeatureAdd", PERM)
def spec_feature_add(p, ctx):
    """Quality › Tolerances › Add a measurement: every filter gets a value for it in Parts & specs (read from the asset
    database once its Part specs has a column for it), QC checks can measure it, and it has its own tolerance."""
    import re
    from hmi.engine import notify
    label = re.sub(r"\s+", " ", str(p.get("label") or "")).strip()[:40]
    need(label, "Name the measurement (e.g. Frame thickness).")
    st = settings_obj()
    cfg = dict(st.config)
    feats = list(cfg.get("specFeatures") or [])
    taken = {x.lower() for _, x in qc.TOL_SPECS} | {f["label"].lower() for f in feats} | {"pleats", "tolerance", "media", "rev"}
    need(label.lower() not in taken, f"There's already a measurement called {label}.")
    need(len(feats) < 20, "That's 20 added measurements already.")
    n = 1 + max([int(f["key"][1:]) for f in feats if f["key"][1:].isdigit()] + [0])
    key = f"x{n}"
    feats.append({"key": key, "label": label})
    cfg["specFeatures"] = feats
    lo, hi = _num(p.get("minus"), f"how far under for {label.lower()}"), _num(p.get("plus"), f"how far over for {label.lower()}")
    if lo is not None or hi is not None:
        cfg["qcTol"] = {**(cfg.get("qcTol") or {}), key: {"minus": lo if lo is not None else hi, "plus": hi if hi is not None else lo}}
    st.config = cfg
    st.save(update_fields=["config"])
    audit(ctx, f"Added the measurement {label} ({key}) to Parts & specs and Tolerances")
    notify(f"New measurement {label}: connect it to the asset database in Admin › Connector builder › Part specs "
           f"(its role is \"{label}\"), then Refresh Quality › Parts & specs to read every filter's value.", "planner", go="admin")
    return {"msg": f"{label} added to every filter in Parts & specs. Connect it to the asset database (Admin › Connector builder › "
                   "Part specs) to read each filter's value, or type them in."}


@cmd("specFeatureDel", PERM)
def spec_feature_del(p, ctx):
    """Remove an added measurement (its values stay on the parts, unused)."""
    key = str(p.get("key") or "")
    st = settings_obj()
    cfg = dict(st.config)
    feats = [f for f in cfg.get("specFeatures") or [] if f.get("key") != key]
    need(len(feats) < len(cfg.get("specFeatures") or []), "That measurement is already gone.")
    gone = next(f for f in cfg["specFeatures"] if f.get("key") == key)
    cfg["specFeatures"] = feats
    cfg["qcTol"] = {k: v for k, v in (cfg.get("qcTol") or {}).items() if k != key}
    cfg["qcItems"] = {s: [i for i in (items or []) if not (isinstance(i, dict) and i.get("key") == key)]
                      for s, items in (cfg.get("qcItems") or {}).items()}
    st.config = cfg
    st.save(update_fields=["config"])
    audit(ctx, f"Removed the measurement {gone['label']}")
    return {"msg": f"{gone['label']} removed."}


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
