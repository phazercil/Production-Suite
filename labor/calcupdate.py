"""Labor Calculator updates straight from its own GitHub repository (Admin › Updates › Labor Calculator).

The Labor Budget page is the Labor Calculator (its own repository, one HTML file) with the suite's changes put in
(labor/build.py). A newer calculator can be installed here without a new Production Suite release:
- check() asks GitHub for the newest commit that changed Labor_Calculator.html on the default branch;
- install() downloads that exact commit's file, builds the page from it (every hook checked; people and pay
  stripped from its built-in plan) and switches the Labor Budget page to it; the page it replaces is kept;
- undo() switches back to the page before; reset() goes back to the one that came with this suite version.
It uses the same opt-in GitHub token as suite updates (it needs read access to the calculator's repository too)
and only talks to GitHub. Installing is an Admin step (updates.install), done by hand, never automatic.
Files live in updates/labor-calc/ next to the suite's own updates: labor.html (the built page), calc.html (the
calculator as downloaded, with its built-in plan stripped) and meta.json, plus the .prev copies for undo. When the suite
itself is updated, the page is built again from calc.html with the new suite's changes; if that no longer fits, the
calculator that came with the suite is used until a newer one is installed.
"""
import json
import os
import urllib.parse
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from plant import ghupdates

from .build import BuildError, build, scrub

DEFAULT_REPO = "phazercil/Labor-Calculator"
FILE = "Labor_Calculator.html"
BUILT = "desktop/app/index.html"   # the offline build (styles and font inside), made by its tools/build_desktop.py
MAX_BYTES = 8 * 1024 * 1024
SHIPPED = Path(__file__).resolve().parent / "page" / "labor.html"


class CalcError(Exception):
    pass


def folder():
    return Path(settings.BASE_DIR) / "updates" / "labor-calc"


def repo():
    from plant import liveconfig
    liveconfig.refresh()
    return settings.SUITE.get("LABOR_CALC_REPO") or os.environ.get("LABOR_CALC_REPO") or DEFAULT_REPO


def _meta(name="meta.json"):
    try:
        return json.loads((folder() / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _revision():
    from suite.version import REVISION
    return REVISION


def page_path():
    """The Labor Budget page in use: one installed from GitHub, else the one that came with the suite."""
    f, m = folder(), _meta()
    p = f / "labor.html"
    if not (p.exists() and m.get("sha")):
        return SHIPPED
    if m.get("revision") != _revision():   # the suite was updated since: build it again with this suite's changes
        if m.get("stale") == _revision():
            return SHIPPED
        try:
            _write(p, build((f / "calc.html").read_text(encoding="utf-8")))
            m.pop("stale", None)
        except (OSError, BuildError, ValueError) as e:
            m["stale"], m["staleWhy"] = _revision(), str(e)[:300]
            _write(f / "meta.json", json.dumps(m))
            return SHIPPED
        m["revision"] = _revision()
        _write(f / "meta.json", json.dumps(m))
    return p


def info():
    m = _meta()
    if m:
        page_path()   # builds it again first if the suite was updated
        m = _meta()
    return {"repo": repo(), "installed": m or None, "prev": (folder() / "meta.prev.json").exists(), "latest": _meta("latest.json") or None,
            "ready": ghupdates.ready()}


def _token():
    cfg = ghupdates.config()
    if not cfg["token"]:
        raise CalcError("Turn on updates from GitHub first (GitHub token, above). It needs read access to the calculator's repository too.")
    return cfg["token"]


def _commit(r, tok, path, branch):
    q = urllib.parse.urlencode({"path": path, "sha": branch, "per_page": 1})
    with ghupdates._open(f"/repos/{r}/commits?{q}", tok) as resp:
        commits = json.loads(resp.read().decode())
    if not commits:
        return None
    c = commits[0]
    cm = c.get("commit") or {}
    return {"sha": c["sha"], "short": c["sha"][:7], "date": (cm.get("committer") or {}).get("date", ""),
            "message": (cm.get("message") or "").splitlines()[0][:200] if cm.get("message") else ""}


def check():
    """The newest commit on the default branch that changed the calculator's offline build (desktop/app/index.html,
    made by its tools/build_desktop.py), or its main file if that already carries its styles. Changes nothing installed."""
    tok, r = _token(), repo()
    try:
        with ghupdates._open(f"/repos/{r}", tok) as resp:
            branch = json.loads(resp.read().decode()).get("default_branch") or "main"
        built, src = _commit(r, tok, BUILT, branch), _commit(r, tok, FILE, branch)
    except ghupdates.GHError as e:
        raise CalcError(str(e))
    latest = built or src
    if not latest:
        raise CalcError(f"{r} has no {FILE} on its {branch} branch.")
    latest.update(branch=branch, checked=timezone.now().isoformat(),
                  behind=bool(built and src and src["sha"] != built["sha"] and src["date"] > built["date"]))
    folder().mkdir(parents=True, exist_ok=True)
    _write(folder() / "latest.json", json.dumps(latest))
    return latest


def _download(r, tok, path, sha):
    try:
        with ghupdates._open(f"/repos/{r}/contents/{urllib.parse.quote(path)}?ref={sha}", tok, accept="application/vnd.github.raw",
                             timeout=60) as resp:
            raw = resp.read(MAX_BYTES + 1)
    except ghupdates.GHError as e:
        raise CalcError(str(e))
    if len(raw) > MAX_BYTES:
        raise CalcError("The calculator file is larger than expected; nothing was installed.")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise CalcError("The calculator file isn't text; nothing was installed.")


def install(by, sha=None):
    """Download the calculator at that commit (the newest when not given), build the page and switch to it."""
    latest = _meta("latest.json") or {}
    sha = sha or latest.get("sha") or check()["sha"]
    if not (isinstance(sha, str) and len(sha) == 40 and all(ch in "0123456789abcdef" for ch in sha)):
        raise CalcError("That isn't a commit GitHub gave us. Check for updates again.")
    tok, r = _token(), repo()
    try:
        text = _download(r, tok, BUILT, sha)
    except CalcError:
        text = ""
    if 'id="offline-assets"' not in text:   # no offline build at that commit: the main file, if it carries its styles
        text = _download(r, tok, FILE, sha)
    try:
        src = scrub(text)
        html = build(src)
    except (BuildError, ValueError) as e:
        raise CalcError(f"This calculator version can't be installed: {e}")
    f = folder()
    f.mkdir(parents=True, exist_ok=True)
    cur = page_path()
    if cur == SHIPPED:
        _write(f / "meta.prev.json", json.dumps({"shipped": True}))
    else:
        _write(f / "labor.prev.html", cur.read_text(encoding="utf-8"))
        _write(f / "calc.prev.html", (f / "calc.html").read_text(encoding="utf-8"))
        _write(f / "meta.prev.json", json.dumps(_meta()))
    _write(f / "calc.html", src)
    _write(f / "labor.html", html)
    same = latest.get("sha") == sha
    meta = {"sha": sha, "short": sha[:7], "repo": r, "installed": timezone.now().isoformat(), "by": by, "revision": _revision(),
            "message": latest.get("message", "") if same else "", "date": latest.get("date", "") if same else ""}
    _write(f / "meta.json", json.dumps(meta))
    return meta


def undo():
    """Back to the Labor Budget page that was in use before the last install."""
    f = folder()
    if not (f / "meta.prev.json").exists():
        raise CalcError("There's no earlier calculator to go back to.")
    prev = _meta("meta.prev.json")
    if prev.get("shipped") or not prev.get("sha") or not (f / "calc.prev.html").exists():
        return reset()
    _write(f / "calc.html", (f / "calc.prev.html").read_text(encoding="utf-8"))
    _write(f / "labor.html", (f / "labor.prev.html").read_text(encoding="utf-8"))
    _write(f / "meta.json", json.dumps(prev))
    for n in ("labor.prev.html", "calc.prev.html", "meta.prev.json"):
        (f / n).unlink(missing_ok=True)
    page_path()   # built again if it was made by an older suite
    return prev


def reset():
    """Back to the calculator that came with this Production Suite version."""
    f = folder()
    for n in ("labor.html", "calc.html", "meta.json", "labor.prev.html", "calc.prev.html", "meta.prev.json"):
        (f / n).unlink(missing_ok=True)
    return {}
