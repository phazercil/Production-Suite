"""Scheduled database backups, a copy off this server, and a check that each copy can be restored.

Admin › Backup › Scheduled backups and off-server copy (settings["dbBackup"]):
- every:  hours between automatic backups (0 = only when someone presses Back up now)
- copyTo: a folder on another machine: a Windows share (\\\\nas01\\backups\\suite) or, on Linux, a network share
  mounted on this server (/mnt/plant-nas/suite). It must be on the plant network: a share's host is checked like
  every other address the suite reaches. Nothing is sent to the internet.
- keep:   how many copies to keep there (the newest; 0 keeps them all)

After each copy the copy itself is read back and checked:
- it is a complete gzip file with the same SHA-256 as the backup it came from;
- a SQLite backup opens and passes SQLite's integrity check (a test restore into a temporary file);
- a MariaDB backup runs to its "-- end of backup" line with every table's CREATE in it.
The result (and when) shows on Admin › Health and Admin › Backup.
"""
import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path, PureWindowsPath

from django.conf import settings

from . import dbbackup

STATE = "offsite.json"


class OffsiteError(Exception):
    pass


def config():
    from plant.models import PlantSettings
    c = (PlantSettings.load().settings or {}).get("dbBackup") or {}
    return {"every": float(c.get("every") or 0), "copyTo": str(c.get("copyTo") or "").strip(), "keep": int(c.get("keep") or 30)}


def check_dest(path):
    """The folder, if it is somewhere a copy may go: not inside the suite's own folder, and a share on the plant
    network when it names a host."""
    p = str(path or "").strip()
    if not p:
        raise OffsiteError("Type the folder to copy backups to.")
    if p.startswith(("\\\\", "//")):
        host = re.split(r"[\\/]+", p.lstrip("\\/"))[0]
        from plant import netsafety
        try:
            netsafety.check_host(host, "The backup share")
        except netsafety.NotPrivate as e:
            raise OffsiteError(str(e))
    elif not (Path(p).is_absolute() or PureWindowsPath(p).is_absolute()):
        raise OffsiteError("Type the whole path of the folder, like \\\\nas01\\backups\\suite or /mnt/plant-nas/suite.")
    try:
        base = Path(settings.BASE_DIR).resolve()
        if Path(p).resolve().is_relative_to(base):
            raise OffsiteError("That folder is inside the suite's own folder: pick one on another machine.")
    except (OSError, ValueError):
        pass
    return p


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def verify(path):
    """Read a backup file all the way through and check it. Returns a sentence; raises OffsiteError.
    SQLite backups are test-restored (opened and integrity-checked). MariaDB backups get a dump check: the whole file
    reads, every table's definition is there and it reaches the end marker. That is not a restore (that needs a
    spare MariaDB database), and the sentence says so."""
    name = Path(path).name
    try:
        if name.endswith(".sqlite3.gz"):
            with tempfile.TemporaryDirectory() as d:
                plain = Path(d) / "restore-test.sqlite3"
                with gzip.open(path, "rb") as i, open(plain, "wb") as o:
                    shutil.copyfileobj(i, o, 1 << 20)
                with sqlite3.connect(plain) as c:
                    r = c.execute("PRAGMA integrity_check").fetchone()[0]
                    n = c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
                if r != "ok":
                    raise OffsiteError(f"The copy doesn't restore cleanly: {r}")
                return f"Test restore passed ({n} tables)."
        tables, last = 0, ""
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("CREATE TABLE"):
                    tables += 1
                if line.strip():
                    last = line.strip()
        if last != "-- end of backup":
            raise OffsiteError("The copy is cut short (it doesn't reach the end of the backup).")
        return f"Dump check passed: complete, {tables} tables (a dump check, not a test restore)."
    except (OSError, EOFError, gzip.BadGzipFile, sqlite3.DatabaseError) as e:
        raise OffsiteError(f"The copy can't be read back: {e}")


def _state_path():
    return dbbackup.folder() / STATE


def status():
    try:
        s = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        s = {}
    c = config()
    s.update({"on": bool(c["copyTo"]), "copyTo": c["copyTo"], "every": c["every"], "keep": c["keep"]})
    return s


def _save(**kw):
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"t": time.time(), **kw}), encoding="utf-8")


def copy(entry=None):
    """Copy the newest backup (or `entry`) to the off-server folder and check the copy. Returns a sentence."""
    c = config()
    dest = Path(check_dest(c["copyTo"]))
    items = dbbackup.listing()
    entry = entry or (items[0] if items else None)
    if not entry:
        raise OffsiteError("There is no backup to copy yet.")
    src = dbbackup.folder() / entry["name"]
    try:
        dest.mkdir(parents=True, exist_ok=True)
        part = dest / (entry["name"] + ".part")
        shutil.copyfile(src, part)
        os.replace(part, dest / entry["name"])
    except OSError as e:
        _save(ok=False, name=entry["name"], msg=f"Couldn't copy to {dest}: {e}")
        raise OffsiteError(f"Couldn't copy the backup to {dest}: {e}")
    got = dest / entry["name"]
    try:
        if _sha(got) != _sha(src):
            raise OffsiteError("The copy doesn't match the backup (it changed on the way).")
        how = verify(got)
    except OffsiteError as e:
        _save(ok=False, name=entry["name"], msg=str(e))
        raise
    if c["keep"]:
        old = sorted((p for p in dest.iterdir() if dbbackup.NAME_RE.match(p.name)), key=lambda p: p.name, reverse=True)[c["keep"]:]
        for p in old:
            try:
                p.unlink()
            except OSError:
                pass
    msg = f"{entry['name']} copied to {dest} and checked. {how}"
    _save(ok=True, name=entry["name"], msg=msg)
    return msg


def tick(state, t):
    """Called by the ticker: a backup when one is due, then its off-server copy."""
    c = config()
    if not c["every"]:
        return None
    items = dbbackup.listing()
    last = items[0]["at"].timestamp() if items else 0
    if time.time() - last < c["every"] * 3600 - 60:
        return None
    entry = dbbackup.make()
    if c["copyTo"]:
        return copy(entry)
    return f"{entry['name']} made."
