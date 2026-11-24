"""Updates without the installer (Operator Kiosk > Admin > Updates).

A shift lead uploads an update file, or IT drops one in the install folder's updates/inbox. The suite
checks it (signature, files, newer revision, packages) and holds it in updates/ready until it is due:
right away, when no line is running a shift, or at a set time. Then it starts the updater (tools/updater.py,
through the "Production Suite Updater" scheduled task on Windows), which stops the suite, backs it up,
installs, and goes back by itself if the new version doesn't come up. See tools/updater.py.
"""
import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from . import updatefile
from .update_key import PUBLIC_KEYS

log = logging.getLogger("plant")
TASK_UPDATER = "Production Suite Updater"
WHEN = ("now", "idle", "at")


class UpdError(Exception):
    pass


def base():
    return Path(settings.BASE_DIR)


def root():
    r = base() / "updates"
    for d in ("inbox", "ready", "run", "rejected"):
        (r / d).mkdir(parents=True, exist_ok=True)
    return r


def _read(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path, data):
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def state():
    return _read(root() / "state.json", {"status": "idle"})


def set_state(**kw):
    cur = state()
    cur.update(kw, at=timezone.localtime().isoformat(timespec="seconds"))
    _write(root() / "state.json", cur)
    return cur


def policy():
    p = _read(root() / "policy.json", {})
    return {"folderWhen": p.get("folderWhen") if p.get("folderWhen") in WHEN[:2] + ("hold",) else "idle"}


def set_policy(folder_when):
    if folder_when not in ("now", "idle", "hold"):
        raise UpdError("Pick when files dropped in the folder install.")
    _write(root() / "policy.json", {"folderWhen": folder_when})


def current():
    return settings.SUITE["VERSION"], int(settings.SUITE["REVISION"])


def backups():
    r = base() / "backups"
    out = []
    if r.is_dir():
        for p in r.iterdir():
            m = _read(p / "meta.json", None)
            if m and int(m.get("revision") or 0) < current()[1]:
                out.append({"version": m.get("version"), "revision": m.get("revision"), "at": m.get("at"), "mtime": p.stat().st_mtime})
    return sorted(out, key=lambda x: x["mtime"], reverse=True)


def info():
    v, r = current()
    st = state()
    ready = next(iter(sorted((root() / "ready").glob("*.zip"))), None)
    if st.get("status") in ("ready", "scheduled") and not ready and st.get("job") != "goback":   # going back needs no file
        st = set_state(status="idle", message="")
    return {"version": v, "revision": r, "state": st, "policy": policy(), "folder": str(root() / "inbox"),
            "backups": [{k: b[k] for k in ("version", "revision", "at")} for b in backups()][:5],
            "installed": _read(base() / "install-info.json", {}).get("updated") or _read(base() / "install-info.json", {}).get("installed", "")}


def _requirements_hash():
    import hashlib
    p = base() / "requirements-windows.txt"
    return hashlib.sha256((p.read_bytes().decode("utf-8") if p.exists() else "").encode()).hexdigest()


def check(path):
    """Verify an update file and that it can go on this install. Returns its manifest."""
    try:
        m = updatefile.verify(path, PUBLIC_KEYS)
    except updatefile.UpdateError as e:
        raise UpdError(str(e))
    v, r = current()
    if int(m["revision"]) <= r:
        raise UpdError(f"That update is v{m['version']} rev {m['revision']}; v{v} rev {r} is already installed. "
                       "Updates only go forward (use Go back for an older version).")
    if m.get("requirements") and m["requirements"] != _requirements_hash() and not m["hasPackages"]:
        raise UpdError("That update changes the Python packages but doesn't carry them. Use the update file built with packages.")
    return m


def busy():
    return state().get("status") in ("starting", "installing")


def receive(src_path, name, by, when="now", at=""):
    """Take a checked update file into updates/ready (replacing one waiting there)."""
    if busy():
        raise UpdError("An update is being installed right now. Wait for it to finish.")
    m = check(src_path)
    when, at_iso = _when(when, at)
    for old in (root() / "ready").glob("*.zip"):
        old.unlink()
    dest = root() / "ready" / f"ProductionSuite-Update-{m['version']}-r{m['revision']}.zip"
    shutil.copyfile(src_path, dest)
    set_state(status="ready", job="apply", file=dest.name, version=m["version"], revision=m["revision"], notes=m.get("notes", ""),
              built=m.get("built", ""), packages=m["hasPackages"], when=when, whenAt=at_iso, by=by, message="", log=[],
              source=name)
    log.info("update v%s rev %s received from %s (%s), installs %s", m["version"], m["revision"], by, name, when)
    return m


def _when(when, at):
    if when not in WHEN:
        raise UpdError("Pick when to install it.")
    if when != "at":
        return when, ""
    try:
        t = datetime.fromisoformat(str(at))
    except ValueError:
        raise UpdError("Pick the date and time to install it.")
    if timezone.is_naive(t):
        t = timezone.make_aware(t)
    if t < timezone.now() - timedelta(minutes=1):
        raise UpdError("That time has already passed.")
    return when, t.isoformat(timespec="minutes")


def schedule(when, at, by):
    st = state()
    if st.get("status") != "ready":
        raise UpdError("There's no update waiting to install.")
    when, at_iso = _when(when, at)
    set_state(when=when, whenAt=at_iso, by=by)


def cancel(by):
    if busy():
        raise UpdError("The update is already installing.")
    for old in (root() / "ready").glob("*.zip"):
        old.unlink()
    set_state(status="idle", message=f"Waiting update removed by {by}.", log=[])


def request_goback(by):
    if busy():
        raise UpdError("An update is being installed right now.")
    b = backups()
    if not b:
        raise UpdError("There is no earlier version to go back to.")
    set_state(status="ready", job="goback", file="", version=b[0]["version"], revision=b[0]["revision"], when="now", whenAt="", by=by,
              message="", log=[], notes="")


def _due(st, now):
    w = st.get("when")
    if w == "now":
        return True
    if w == "idle":
        from hmi.models import ShiftRun
        return not ShiftRun.objects.filter(active=True).exists()
    if w == "at" and st.get("whenAt"):
        try:
            return now >= datetime.fromisoformat(st["whenAt"])
        except ValueError:
            return True
    return False


def _take_inbox(now):
    pol = policy()
    for f in sorted((root() / "inbox").glob("*")):
        if not f.is_file() or busy():
            continue
        if f.suffix.lower() != ".zip":
            continue
        # leave a file that is still being copied in
        if (now.timestamp() - f.stat().st_mtime) < 20:
            continue
        try:
            receive(f, f.name, "update folder", "idle" if pol["folderWhen"] == "hold" else pol["folderWhen"])
            if pol["folderWhen"] == "hold":
                set_state(when="hold")
            f.unlink()
        except (UpdError, OSError) as e:
            stamp = timezone.localtime(now).strftime("%Y%m%d-%H%M%S")
            try:
                shutil.move(str(f), str(root() / "rejected" / f"{stamp}-{f.name}"))
            except OSError:
                pass
            if not busy():
                set_state(status="rejected", message=f"{f.name} from the update folder was not installed: {e}", file=f.name)
            log.warning("update file %s rejected: %s", f.name, e)


def _prepare_run():
    run = root() / "run"
    shutil.copyfile(base() / "tools" / "updater.py", run / "updater.py")
    shutil.copyfile(base() / "plant" / "updatefile.py", run / "updatefile.py")
    shutil.copyfile(base() / "plant" / "ed25519.py", run / "ed25519.py")
    _write(run / "keys.json", list(PUBLIC_KEYS))
    return run


def launch(st):
    """Start the updater for the waiting job. It runs on its own and outlives the suite's processes."""
    run = _prepare_run()
    args = ["apply", str(root() / "ready" / st["file"])] if st.get("job") == "apply" else ["goback"]
    _write(run / "request.json", {"args": args})
    set_state(status="starting", message="Starting the updater", log=[])
    if os.name == "nt":
        r = subprocess.run(["schtasks", "/Run", "/TN", TASK_UPDATER], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            set_state(status="failed", message="The updater task is missing or couldn't start "
                      f"({(r.stderr or r.stdout).strip()[:200]}). Run Setup.exe once to add it; nothing was changed.")
            return False
        return True
    if Path("/etc/systemd/system/production-suite-updater.path").exists():
        return True  # Linux install: systemd sees request.json and runs the updater as root
    py = base() / ".venv" / "bin" / "python"
    subprocess.Popen([str(py) if py.exists() else sys.executable, str(run / "updater.py"), "--dir", str(base()), "request"],
                     cwd=str(base()), start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def tick(now=None):
    """Called by the ticker: take files from the update folder and start a due update."""
    now = now or timezone.now()
    try:
        _take_inbox(now)
        st = state()
        if st.get("status") == "starting" and st.get("at"):
            # the updater never picked it up (task disabled, server busy): say so after 5 minutes
            if (now - datetime.fromisoformat(st["at"])).total_seconds() > 300:
                set_state(status="failed", message="The updater didn't start within 5 minutes. Check the \"Production Suite Updater\" "
                          "scheduled task, or run Setup.exe; nothing was changed.")
        if st.get("status") == "ready" and _due(st, now):
            launch(st)
    except Exception:
        log.exception("update check failed")

