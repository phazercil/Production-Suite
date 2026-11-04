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


def patch(html, old, new, what, count=1):
    n = html.count(old)
    if n != count:
        sys.exit(f"build_labor: can't find {what} in the calculator (found {n} times, expected {count}). "
                 "Update tools/build_labor.py for the new version.")
    return html.replace(old, new)


def scrub(html):
    """The calculator's built-in example plan, without people: no names on crew slots, no wages, no project name.
    A calculator downloaded from its own page carries the whole plan it was showing."""
    m = re.search(r"(/\*EMBEDDED_STATE_START\*/\s*const defaultState = )(.*?)(;\s*/\*EMBEDDED_STATE_END\*/)", html, re.S)
    if not m:
        sys.exit("build_labor: can't find the calculator's built-in plan (EMBEDDED_STATE).")
    d = json.loads(m.group(2))
    d["slotAssignments"] = {}
    d["projectName"] = ""
    for p in d.get("positionsDefaults") or []:
        if isinstance(p, dict) and "defaultWage" in p:
            p["defaultWage"] = 0
    return html[:m.start(2)] + json.dumps(d, indent=2) + html[m.end(2):]


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


def build_page(html):
    if 'id="offline-assets"' in html:
        # newer calculators carry Tailwind and their font inside the file: nothing to build or fetch
        html = patch(html, "</head>", '  <link rel="stylesheet" href="__SUITEBAR_CSS__">\n</head>', "</head>")
    else:
        html = patch(html, '<script src="https://cdn.tailwindcss.com"></script>',
                     '<link rel="stylesheet" href="__LABOR_CSS__">\n  <link rel="stylesheet" href="__SUITEBAR_CSS__">', "the Tailwind CDN script")
    html = re.sub(r'\s*<link href="https://fonts\.googleapis\.com/[^"]*" rel="stylesheet">', "", html, count=1)
    if "googleapis" in html or "cdn.tailwindcss" in html:
        sys.exit("build_labor: the calculator still loads something from the internet.")
    # the budgets live on the server (static/labor/sync.js): A is the plant's plan, B a second budget to compare
    html = patch(html, """        const saved = localStorage.getItem(localStorage.getItem('MFG_AB_ACTIVE') === 'B' ? STORAGE_KEY + '__B' : STORAGE_KEY);
        if (saved) return JSON.parse(saved);""", """        const boot = LaborSync.boot(STORAGE_KEY);   // Production Suite: from the server
        if (boot) return boot;""", "loadState()")
    html = patch(html, "        localStorage.setItem(budgetKey(activeBudget()), JSON.stringify(state));",
                 "        LaborSync.save(state, activeBudget());", "persistState()")
    html = patch(html, "      try { const t = localStorage.getItem(budgetKey(which)); return t ? JSON.parse(t) : null; } catch (e) { return null; }",
                 "      return LaborSync.read(which);", "readBudget()")
    html = patch(html, "        try { localStorage.setItem(budgetKey(which), JSON.stringify(next)); } catch (e) {}",
                 "        LaborSync.write(which, next);", "switchBudget()")
    html = patch(html, "        try { localStorage.setItem(budgetKey(other), JSON.stringify(copy)); } catch (e) {}",
                 "        LaborSync.write(other, copy);", "copyBudget()")
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
    # in step with the rest of the suite: lines, speeds and shifts come from it (tools/labor_suite_sync.py)
    sys.path.insert(0, str(ROOT / "tools"))
    import labor_suite_sync
    return labor_suite_sync.apply(html, patch)


if __name__ == "__main__":
    main()
