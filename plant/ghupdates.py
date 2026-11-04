"""Updates from GitHub (Admin › Updates › Updates from GitHub).

An opt-in internet exception, like Paycom and Twilio: used only when an Admin saves a GitHub token here.
The server asks GitHub for the newest release of the suite's repository, downloads that release's signed
update file (ProductionSuite-Update-<version>-r<revision>.zip) and hands it to plant.updates, which checks
the signature, the files and the revision exactly as it does for an uploaded file. A file GitHub serves
that wasn't signed with the suite's update key is refused, so the token only lets the server download;
it can't make the server install anything else.

Settings (.env, never the database): GITHUB_UPDATES_REPO (owner/name), GITHUB_UPDATES_TOKEN (a
fine-grained token with Contents: Read-only on that one repository), GITHUB_UPDATES_CHECK=1 to check once a
day and post a notice when a newer release is out (it never installs by itself).
"""
import hashlib
import json
import logging
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from . import updates

log = logging.getLogger("plant")

API = "https://api.github.com"
DEFAULT_REPO = "phazercil/Production-Suite"
REPO_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
ASSET_RE = re.compile(r"^ProductionSuite-Update-(?P<version>[0-9][0-9A-Za-z.]*)-r(?P<revision>[0-9]+)(?P<packages>-with-packages)?\.zip$")
# where GitHub sends a release download: its API, then its file servers
HOSTS = ("api.github.com", "github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com")
MAX_BYTES = 400 * 1024 * 1024


class GHError(Exception):
    pass


def config():
    from . import liveconfig
    liveconfig.refresh()
    s = settings.SUITE
    return {"repo": s.get("GITHUB_UPDATES_REPO") or DEFAULT_REPO, "token": s.get("GITHUB_UPDATES_TOKEN") or "",
            "daily": s.get("GITHUB_UPDATES_CHECK") == "1"}


def ready():
    return bool(config()["token"])


class _Redirect(urllib.request.HTTPRedirectHandler):
    """Follow GitHub's redirect to its file servers: HTTPS to GitHub's own hosts only, and the token is sent
    to the API alone (the file server's link carries its own short-lived permission)."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        u = urllib.parse.urlsplit(newurl)
        if u.scheme != "https" or (u.hostname or "") not in HOSTS:
            raise GHError(f"GitHub sent the download somewhere unexpected ({u.hostname}). Nothing was downloaded.")
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and u.hostname != "api.github.com":
            new.headers.pop("Authorization", None)
            new.unredirected_hdrs.pop("Authorization", None)
        return new


# Like Paycom: the system's normal internet route, including a company proxy if there is one
_OPENER = urllib.request.build_opener(_Redirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))


def _open(path, token, accept="application/vnd.github+json", timeout=20):
    req = urllib.request.Request(API + path, headers={"Accept": accept, "Authorization": f"Bearer {token}",
                                                      "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "production-suite"})
    try:
        return _OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise GHError("GitHub refused the token (expired, or mistyped). Save a new one.")
        if e.code in (403, 404):
            raise GHError("GitHub didn't let this token read the repository. Check the repository name and that the token has Contents: Read-only on it.")
        raise GHError(f"GitHub answered {e.code} {e.reason}.")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise GHError(f"Couldn't reach GitHub ({getattr(e, 'reason', e)}). This server needs to reach api.github.com over HTTPS.")


def pick_asset(release):
    """The update file of a release: the one with the Python packages when there is one (it always installs)."""
    files = []
    for a in release.get("assets") or []:
        m = ASSET_RE.match(a.get("name") or "")
        if m:
            files.append((bool(m["packages"]), int(m["revision"]), m["version"], a))
    if not files:
        return None
    files.sort(key=lambda x: (x[1], x[0]), reverse=True)
    packages, revision, version, a = files[0]
    return {"id": a["id"], "name": a["name"], "size": a.get("size") or 0, "version": version, "revision": revision, "packages": packages,
            "digest": (a.get("digest") or "").removeprefix("sha256:")}


def check(cfg=None):
    """The newest release that has an update file, and whether it is newer than what's installed."""
    cfg = cfg or config()
    if not cfg["token"]:
        raise GHError("Set up updates from GitHub first (an Admin saves a GitHub token below).")
    if not REPO_RE.match(cfg["repo"]):
        raise GHError("The repository should look like owner/name.")
    with _open(f"/repos/{cfg['repo']}/releases?per_page=15", cfg["token"]) as r:
        releases = json.loads(r.read().decode() or "[]")
    v, rev = updates.current()
    best = None
    for rel in releases:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        a = pick_asset(rel)
        if a and (best is None or a["revision"] > best["asset"]["revision"]):
            best = {"tag": rel.get("tag_name", ""), "name": rel.get("name") or rel.get("tag_name", ""), "notes": (rel.get("body") or "")[:4000],
                    "published": rel.get("published_at") or "", "url": rel.get("html_url", ""), "asset": a}
    out = {"checked": timezone.now().isoformat(timespec="seconds"), "installed": {"version": v, "revision": rev}, "release": best,
           "newer": bool(best and best["asset"]["revision"] > rev)}
    _remember(out)
    return out


def download(asset, cfg=None):
    """Download a release's update file into updates/run, checked against GitHub's sha256 when it has one."""
    cfg = cfg or config()
    if not ASSET_RE.match(asset.get("name") or ""):
        raise GHError("That isn't an update file.")
    dest = updates.root() / "run" / "github-download.tmp"
    h, n = hashlib.sha256(), 0
    with _open(f"/repos/{cfg['repo']}/releases/assets/{int(asset['id'])}", cfg["token"], accept="application/octet-stream", timeout=60) as r, \
            open(dest, "wb") as out:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            n += len(chunk)
            if n > MAX_BYTES:
                raise GHError("That file is too large to be an update file.")
            h.update(chunk)
            out.write(chunk)
    if asset.get("digest") and h.hexdigest() != asset["digest"]:
        dest.unlink(missing_ok=True)
        raise GHError("The download doesn't match GitHub's checksum. Nothing was installed; try again.")
    return dest


def install(by, when="idle", at="", cfg=None):
    """Check GitHub, download the newest update file and hand it to the normal update checks."""
    cfg = cfg or config()
    c = check(cfg)
    if not c["release"]:
        raise GHError("No release on GitHub has an update file yet.")
    if not c["newer"]:
        a = c["release"]["asset"]
        raise GHError(f"Nothing newer: the newest release is v{a['version']} rev {a['revision']}, and this server has rev {c['installed']['revision']}.")
    a = c["release"]["asset"]
    path = download(a, cfg)
    try:
        return updates.receive(path, f"GitHub {c['release']['tag']} ({a['name']})", by, when, at)
    except updates.UpdError as e:
        raise GHError(str(e))
    finally:
        Path(path).unlink(missing_ok=True)


# ------------------------------------------------------------------ last check, and the daily check
def _file():
    return updates.root() / "github.json"


def _remember(result):
    try:
        _file().write_text(json.dumps(result), encoding="utf-8")
    except OSError:
        pass


def last():
    try:
        return json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def daily(now):
    """Run by the ticker: once a day when switched on, post a notice when a newer release is out."""
    cfg = config()
    if not cfg["token"] or not cfg["daily"]:
        return
    prev = last() or {}
    try:
        from datetime import datetime
        if prev.get("checked") and now - datetime.fromisoformat(prev["checked"]) < timedelta(hours=23):
            return
    except ValueError:
        pass
    try:
        c = check(cfg)
    except GHError as e:
        log.warning("GitHub update check: %s", e)
        _remember({**prev, "checked": now.isoformat(timespec="seconds"), "error": str(e)})
        return
    a = (c.get("release") or {}).get("asset")
    told = (prev.get("release") or {}).get("asset", {}).get("revision") if prev.get("newer") else None
    if c["newer"] and a and a["revision"] != told:
        from hmi.models import Notice
        Notice.objects.create(t=now, kind="update", text=f"Update v{a['version']} rev {a['revision']} is on GitHub. An Admin can install it in Admin › Updates.")
