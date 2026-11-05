"""Turns the Labor Calculator's single HTML file into the suite's Labor Budget page (labor/page/labor.html).

Used by tools/build_labor.py when the suite is built, and by labor/calcupdate.py when Admin › Updates installs a
newer calculator straight from its own GitHub repository. Every change is checked: if the calculator's code no
longer matches a part the suite hooks into, BuildError says which part and nothing is changed.
"""
import json
import re


class BuildError(Exception):
    pass


def build(html):
    """Calculator HTML in, the suite's page out (people and pay stripped from its built-in plan first)."""
    if 'id="offline-assets"' not in html:
        raise BuildError("This calculator still loads its styles from the internet (no offline-assets block). It needs the "
                         "full build (tools/build_labor.py with the Tailwind CLI), so it can only come with a suite update.")
    return build_page(scrub(html))


def patch(html, old, new, what, count=1):
    n = html.count(old)
    if n != count:
        raise BuildError(f"Can't find {what} in the calculator (found {n} times, expected {count}). This calculator changed a "
                         "part the suite hooks into, so it needs a Production Suite update (labor/build.py) first.")
    return html.replace(old, new)


def scrub(html):
    """The calculator's built-in example plan, without people: no names on crew slots, no wages, no project name.
    A calculator downloaded from its own page carries the whole plan it was showing."""
    m = re.search(r"(/\*EMBEDDED_STATE_START\*/\s*const defaultState = )(.*?)(;\s*/\*EMBEDDED_STATE_END\*/)", html, re.S)
    if not m:
        raise BuildError("Can't find the calculator's built-in plan (EMBEDDED_STATE).")
    d = json.loads(m.group(2))
    d["slotAssignments"] = {}
    d["projectName"] = ""
    for p in d.get("positionsDefaults") or []:
        if isinstance(p, dict) and "defaultWage" in p:
            p["defaultWage"] = 0
    return html[:m.start(2)] + json.dumps(d, indent=2) + html[m.end(2):]


def build_page(html):
    if 'id="offline-assets"' in html:
        # newer calculators carry Tailwind and their font inside the file: nothing to build or fetch
        html = patch(html, "</head>", '  <link rel="stylesheet" href="__SUITEBAR_CSS__">\n</head>', "</head>")
    else:
        html = patch(html, '<script src="https://cdn.tailwindcss.com"></script>',
                     '<link rel="stylesheet" href="__LABOR_CSS__">\n  <link rel="stylesheet" href="__SUITEBAR_CSS__">', "the Tailwind CDN script")
    html = re.sub(r'\s*<link href="https://fonts\.googleapis\.com/[^"]*" rel="stylesheet">', "", html, count=1)
    if "googleapis" in html or "cdn.tailwindcss" in html:
        raise BuildError("The calculator still loads something from the internet.")
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
        raise BuildError("The calculator has no <body> tag.")
    html = html[:m.end()] + "\n__SUITEBAR__\n" + html[m.end():]
    html = patch(html, '<script id="app-script">',
                 '<script type="application/json" id="labor-boot">__LABOR_BOOT__</script>\n'
                 '<script src="__LABOR_SYNC_JS__"></script>\n<script id="app-script">', "the app script")
    # in step with the rest of the suite: lines and shifts (labor/suite_patches.py)
    from .suite_patches import apply
    return apply(html, patch)


