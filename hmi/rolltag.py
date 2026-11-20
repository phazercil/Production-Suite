"""Reading a roll tag from a picture (Roll tags › Take a picture of the tag).

The operator's tablet takes the picture and reads it on the tablet itself: the text with Tesseract (served by the
suite from static/vendor/tesseract, so nothing leaves the plant network) and any barcodes with the browser's own
barcode reader. It sends the picture and what it read here, and this module decides what each field is:

  roll      the roll ID / serial number
  lot       the lot or batch number
  material  the media
  feet      the footage on the roll (metres and yards are converted to feet)

Each field is found by the labels printed next to it on the tag ("Roll No", "Lot", "Length" …: Quality ›
Traceability › Reading roll tags) with the value after the label on the same line, or on the line under it. A
field can also have a pattern of its own (a regular expression) for tags that print a value with no label.
Barcodes: GS1 barcodes are split into their parts ((10) batch/lot, (21) serial, (37) count…); a plain barcode
fills the field picked in the settings (the roll ID unless changed), when the text didn't already give it.

The settings are in PlantSettings.config["rollTag"].
"""
import re

FIELDS = [("roll", "Roll ID"), ("lot", "Lot"), ("material", "Material"), ("feet", "Footage")]
DEFAULT = {
    "labels": {
        "roll": ["roll no", "roll number", "roll #", "roll id", "roll", "serial no", "serial number", "serial", "s/n", "sn"],
        "lot": ["lot no", "lot number", "lot #", "lot", "batch no", "batch number", "batch"],
        "material": ["material", "media", "product", "description", "item", "grade", "style", "part no", "part"],
        "feet": ["footage", "linear feet", "lin ft", "length", "lf", "feet", "ft", "meters", "metres", "yards"],
    },
    "patterns": {"roll": "", "lot": "", "material": "", "feet": ""},
    "barcode": "roll",
}
M_TO_FT, YD_TO_FT = 3.28084, 3.0
GS1 = {"10": "lot", "21": "roll", "240": "material", "241": "material", "01": "gtin", "37": "count"}
_SEP = r"[\s:#.\-=]*"
MAX_LINES = 200


def settings(cfg=None):
    from plant.models import PlantSettings
    cfg = cfg if cfg is not None else PlantSettings.load().config
    own = cfg.get("rollTag") if isinstance(cfg.get("rollTag"), dict) else {}
    out = {"labels": {}, "patterns": {}, "barcode": own.get("barcode") if own.get("barcode") in dict(FIELDS) or own.get("barcode") == "" else DEFAULT["barcode"]}
    for k, _ in FIELDS:
        lab = (own.get("labels") or {}).get(k)
        out["labels"][k] = [str(x).strip().lower() for x in lab if str(x).strip()] if isinstance(lab, list) else list(DEFAULT["labels"][k])
        pat = (own.get("patterns") or {}).get(k)
        out["patterns"][k] = str(pat) if isinstance(pat, str) else DEFAULT["patterns"][k]
    return out


def check_pattern(p):
    """A field's own pattern must be a regular expression that can be used safely; returns an error or ''."""
    if not p:
        return ""
    if len(p) > 200:
        return "Keep the pattern under 200 characters."
    if re.search(r"\([^)]*[+*][^)]*\)[+*{]", p):
        return "That pattern repeats a repeat (like (a+)+), which can take forever to check. Simplify it."
    try:
        re.compile(p)
    except re.error as e:
        return f"That isn't a pattern the server can read: {e}."
    return ""


def clean_lines(raw):
    """The text lines the tablet read, as [{"text", "box": (x0, y0, x1, y1)}], top to bottom."""
    out = []
    for x in (raw or [])[:MAX_LINES]:
        if isinstance(x, str):
            x = {"text": x}
        if not isinstance(x, dict):
            continue
        text = re.sub(r"\s+", " ", str(x.get("text") or "")).strip()[:200]
        if not text:
            continue
        b = x.get("box")
        try:
            box = tuple(float(v) for v in b[:4]) if isinstance(b, (list, tuple)) and len(b) >= 4 else None
        except (TypeError, ValueError):
            box = None
        out.append({"text": text, "box": box})
    if all(x["box"] for x in out):
        out.sort(key=lambda x: (round(x["box"][1] / 12), x["box"][0]))
    return out


def _label_re(label):
    words = [re.escape(w) for w in label.split()]
    body = r"[\s.]*".join(words)
    if label.endswith("#"):
        body = body[:-2] + r"\s*#"
    return re.compile(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z]){_SEP}", re.I)


def _below(lines, i):
    """The line under line i: the next line down that overlaps it sideways (or simply the next line)."""
    me = lines[i]
    if not me["box"]:
        return lines[i + 1]["text"] if i + 1 < len(lines) else ""
    x0, y0, x1, y1 = me["box"]
    best = None
    for j, o in enumerate(lines):
        if j == i or not o["box"]:
            continue
        a0, b0, a1, _ = o["box"]
        if b0 <= y0 + (y1 - y0) * 0.5 or b0 - y1 > 3 * max(8, y1 - y0):
            continue
        if min(x1, a1) - max(x0, a0) < -0.25 * (x1 - x0):
            continue
        if best is None or b0 < best[0]:
            best = (b0, o["text"])
    return best[1] if best else ""


def _strip_other_labels(v, labels, me):
    """A line like "Lot 4471 Roll 0098": cut the value where another field's label starts."""
    cut = len(v)
    for k, labs in labels.items():
        if k == me:
            continue
        for lab in labs:
            if len(lab) < 3:
                continue
            m = re.search(rf"\s{re.escape(lab)}(?![A-Za-z]){_SEP}", " " + v, re.I)
            if m and m.start() > 0:
                cut = min(cut, m.start() - 1)
    return v[:cut].strip(" :#.-=")


def _by_label(lines, field, cfg):
    labels = sorted(cfg["labels"][field], key=len, reverse=True)   # "roll no" before "roll"
    for lab in labels:
        rx = _label_re(lab)
        for i, ln in enumerate(lines):
            m = rx.search(ln["text"])
            if not m:
                continue
            if m.start() > 0 and ln["text"][m.start() - 1].isalnum():
                continue
            v = _strip_other_labels(ln["text"][m.end():].strip(), cfg["labels"], field)
            if not v or (field != "feet" and not re.search(r"[A-Za-z0-9]", v)):
                v = _strip_other_labels(_below(lines, i), cfg["labels"], field)
            if field == "feet" and v:
                unit = "m" if re.search(r"\b(meters?|metres?)\b", lab, re.I) else "yd" if "yard" in lab else ""
                ft = feet_of(v, unit)
                if ft is not None:
                    return ft
                continue
            if v:
                return v
    return None


def feet_of(v, unit=""):
    """'2,500 LF' -> 2500; '1000 m' -> 3281; '500 yds' -> 1500. None when there's no number."""
    s = str(v or "").replace(",", "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(ft|feet|lf|lin\.?\s*ft|'|m|meters?|metres?|mtrs?|yds?|yards?)?\b", s, re.I)
    if not m:
        return None
    n = float(m.group(1))
    u = (m.group(2) or unit or "").lower().strip()
    if u in ("m", "meter", "meters", "metre", "metres", "mtr", "mtrs"):
        n *= M_TO_FT
    elif u.startswith("y"):
        n *= YD_TO_FT
    n = int(round(n))
    return n if 0 < n <= 1_000_000 else None


def gs1(value):
    """The parts of a GS1 barcode: '(10)A123(21)R77' or the same with group separators. {} if it isn't one."""
    v = str(value or "")
    out = {}
    if "(" in v:
        for ai, val in re.findall(r"\((\d{2,4})\)([^()]+)", v):
            if ai in GS1:
                out[GS1[ai]] = val.strip()
        return out
    if "\x1d" in v or v.startswith("]C1") or v.startswith("]d2") or v.startswith("]Q3"):
        v = re.sub(r"^\][A-Za-z]\d", "", v)
        fixed = {"01": 14, "37": None}
        while v:
            ai = next((a for a in sorted(GS1, key=len, reverse=True) if v.startswith(a)), None)
            if not ai:
                break
            v = v[len(ai):]
            n = fixed.get(ai)
            if n:
                val, v = v[:n], v[n:]
            else:
                val, _, v = v.partition("\x1d")
            out[GS1[ai]] = val
    return out


def known_media():
    from plant.models import PartSpec
    return sorted({m.strip() for m in PartSpec.objects.exclude(media="").values_list("media", flat=True) if m and m.strip()}, key=len, reverse=True)


def read(lines, barcodes, cfg=None, media=None):
    """{field: value} from the text lines and barcodes the tablet read, and how each was found."""
    cfg = cfg or settings()
    lines = clean_lines(lines)
    found, how = {}, {}
    for k, _ in FIELDS:
        pat = cfg["patterns"].get(k)
        if pat and not check_pattern(pat):
            rx = re.compile(pat, re.I)
            for ln in lines:
                m = rx.search(ln["text"])
                if m:
                    v = m.group(1) if m.groups() else m.group(0)
                    if k == "feet":
                        v = feet_of(v)
                    if v:
                        found[k], how[k] = v, "pattern"
                        break
        if k not in found:
            v = _by_label(lines, k, cfg)
            if v:
                found[k], how[k] = v, "label"
    codes = [str(b.get("value") if isinstance(b, dict) else b or "").strip()[:200] for b in (barcodes or [])[:10]]
    codes = [c for c in codes if c]
    for c in codes:
        for k, v in gs1(c).items():
            if k in dict(FIELDS) and v and (k not in found or how.get(k) != "pattern"):
                found[k], how[k] = v, "barcode"
    plain = [c for c in codes if not gs1(c)]
    if plain and cfg.get("barcode") in dict(FIELDS) and cfg["barcode"] not in found:
        k = cfg["barcode"]
        v = feet_of(plain[0]) if k == "feet" else plain[0]
        if v:
            found[k], how[k] = v, "barcode"
    if how.get("material") != "pattern":   # a media the parts list knows, printed anywhere on the tag, beats a guess by label
        text = " ".join(x["text"] for x in lines).upper()
        have = str(found.get("material") or "").upper()
        for m in media if media is not None else known_media():
            mu = m.upper()
            if len(m) >= 3 and re.search(rf"(?<![A-Z0-9]){re.escape(mu)}(?![A-Z0-9])", text):
                if mu not in have:
                    found["material"], how["material"] = m, "media list"
                break
    for k in ("roll", "lot", "material"):
        if k in found:
            found[k] = str(found[k]).strip()[:120 if k == "material" else 80]
    return {"fields": found, "how": how, "text": "\n".join(x["text"] for x in lines), "barcodes": codes}
