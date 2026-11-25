"""Formulas in a work instruction's process parameters: Target, Min and Max can use the filter being run, e.g.
[Pleat height (mm)] + 1. Each [name] is a variable from the filter in the 3D viewer's part library (Quality › 3D
viewer, kept in the suite): its settings (numbers in mm, yes/no as 1 / 0, text) and the filter's size (overall length,
width, height) as the viewer works it out. The drawing is only a picture: nothing is read from it.

Conditions (Show when / Not for filters where) also compare: = <> < > <= >=, and / or / not, "text".
Only that and arithmetic are worked out: numbers, [variables], + - * / ^ (or **), brackets and round(), min(), max(),
abs(), ceil(), floor(). Nothing else is run. A formula whose variable the part doesn't have gives no value (shown as missing), never a guess."""
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


SIZE_VARS = [("olen", "Overall length (mm)"), ("len", "Pack length (mm)"), ("wid", "Filter width (mm)"), ("hgt", "Filter height (mm)")]


TOL_VARS = [("tol_len", "Tolerance – length"), ("tol_wid", "Tolerance – width"), ("tol_fh", "Tolerance – filter height"),
            ("tol_ph", "Tolerance – pleat height")]


def _extra_labels():
    """The rows people added to filters' information sheets (Notes-like extras), across the library: each is a
    variable (text) under "Info sheet · <label>". Read at most once a minute."""
    from django.core.cache import cache
    labels = cache.get("wi-extra-labels")
    if labels is None:
        from plant.models import FilterModel3D
        seen = {}
        try:
            for data in FilterModel3D.objects.values_list("data", flat=True):
                for r in (data or {}).get("extra") or []:
                    lbl = " ".join(str((r or {}).get("label") or "").split())[:60]
                    if lbl:
                        seen.setdefault(_norm(lbl), lbl)
        except Exception:  # noqa: BLE001 - before the first migration
            pass
        labels = sorted(seen.values(), key=str.lower)
        cache.set("wi-extra-labels", labels, 60)
    return labels


def variables():
    """Every variable a formula can use, all from the filter in the 3D viewer's library: [{name, key, src, group,
    type}], in the order they're listed. The information sheet's fields (glue, screw #, helix #, tolerances, notes…)
    are text; each tolerance also gives its two limits as numbers. (The drawing is a picture: nothing is read from it.)"""
    from hmi.snapshot import fv_columns
    out = [{"name": n, "key": f"size.{k}", "src": "3d", "group": "3D viewer · Filter size", "type": "number"} for k, n in SIZE_VARS]
    for c in fv_columns():
        if c.get("type") in ("num", "int", "bool", "enum", "text"):
            kind = {"bool": "yes/no", "enum": "text", "text": "text"}.get(c["type"], "number")
            out.append({"name": c["h"], "key": f"3d.{c['db']}", "src": "3d", "group": f"3D viewer · {c['group']}", "type": kind,
                        "path": c.get("key", "")})
    for k, lbl in TOL_VARS:
        out.append({"name": f"{lbl} plus (mm)", "key": f"tol.{k}.plus", "src": "3d", "group": "3D viewer · Info", "type": "number"})
        out.append({"name": f"{lbl} minus (mm)", "key": f"tol.{k}.minus", "src": "3d", "group": "3D viewer · Info", "type": "number"})
    out += [{"name": f"Info sheet · {lbl}", "key": f"extra.{_norm(lbl)}", "src": "3d", "group": "3D viewer · Info sheet rows added",
             "type": "text"} for lbl in _extra_labels()]
    seen, uniq = set(), []
    for v in out:   # the first of two with the same name wins (they never should clash, but a name must mean one thing)
        if _norm(v["name"]) not in seen:
            seen.add(_norm(v["name"]))
            uniq.append(v)
    return uniq


def _at(data, path):
    """A setting in the viewer's own document by its batch-sheet key: info.glue, P.ph, ui.side_c_on,
    band.side.0.sb, foam.0.t."""
    cfg = data.get("config") or {}
    bits = path.split(".")
    try:
        if bits[0] == "info":
            return (data.get("info") or {}).get(bits[1])
        if bits[0] == "P":
            return (cfg.get("P") or {}).get(bits[1])
        if bits[0] == "ui":
            return (cfg.get("ui") or {}).get(bits[1])
        if bits[0] == "band":
            return ((cfg.get("groups") or {}).get(bits[1]) or {}).get("bands", [])[int(bits[2])].get(bits[3])
        if bits[0] == "foam":
            return (cfg.get("foams") or [])[int(bits[1])].get(bits[2])
    except (IndexError, ValueError, AttributeError, TypeError):
        return None
    return None


def _value(v):
    """A setting as a formula sees it: yes/no as 1 / 0, numbers as numbers, the rest as text; None when blank."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if _num(v) is not None:
        return _num(v)
    t = str(v).strip()
    if t.lower() in ("yes", "true", "on", "no", "false", "off"):
        return 1.0 if t.lower() in ("yes", "true", "on") else 0.0
    return t


def _num(v):
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def values_for(part):
    """The variables' values for a part, from its filter in the 3D viewer's library: {normalized name: value}. Only
    what's on file: a setting the viewer still flags for review (a default nobody entered) isn't used."""
    from quality import viewer
    vals = {}
    x = viewer.find(part) if part else None
    if not x:
        return vals
    data = x.data or {}
    needs = set(data.get("needs") or [])
    flat = x.flat or {}
    for v in variables():
        name, key = _norm(v["name"]), v["key"]
        if key.startswith("3d."):
            if v.get("path") in needs:
                continue
            col = key[3:]
            got = _value(flat.get(col))
            if got is None and v.get("path"):
                got = _value(_at(data, v["path"]))
            if got is not None:
                vals[name] = got
        elif key.startswith("tol."):
            _, col, side = key.split(".")
            from quality.viewer import parse_tol
            t = parse_tol((data.get("info") or {}).get(col.replace("tol_", "tol_")) or flat.get({"tol_len": "tolerance_length",
                          "tol_wid": "tolerance_width", "tol_fh": "tolerance_filter_height", "tol_ph": "tolerance_pleat_height"}[col]))
            if t:
                vals[name] = t[1] if side == "plus" else t[0]
        elif key.startswith("extra."):
            for r in data.get("extra") or []:
                if _norm((r or {}).get("label")) == key[6:]:
                    got = _value((r or {}).get("value"))
                    if got is not None:
                        vals[name] = got
    for k, v in viewer.dims(x).items():   # the size, worked out as the viewer does; None when not entered
        n = _num(v)
        if n is not None:
            vals[_norm(dict(SIZE_VARS).get(k, ""))] = n
    vals.pop("", None)
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

    py = VAR.sub(repl, s).replace("^", "**")
    py = re.sub(r"(?<![<>=!])=(?!=)", "==", py)                   # "=" means equal to
    py = re.sub(r"<>", "!=", py)
    py = re.sub(r"\b(AND|And)\b", "and", py)
    py = re.sub(r"\b(OR|Or)\b", "or", py)
    py = re.sub(r"\b(NOT|Not)\b", "not", py)
    py = py.strip()
    if "[" in py or "]" in py:
        raise FormulaError("A [ without its ] (variables are written [Pleat height (mm)]).")
    try:
        tree = ast.parse(py, mode="eval")
    except SyntaxError:
        raise FormulaError("That isn't a formula the suite can work out (numbers, [variables], + - * / ^ and brackets).") from None
    for node in ast.walk(tree):
        if isinstance(node, (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Load, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod,
                             ast.USub, ast.UAdd, ast.Not, ast.BoolOp, ast.And, ast.Or, ast.Compare, ast.Eq, ast.NotEq,
                             ast.Lt, ast.LtE, ast.Gt, ast.GtE)):
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, str)) and not isinstance(node.value, bool):
            continue
        if isinstance(node, ast.Name) and (node.id in slots or node.id in FUNCS):
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCS and not node.keywords:
            continue
        raise FormulaError("Only numbers, \"text\", [variables], + - * / ^, = < > <= >= <>, and / or / not, brackets and "
                           "round(), min(), max(), abs(), ceil(), floor().")
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
        return node.value if isinstance(node.value, str) else float(node.value)
    if isinstance(node, ast.Name):
        if node.id not in env:
            raise FormulaError(f"{node.id} needs brackets: {node.id}(…).")
        return env[node.id]
    if isinstance(node, ast.UnaryOp):
        v = _eval(node.operand, env)
        if isinstance(node.op, ast.Not):
            return 0.0 if _truth(v) else 1.0
        _need_num(v)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.BoolOp):
        vals = [_truth(_eval(v, env)) for v in node.values]
        return 1.0 if (all(vals) if isinstance(node.op, ast.And) else any(vals)) else 0.0
    if isinstance(node, ast.Compare):
        left = _eval(node.left, env)
        for op, right_node in zip(node.ops, node.comparators):
            right = _eval(right_node, env)
            if not _compare(op, left, right):
                return 0.0
            left = right
        return 1.0
    if isinstance(node, ast.BinOp):
        a, b = _eval(node.left, env), _eval(node.right, env)
        _need_num(a)
        _need_num(b)
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
        for a in args:
            _need_num(a)
        if node.func.id == "round":
            return float(round(args[0], int(args[1]) if len(args) > 1 else 0))
        return float(FUNCS[node.func.id](*args))
    raise FormulaError("Not a formula.")


def _need_num(v):
    if isinstance(v, str):
        raise FormulaError(f"\"{v}\" is text: only = and <> work with text.")


def _truth(v):
    return bool(v.strip()) if isinstance(v, str) else bool(v)


def _compare(op, a, b):
    """= and <> compare numbers (to the nearest thousandth) or text (any letter case); < > <= >= only numbers."""
    if isinstance(a, str) or isinstance(b, str):
        if not isinstance(op, (ast.Eq, ast.NotEq)):
            raise FormulaError("Text can only be compared with = or <>.")
        ta = a if isinstance(a, str) else fmt(a)   # a number-looking setting (Screw # 8.1) compares with "8.1" too
        tb = b if isinstance(b, str) else fmt(b)
        same = ta.strip().lower() == tb.strip().lower()
        return same if isinstance(op, ast.Eq) else not same
    if isinstance(op, ast.Eq):
        return abs(a - b) < 1e-3
    if isinstance(op, ast.NotEq):
        return abs(a - b) >= 1e-3
    return {ast.Lt: a < b, ast.LtE: a <= b + 1e-9, ast.Gt: a > b, ast.GtE: a >= b - 1e-9}[type(op)]


def condition(expr, vals):
    """A Show when condition for a filter: (True / False, or None when it can't be checked, [missing names], error)."""
    if not str(expr or "").strip():
        return True, [], ""
    try:
        tree, slots = _parse(expr)
    except FormulaError as e:
        return None, [], str(e)
    missing = [n for n in dict.fromkeys(slots.values()) if n not in vals]
    if missing:
        byname = {_norm(v["name"]): v["name"] for v in variables()}
        return None, [byname.get(m, m) for m in missing], ""
    try:
        return _truth(_eval(tree, {k: vals[n] for k, n in slots.items()})), [], ""
    except FormulaError as e:
        return None, [], str(e)
    except (ZeroDivisionError, OverflowError, ValueError, TypeError) as e:
        return None, [], f"Can't be checked ({e})."


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
        if isinstance(v, str):
            raise FormulaError(f"Works out to text (\"{v}\"), not a number.")
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
