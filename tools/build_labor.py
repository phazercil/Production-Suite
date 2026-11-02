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
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LABOR = ROOT / "labor"


def patch(html, old, new, what, count=1):
    n = html.count(old)
    if n != count:
        sys.exit(f"build_labor: can't find {what} in the calculator (found {n} times, expected {count}). "
                 "Update tools/build_labor.py for the new version.")
    return html.replace(old, new)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source", nargs="?", default=str(LABOR / "source" / "Labor_Calculator.html"))
    ap.add_argument("--tailwind", default="tailwindcss")
    a = ap.parse_args()
    src = Path(a.source)
    html = src.read_text(encoding="utf-8")
    if src.resolve() != (LABOR / "source" / "Labor_Calculator.html").resolve():
        shutil.copyfile(src, LABOR / "source" / "Labor_Calculator.html")

    # ---- stylesheet (Tailwind v3, the same version the CDN script served)
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
        subprocess.run([a.tailwind, "-c", str(cfg), "-i", str(inp), "-o", str(out), "--minify"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # ---- page
    html = patch(html, '<script src="https://cdn.tailwindcss.com"></script>',
                 '<link rel="stylesheet" href="__LABOR_CSS__">\n  <link rel="stylesheet" href="__SUITEBAR_CSS__">', "the Tailwind CDN script")
    html = re.sub(r'\s*<link href="https://fonts\.googleapis\.com/[^"]*" rel="stylesheet">', "", html, count=1)
    if "googleapis" in html or "cdn.tailwindcss" in html:
        sys.exit("build_labor: the calculator still loads something from the internet.")
    html = patch(html, '''      try {
        const saved = localStorage.getItem(STORAGE_KEY);
        if (saved) return JSON.parse(saved);
      } catch (e) {
        console.warn('Could not read localStorage', e);
      }
      return JSON.parse(JSON.stringify(defaultState));''', '''      // Production Suite: the plan lives on the server (see static/labor/sync.js).
      try {
        const boot = LaborSync.boot(STORAGE_KEY);
        if (boot) return boot;
      } catch (e) {
        console.warn('Could not read the plan from the server page', e);
      }
      return JSON.parse(JSON.stringify(defaultState));''', "loadState()")
    html = patch(html, '''      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
      } catch (e) {
        console.warn('Could not save to localStorage', e);
      }''', '''      try {
        LaborSync.save(state);
      } catch (e) {
        console.warn('Could not save the plan to the server', e);
      }''', "persistState()")
    html = patch(html, '''      saveState();
      showToast('State successfully saved to browser storage!', 'success');''', '''      saveState();
      LaborSync.flush().then(ok => { if (ok) showToast('Plan saved on the server.', 'success'); });''', "manualSaveState()")
    html = patch(html, 'title="Save state to local storage"', 'title="Save the plan on the server"', "the Save button")
    # The suite is the app now: swap "Download App (.html)" for the server's saved versions (undo).
    html = patch(html, '''<button onclick="downloadStandaloneHTML()" class="bg-indigo-600 hover:bg-indigo-700 text-white text-xs px-3 py-2 rounded-lg font-semibold transition cursor-pointer" title="Download offline-ready HTML app">
          Download App (.html)
        </button>''', '''<button onclick="LaborSync.showHistory()" class="bg-indigo-600 hover:bg-indigo-700 text-white text-xs px-3 py-2 rounded-lg font-semibold transition cursor-pointer" title="Earlier saves of this plan; restore one to undo changes">
          Saved Versions
        </button>''', "the Download App button")
    m = re.search(r"<body[^>]*>", html)
    if not m:
        sys.exit("build_labor: no <body> tag.")
    html = html[:m.end()] + "\n__SUITEBAR__\n" + html[m.end():]
    html = patch(html, '<script id="app-script">',
                 '<script type="application/json" id="labor-boot">__LABOR_BOOT__</script>\n'
                 '<script src="__LABOR_SYNC_JS__"></script>\n<script id="app-script">', "the app script")
    (LABOR / "page").mkdir(exist_ok=True)
    (LABOR / "page" / "labor.html").write_text(html, encoding="utf-8")
    print("Built labor/page/labor.html and labor/static/labor/labor.css")


if __name__ == "__main__":
    main()
