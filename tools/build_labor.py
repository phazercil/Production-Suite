"""Rebuild the Labor Budget mode from the Labor Calculator's single HTML file.

    python tools/build_labor.py [path/to/Labor_Calculator.html] [--tailwind path/to/tailwindcss-cli]

1. Keeps a copy of the source in labor/source/ (so a newer calculator can be merged the same way).
2. Builds labor/static/labor/labor.css with the Tailwind CLI (v3), so the page needs nothing from the internet.
3. Writes labor/page/labor.html: the calculator with
   - the Tailwind CDN and Google Fonts links replaced by the local stylesheet,
   - the plan loaded from and saved to the suite's server (shift leads only) instead of this browser's storage,
   - the suite's mode bar on top.
Each change is checked: if the calculator's code no longer matches, the build stops and says which part.
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LABOR = ROOT / "labor"


sys.path.insert(0, str(ROOT))
from labor.build import BuildError, build_page as _build_page, scrub as _scrub  # noqa: E402


def scrub(html):
    try:
        return _scrub(html)
    except BuildError as e:
        sys.exit(f"build_labor: {e}")


def build_page(html):
    try:
        return _build_page(html)
    except BuildError as e:
        sys.exit(f"build_labor: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source", nargs="?", default=str(LABOR / "source" / "Labor_Calculator.html"))
    ap.add_argument("--tailwind", default="tailwindcss")
    ap.add_argument("--no-css", action="store_true", help="keep labor.css as it is (the suite's additions use inline styles)")
    a = ap.parse_args()
    src = Path(a.source)
    html = scrub(src.read_text(encoding="utf-8"))
    # the kept copy never holds anyone's names or pay (a calculator saved as a file carries its whole plan)
    (LABOR / "source" / "Labor_Calculator.html").write_text(html, encoding="utf-8")

    # ---- stylesheet (Tailwind v3, the same version the CDN script served)
    if a.no_css or 'id="offline-assets"' in html:
        pass
    else:
        build_css(src, a.tailwind)
    html = build_page(html)
    (LABOR / "page").mkdir(exist_ok=True)
    (LABOR / "page" / "labor.html").write_text(html, encoding="utf-8")
    print("Built labor/page/labor.html" + ("" if a.no_css or "id=\"offline-assets\"" in html else " and labor/static/labor/labor.css"))


def build_css(src, tailwind):
    accents = ["amber", "blue", "indigo", "slate", "violet", "emerald", "rose", "sky", "teal", "orange", "red", "green"]
    safelist = [f"{p}-{c}-{n}" for c in accents for p, ns in (("bg", (50, 100)), ("text", (700, 800))) for n in ns]
    safelist += [f"sm:grid-cols-{n}" for n in range(1, 7)]
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "tailwind.config.js"
        cfg.write_text("module.exports = { content: [%s], safelist: %s, theme: { extend: {} } };"
                       % (json.dumps(str(src)), json.dumps(safelist)))
        inp = Path(tmp) / "in.css"
        inp.write_text("@tailwind base;\n@tailwind components;\n@tailwind utilities;\n")
        out = LABOR / "static" / "labor" / "labor.css"
        subprocess.run([tailwind, "-c", str(cfg), "-i", str(inp), "-o", str(out), "--minify"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
