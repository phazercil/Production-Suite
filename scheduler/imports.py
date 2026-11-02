"""Reading MO exports (Excel / CSV / pasted rows) and the cost sheet workbook."""
import csv
import io
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from django.conf import settings

from plant.models import CostItem
from plant.util import Catalog, default_ops

FIELDS = [
    ("mo", "MO number", re.compile(r"^(mo|m\.?o\.?\s*#?|mo ?(no|num|number)|mfg.*order|manufacturing.*order|work ?order|wo|order ?(no|#|num|number)?)$", re.I)),
    ("part", "Part / item", re.compile(r"(part|item|sku|product|filter|mg ?#|aq)", re.I)),
    ("qty", "Quantity", re.compile(r"(qty|quantity|pcs|pieces|amount)", re.I)),
    ("due", "Due date", re.compile(r"(due|ship|need|req.*date|date)", re.I)),
    ("pleats", "Pleats (if not on sheet)", re.compile(r"pleat", re.I)),
    ("eb", "End Band flag", re.compile(r"(end ?band|stage ?2|bander)", re.I)),
    ("ac", "Arts & Crafts flag", re.compile(r"(art|craft|stage ?3)", re.I)),
    ("gb", "Glue Bead flag", re.compile(r"glue ?bead", re.I)),
]


def cell(v):
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def read_rows(upload=None, text=None):
    """Rows of cells from an .xlsx/.xlsm upload, a CSV upload or pasted tab/comma text."""
    if upload is not None:
        name = upload.name.lower()
        if name.endswith((".xlsx", ".xlsm")):
            from openpyxl import load_workbook
            wb = load_workbook(upload, read_only=True, data_only=True)
            ws = wb.worksheets[0]
            return [[cell(v) for v in r] for r in ws.iter_rows(values_only=True)]
        if name.endswith(".xls"):
            raise ValueError("Old .xls files can't be read. Save it as .xlsx or CSV and import that.")
        text = upload.read().decode("utf-8-sig", errors="replace")
    text = text or ""
    first = text.split("\n", 1)[0]
    delim = "\t" if first.count("\t") > first.count(",") else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    return [r for r in rows if any(str(c).strip() for c in r)]


def detect(rows):
    hi = 0
    for i, r in enumerate(rows[:10]):
        if sum(1 for c in r if any(rx.search(str(c).strip()) for _, _, rx in FIELDS)) >= 2:
            hi = i
            break
    header = [str(c if c is not None else "").strip() for c in (rows[hi] if rows else [])]
    mp = {}
    for k, _, rx in FIELDS:
        idx = next((i for i, h in enumerate(header) if rx.search(h) and i not in mp.values()), -1)
        mp[k] = idx
    if mp["due"] >= 0 and re.search(r"qty|quant", header[mp["due"]], re.I):
        mp["due"] = -1
    return {"header": header, "rows": rows[hi + 1:], "map": mp}


def truthy(v):
    return bool(re.match(r"^(y|yes|x|1|true|t)$", str(v if v is not None else "").strip(), re.I))


def parse_date(v):
    if isinstance(v, (int, float)) and 20000 < v < 80000:  # Excel serial
        return date(1899, 12, 30) + timedelta(days=int(v))
    s = str(v if v is not None else "").strip()
    if not s:
        return None
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})$", s)
    if m:
        y = int(m.group(3))
        y = y + 2000 if y < 100 else y
        try:
            return date(y, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    try:
        return datetime.fromisoformat(s[:19]).date()
    except ValueError:
        return None


def orders(imp, catalog=None):
    cat = catalog or Catalog()
    mp = imp["map"]

    def g(r, k):
        i = mp.get(k, -1)
        return r[i] if 0 <= i < len(r) else None

    out = []
    for r in imp["rows"]:
        mo = str(g(r, "mo") if g(r, "mo") is not None else "").strip()
        part = str(g(r, "part") if g(r, "part") is not None else "").strip()
        try:
            qty = float(re.sub(r"[^0-9.]", "", str(g(r, "qty") or "")) or 0)
        except ValueError:
            qty = 0
        if not mo or not part or not qty:
            continue
        item = (cat.matches(part) or [None])[0]
        ops = default_ops(item)
        for k in ("eb", "ac"):
            if mp.get(k, -1) >= 0 and str(g(r, k) or "").strip() != "":
                ops[k] = truthy(g(r, k))
        if mp.get("gb", -1) >= 0:
            ops["gb"] = truthy(g(r, "gb"))
        try:
            pleats = float(g(r, "pleats")) if g(r, "pleats") not in (None, "") else None
        except ValueError:
            pleats = None
        due = parse_date(g(r, "due"))
        out.append({"mo": mo, "part": part, "qty": int(qty), "due": due.isoformat() if due else None, "pleats": pleats, "ops": ops,
                    "matched": bool(item)})
    return out


# ------------------------------------------------------------------ cost sheet
def parse_cost_workbook(upload):
    from openpyxl import load_workbook
    wb = load_workbook(upload, read_only=True, data_only=True)
    items = []
    for ws in wb.worksheets:
        if re.search(r"non-?active", ws.title, re.I):
            continue
        rows = list(ws.iter_rows(values_only=True))
        if len(rows) < 3:
            continue
        hdr = [str(h).strip() if h is not None else "" for h in rows[1]]

        def ci(n):
            return hdr.index(n) if n in hdr else -1

        if ci("Pleats") < 0 or ci("Pleats per Minute") < 0:
            continue

        def num(v):
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

        for r in rows[2:]:
            aq = r[0] if r else None
            if not isinstance(aq, str) or not aq.strip():
                continue
            pl = num(r[ci("Pleats")])
            if not pl:
                continue

            def g(n):
                i = ci(n)
                return num(r[i]) if 0 <= i < len(r) else None

            items.append(CostItem(aq=aq.strip()[:40], cust=(str(r[1]).strip() if len(r) > 1 and r[1] is not None else "")[:40],
                                  cat=re.sub(r"^Cost ", "", ws.title)[:40], pieces=g("Pieces"), pleats=pl, ppm=g("Pleats per Minute"),
                                  nppm=g("New Machine Pleats per Minute"), s2=g("Stage 2 Filters per Hour") or None,
                                  s3=g("Stage 3 Filters per Hour") or None, mask=g("3rd and 4th Bander Mask Qty") or None))
    return items


def builtin_catalog():
    data = json.loads((Path(settings.BASE_DIR) / "plant" / "data" / "cost_sheet.json").read_text())
    rows = [CostItem(aq=a[0], cust=a[1] or "", cat=a[2], pieces=a[3], pleats=a[4], ppm=a[5], nppm=a[6], s2=a[7], s3=a[8], mask=a[9])
            for a in data["items"]]
    return rows, data["source"]
