"""Formulas in a work instruction's process parameters: Target, Min and Max can use the filter being run, e.g.
[Pleat height (mm)] + 1. Each [name] is a variable from the filter's data kept in the suite:

- the 3D viewer's part library (Quality › 3D viewer): its numeric settings and the filter's size (overall length,
  width, height) as the viewer works it out, in mm;
- Parts & specs (the drawing): pleats, length, width, pleat height, side band height, tolerance and the
  measurements added in Quality › Tolerances, in inches.

Only arithmetic: numbers, [variables], + - * / ^ (or **), brackets and round(), min(), max(), abs(), ceil(), floor().
Nothing else is run. A formula whose variable the part doesn't have gives no value (shown as missing), never a guess."""
import ast
import math
import re

VAR = re.compile(r"\[([^\[\]]{1,80})\]")
FUNCS = {"round": round, "min": min, "max": max, "abs": abs, "ceil": math.ceil, "floor": math.floor}
MAX_LEN = 200


class FormulaError(ValueError):
    pass


def _norm(name):
    return " ".join(str(name or "").lower().split())


SPEC_VARS = [("pleats", "Pleats", ""), ("len", "Length (in)", "in"), ("wid", "Width (in)", "in"), ("ph", "Pleat height (in)", "in"),
             ("sb", "Side band height (in)", "in"), ("tol", "Tolerance (in)", "in")]


SIZE_VARS = [("olen", "Overall length (mm)"), ("len", "Pack length (mm)"), ("wid", "Filter width (mm)"), ("hgt", "Filter height (mm)")]


def variables():
    """Every variable a formula can use: [{name, key, src}] (src "3d" or "spec"), in the order they're listed."""
    from hmi import qc
    from hmi.snapshot import fv_columns
    out = [{"name": n, "key": f"spec.{k}", "src": "spec", "group": "Parts & specs (drawing)"} for k, n, _ in SPEC_VARS]
    try:
        feats = qc.features()
    except Exception:  # noqa: BLE001 - no settings yet
        feats = []
    out += [{"name": f"{f['label']} (in)", "key": f"spec.{f['key']}", "src": "spec", "group": "Parts & specs (drawing)"} for f in feats]
    out += [{"name": n, "key": f"size.{k}", "src": "3d", "group": "3D viewer · Filter size"} for k, n in SIZE_VARS]
    for c in fv_columns():
        if c.get("type") in ("num", "int"):
            out.append({"name": c["h"], "key": f"3d.{c['db']}", "src": "3d", "group": f"3D viewer · {c['group']}"})
    seen, uniq = set(), []
    for v in out:   # the first of two with the same name wins (they never should clash, but a name must mean one thing)
        if _norm(v["name"]) not in seen:
            seen.add(_norm(v["name"]))
            uniq.append(v)
    return uniq


def _num(v):
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def values_for(part):
    """The variables' values for a part: {normalized name: number}. Only what's on file; nothing filled in."""
    from plant.models import PartSpec, part_key
    from quality import viewer
    vals = {}
    names = {v["key"]: _norm(v["name"]) for v in variables()}
    sp = PartSpec.objects.filter(key=part_key(part)).first() if part else None
    if sp:
        for k in ("pleats", "len", "wid", "ph", "sb", "tol"):
            n = _num(getattr(sp, k))
            if n is not None and f"spec.{k}" in names:
                vals[names[f"spec.{k}"]] = n
        for k, v in (sp.extra or {}).items():
            n = _num(v)
            if n is not None and f"spec.{k}" in names:
                vals[names[f"spec.{k}"]] = n
    x = viewer.find(part) if part else None
    if x:
        for k, v in viewer.dims(x).items():   # worked out as the viewer does; None when not entered
            n = _num(v)
            if n is not None and f"size.{k}" in names:
                vals[names[f"size.{k}"]] = n
        for k, v in (x.flat or {}).items():
            n = _num(v)
            if n is not None and f"3d.{k}" in names:
                vals[names[f"3d.{k}"]] = n
    return vals


def _parse(expr):
    """The formula as a checked expression tree, with [names] replaced by placeholders: (tree, {placeholder: name})."""
    s = str(expr or "").strip()
    if len(s) > MAX_LEN:
        raise FormulaError(f"{MAX_LEN} characters at most.")
    known = {_norm(v["name"]) for v in variables()}
    slots = {}

    def repl(m):
        name = _norm(m.group(1))
        if name not in known:
            raise FormulaError(f'There is no variable [{m.group(1).strip()}]. Pick one from the Insert variable list.')
        key = f"v{len(slots)}"
        slots[key] = name
        return f" {key} "

    py = VAR.sub(repl, s).replace("^", "**").strip()
    if "[" in py or "]" in py:
        raise FormulaError("A [ without its ] (variables are written [Pleat height (mm)]).")
    try:
        tree = ast.parse(py, mode="eval")
    except SyntaxError:
        raise FormulaError("That isn't a formula the suite can work out (numbers, [variables], + - * / ^ and brackets).") from None
    for node in ast.walk(tree):
        if isinstance(node, (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Load, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod,
                             ast.USub, ast.UAdd)):
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            continue
        if isinstance(node, ast.Name) and (node.id in slots or node.id in FUNCS):
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCS and not node.keywords:
            continue
        raise FormulaError("Only numbers, [variables], + - * / ^, brackets and round(), min(), max(), abs(), ceil(), floor().")
    return tree, slots


def is_formula(text):
    s = str(text or "").strip()
    return bool(s) and _num(s) is None


def check(expr):
    """Raise FormulaError if the formula can't be worked out for any part (a typo, an unknown variable)."""
    _parse(expr)


def used(expr):
    """The variables a formula uses (their names as written in the list)."""
    try:
        _, slots = _parse(expr)
    except FormulaError:
        return []
    byname = {_norm(v["name"]): v["name"] for v in variables()}
    return [byname.get(n, n) for n in dict.fromkeys(slots.values())]


def _eval(node, env):
    if isinstance(node, ast.Expression):
        return _eval(node.body, env)
    if isinstance(node, ast.Constant):
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id not in env:
            raise FormulaError(f"{node.id} needs brackets: {node.id}(…).")
        return env[node.id]
    if isinstance(node, ast.UnaryOp):
        v = _eval(node.operand, env)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.BinOp):
        a, b = _eval(node.left, env), _eval(node.right, env)
        if isinstance(node.op, ast.Add):
            return a + b
        if isinstance(node.op, ast.Sub):
            return a - b
        if isinstance(node.op, ast.Mult):
            return a * b
        if isinstance(node.op, ast.Div):
            return a / b
        if isinstance(node.op, ast.Mod):
            return a % b
        if abs(b) > 10 or abs(a) > 1e6:
            raise FormulaError("That power is too big.")
        return a ** b
    if isinstance(node, ast.Call):
        args = [_eval(a, env) for a in node.args]
        if node.func.id == "round":
            return float(round(args[0], int(args[1]) if len(args) > 1 else 0))
        return float(FUNCS[node.func.id](*args))
    raise FormulaError("Not a formula.")


def evaluate(expr, vals):
    """(number or None, [missing variable names], error or ""): a plain number is itself."""
    n = _num(str(expr or "").strip())
    if n is not None:
        return n, [], ""
    try:
        tree, slots = _parse(expr)
    except FormulaError as e:
        return None, [], str(e)
    missing = [n for n in dict.fromkeys(slots.values()) if n not in vals]
    if missing:
        byname = {_norm(v["name"]): v["name"] for v in variables()}
        return None, [byname.get(m, m) for m in missing], ""
    try:
        v = _eval(tree, {k: vals[n] for k, n in slots.items()})
    except FormulaError as e:
        return None, [], str(e)
    except (ZeroDivisionError, OverflowError, ValueError, TypeError) as e:
        return None, [], "Can't be worked out for this part (" + ("divides by zero" if isinstance(e, ZeroDivisionError) else str(e) or "out of range") + ")."
    if not math.isfinite(v):
        return None, [], "Can't be worked out for this part."
    return v, [], ""


def fmt(v):
    if v is None:
        return ""
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def resolve_params(params, part):
    """A revision's parameters worked out for a part: each with value/min/max as numbers (text), the formulas as
    written, and what's missing. [{name, desc, unit, target, min, max, f_target, f_min, f_max, missing, err}]."""
    vals = values_for(part) if any(is_formula(p.get(k)) for p in params for k in ("target", "min", "max")) else {}
    out = []
    for p in params:
        row = {"name": p.get("name") or "", "desc": p.get("desc") or "", "unit": p.get("unit") or "", "missing": [], "err": ""}
        for k in ("target", "min", "max"):
            raw = str(p.get(k) or "").strip()
            if not raw:
                row[k], row["f_" + k] = "", ""
                continue
            v, miss, err = evaluate(raw, vals)
            row[k] = fmt(v)
            row["f_" + k] = raw if is_formula(raw) else ""
            row["missing"] += [m for m in miss if m not in row["missing"]]
            row["err"] = row["err"] or err
        out.append(row)
    return out


# Parameters (and formulas) inside a step's text: "Set the screws to {Screw height}" shows "Set the screws to 31.5 mm".
# {Name} is the parameter's target with its unit; {Name:min}, {Name:max} and {Name:range} its limits;
# {[Pleat height (mm)] + 1} works a formula out on the spot (a number, no unit).
PLACE = __import__("re").compile(r"\{([^{}\n]{1,200})\}")
PARTS = {"": "target", "target": "target", "min": "min", "max": "max", "range": "range"}


def _split(ref):
    name, _, part = ref.rpartition(":") if ":" in ref else (ref, "", "")
    if ":" in ref and part.strip().lower() in PARTS:
        return name.strip(), PARTS[part.strip().lower()]
    return ref.strip(), "target"


def check_text(text, param_names):
    """Raise FormulaError for a {…} in a step that isn't one of the parameters or a working formula."""
    names = {_norm(n) for n in param_names if n}
    for m in PLACE.finditer(str(text or "")):
        ref = m.group(1)
        if "[" in ref:
            check(ref)
            continue
        name, _ = _split(ref)
        if _norm(name) not in names:
            raise FormulaError(f"{{{ref}}}: there's no parameter called {name}. Use one of the parameters below, "
                               "e.g. {" + (sorted(param_names)[0] if param_names else "Screw height") + "}.")


def fill_text(text, resolved, vals=None):
    """A step's text with each {…} replaced by its value for the part (resolved = resolve_params for it). A value the
    part doesn't have reads "(not on file: …)": the step still shows, never a guessed number."""
    by = {_norm(p["name"]): p for p in resolved or []}

    def one(m):
        ref = m.group(1)
        if "[" in ref:
            v, missing, err = evaluate(ref, vals if vals is not None else {})
            if missing:
                return f"(not on file: {', '.join(missing)})"
            return fmt(v) if v is not None else f"({err or 'can’t be worked out'})"
        name, part = _split(ref)
        p = by.get(_norm(name))
        if not p:
            return m.group(0)
        unit = f" {p['unit']}" if p.get("unit") else ""
        if p.get("missing"):
            return f"(not on file: {', '.join(p['missing'])})"
        if part == "range":
            lo, hi = p.get("min"), p.get("max")
            val = f"{lo}–{hi}" if lo and hi else f"≥ {lo}" if lo else f"≤ {hi}" if hi else ""
        else:
            val = p.get(part) or ""
        return f"{val}{unit}" if val else f"({p['name']}: no {('value' if part == 'target' else part)} set)"
    return PLACE.sub(one, str(text or ""))


def fill_steps(steps, params, part):
    """Steps with their {…} filled in for a part, and the parameters worked out: (steps, resolved)."""
    resolved = resolve_params(params, part)
    need_vals = any("[" in m.group(1) for s in steps for m in PLACE.finditer(str(s.get("text") or "") + str(s.get("caution") or "")))
    vals = values_for(part) if need_vals else {}
    return [{**s, "text": fill_text(s.get("text"), resolved, vals), "caution": fill_text(s.get("caution"), resolved, vals)}
            for s in steps], resolved
