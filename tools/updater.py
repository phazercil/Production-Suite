"""Installs an update file, or goes back to the previous version, while the suite is stopped.

Started by the suite (Admin > Updates, or a file dropped in updates/inbox) through the "Production Suite
Updater" scheduled task, which runs run-update.cmd. Before starting it, the suite copies this script,
ed25519.py, updatefile.py and the trusted public keys into updates/run, so nothing here is replaced while
it runs.

    python updates/run/updater.py --dir C:\\ProductionSuite apply updates/ready/<file>.zip
    python updates/run/updater.py --dir C:\\ProductionSuite goback

What "apply" does, and how it backs out:
  1. checks the file again (signature, every file's checksum, newer revision, packages it needs)
  2. notes which database migrations are applied now
  3. stops the web app and the ticker
  4. copies the current program files (and packages) to backups/<version>-r<revision>-<time>
  5. replaces the program files (settings, records, logs and the Python environment are kept)
  6. installs the packages if the update changed them
  7. updates the database tables (migrate) and the static files
  8. starts the suite and waits for it to answer with the new revision
If anything from step 3 on fails, it puts back the backed-up files, takes the database tables back to how
they were, starts the old version again and says so on Admin > Updates. "goback" does the same with the
newest backup, on request.

Progress goes to updates/state.json (read by Admin > Updates) and logs/update.log.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import updatefile  # noqa: E402

LINUX_ROOT_DIR = Path("/usr/libexec/production-suite")
TASK_WEB, TASK_TICKER, TASK_CONTROL = "Production Suite Web", "Production Suite Ticker", "Production Suite Control"
KEEP_BACKUPS = 3
WIN = os.name == "nt"


class Failed(Exception):
    pass


# ------------------------------------------------------------------ state and log
class Run:
    def __init__(self, install, services=None, health=None, python=None):
        self.dir = Path(install).resolve()
        self.upd = self.dir / "updates"
        self.upd.mkdir(exist_ok=True)
        (self.dir / "logs").mkdir(exist_ok=True)
        self.services = services or Services(self.dir)
        self.health = health or self._health
        self.python = python or self._python()
        self.lines = []

    def _python(self):
        p = self.dir / ".venv" / ("Scripts/python.exe" if WIN else "bin/python")
        return str(p) if p.exists() else sys.executable

    def state(self, **kw):
        path = self.upd / "state.json"
        try:
            cur = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cur = {}
        cur.update(kw)
        cur["log"] = self.lines[-60:]
        cur["at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        updatefile.safe_write(path, json.dumps(cur, indent=1))

    def say(self, msg):
        line = f"{datetime.now():%H:%M:%S} {msg}"
        self.lines.append(line)
        try:
            updatefile.safe_write(self.dir / "logs" / "update.log", f"{datetime.now():%Y-%m-%d} {line}\n", mode="a")
        except OSError:
            pass
        print(line, flush=True)
        self.state()

    # -------------------------------------------------------------- helpers
    def owner(self):
        """Linux, run as root: the account that owns the install (psuite). The suite's own commands run as it,
        and files the update puts in place are given to it."""
        if WIN or not hasattr(os, "geteuid") or os.geteuid() != 0:
            return None
        st = self.dir.stat()
        return (st.st_uid, st.st_gid) if st.st_uid != 0 else None

    def fix_owner(self):
        o = self.owner()
        if not o:
            return
        for root, dirs, files in os.walk(self.dir):
            for n in dirs + files:
                try:
                    os.chown(os.path.join(root, n), *o, follow_symlinks=False)
                except OSError:
                    pass

    def manage(self, *args, check=True, timeout=1800):
        env = dict(os.environ, PYTHONUTF8="1", DJANGO_SETTINGS_MODULE="suite.settings", TICKER_IN_PROCESS="0")
        o = self.owner()
        extra = {"user": o[0], "group": o[1]} if o else {}
        if o:
            self.fix_owner()  # files just put in place must be readable by the account that runs the command
        r = subprocess.run([self.python, "manage.py", *args], cwd=self.dir, env=env, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace", **extra)
        out = (r.stdout or "") + (r.stderr or "")
        if check and r.returncode != 0:
            tail = " | ".join(x.strip() for x in out.strip().splitlines()[-4:])
            raise Failed(f"manage.py {' '.join(args)} failed: {tail[:400]}")
        return out

    def leaves(self):
        """{app: newest applied migration} for the suite's own records database."""
        out = self.manage("showmigrations", "--list", "--database", "default")
        res, app = {}, None
        for line in out.splitlines():
            if line and not line.startswith(" "):
                app = line.strip()
                res.setdefault(app, None)
            else:
                m = re.match(r"\s*\[X\]\s+(\S+)", line)
                if m and app:
                    res[app] = m.group(1)
        return res

    def migrate_back(self, before):
        """Take the tables back to `before` ({app: migration or None}), using the program files now in place."""
        now = self.leaves()
        for app, leaf in now.items():
            want = before.get(app)
            if leaf and leaf != want:
                self.say(f"Taking the {app} tables back to {want or 'before it existed'}")
                self.manage("migrate", app, want or "zero", "--noinput", "--database", "default")

    def port(self):
        try:
            for line in (self.dir / ".env").read_text(encoding="utf-8-sig").splitlines():
                m = re.match(r'\s*WEB_PORT\s*=\s*"?(\d+)', line)
                if m:
                    return int(m.group(1))
        except OSError:
            pass
        return 8000

    def _health(self, revision, wait=150):
        url = f"http://127.0.0.1:{self.port()}/api/version"
        end = time.time() + wait
        last = ""
        while time.time() < end:
            try:
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=5) as r:
                    data = json.loads(r.read().decode() or "{}")
                if int(data.get("revision", -1)) == int(revision):
                    return True, f"answering as v{data.get('version')} rev {data.get('revision')}"
                last = f"answering as rev {data.get('revision')}, not {revision}"
            except Exception as e:  # not up yet
                last = str(getattr(e, "reason", e))
            time.sleep(3)
        return False, f"the suite didn't answer as rev {revision} within {wait} s ({last})"

    # -------------------------------------------------------------- files
    def backup(self, version, revision):
        b = self.dir / "backups" / f"{version}-r{revision}-{datetime.now():%Y%m%d-%H%M%S}"
        (b / "app").mkdir(parents=True)
        for rel in updatefile.app_files(self.dir):
            dst = b / "app" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.dir / rel, dst, follow_symlinks=False)
        if (self.dir / "wheels").is_dir():
            shutil.copytree(self.dir / "wheels", b / "wheels")
        info = self.info()
        meta = {"version": version, "revision": revision, "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "packagesHash": info.get("packagesHash", ""), "requirements": self.requirements()}
        updatefile.safe_write(b / "meta.json", json.dumps(meta, indent=1))
        return b

    def clear_program_files(self):
        for p in list(self.dir.iterdir()):
            if updatefile.preserved(p.name):
                continue
            if p.is_symlink():
                p.unlink()
            elif p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()

    def restore(self, b):
        self.clear_program_files()
        shutil.copytree(b / "app", self.dir, dirs_exist_ok=True,
                        ignore=lambda d, names: [n for n in names if os.path.islink(os.path.join(d, n))])
        self.grant_desktop()

    def grant_desktop(self):
        """Ordinary users may read the desktop folder (Production Suite Admin and the tray icon run as them);
        new files would otherwise only get the install folder's administrators-only rule. On Linux, files the
        update put in place go to the suite's own account."""
        self.fix_owner()
        d = self.dir / "desktop"
        if WIN and d.is_dir():
            subprocess.run(["icacls", str(d), "/grant", "*S-1-5-32-545:(OI)(CI)RX", "/T", "/C", "/Q"], capture_output=True, timeout=120)

    def refresh_root_copy(self, zip_path):
        """Linux: the updater and network helper root runs live in a root-owned folder (/usr/libexec/production-suite),
        with the trusted update keys. Bring them up to date from the update file just checked and installed."""
        if WIN or HERE != LINUX_ROOT_DIR or not os.access(str(HERE), os.W_OK):
            return
        import zipfile
        try:
            with zipfile.ZipFile(zip_path) as z:
                for member, name in (("app/tools/updater.py", "updater.py"), ("app/plant/updatefile.py", "updatefile.py"),
                                     ("app/plant/ed25519.py", "ed25519.py"), ("app/tools/linux_apply.py", "linux_apply.py")):
                    if member in z.namelist():
                        updatefile.safe_write(HERE / name, z.read(member))
                        os.chmod(HERE / name, 0o644)
                if "app/plant/update_key.py" in z.namelist():
                    keys = re.findall(r'"([A-Za-z0-9+/=]{40,60})"', z.read("app/plant/update_key.py").decode("utf-8"))
                    if keys:
                        updatefile.safe_write(HERE / "keys.json", json.dumps(keys))
                        os.chmod(HERE / "keys.json", 0o644)
        except (OSError, KeyError, zipfile.BadZipFile) as e:
            self.say(f"Couldn't refresh the updater in {HERE}: {e}")

    def ensure_shortcuts(self):
        """The Production Suite Admin shortcut on the desktop and in the Start menu, made again after every
        update (same as Setup.exe), so servers updated here have it and it always points at this install."""
        exe = self.dir / "desktop" / "SuiteAdmin.exe"
        if not WIN or not exe.exists():
            return
        q = lambda s: str(s).replace("'", "''")  # noqa: E731
        ps = ("$made = @(); $places = @(@([Environment]::GetFolderPath('CommonDesktopDirectory'), 'desktop'), "
              "@((Join-Path ([Environment]::GetFolderPath('CommonPrograms')) 'Production Suite'), 'Start menu')); "
              "$st = [Environment]::GetFolderPath('CommonStartup'); if ($st) { $s0 = New-Object -ComObject WScript.Shell; "
              f"$t0 = $s0.CreateShortcut((Join-Path $st 'Production Suite.lnk')); $t0.TargetPath = '{q(exe)}'; $t0.Arguments = '--tray'; "
              f"$t0.WorkingDirectory = '{q(self.dir)}'; $t0.IconLocation = '{q(self.dir / 'desktop' / 'suite.ico')},0'; $t0.Save() }}; "
              "foreach ($pl in $places) { if (-not $pl[0]) { continue }; "
              "if (-not (Test-Path $pl[0])) { New-Item -ItemType Directory -Path $pl[0] -Force | Out-Null }; "
              "$s = New-Object -ComObject WScript.Shell; $l = $s.CreateShortcut((Join-Path $pl[0] 'Production Suite Admin.lnk')); "
              f"$l.TargetPath = '{q(exe)}'; $l.WorkingDirectory = '{q(self.dir)}'; $l.IconLocation = '{q(self.dir / 'desktop' / 'suite.ico')},0'; "
              "$l.Description = 'Production Suite server settings: connections, start, stop and restart'; $l.Save(); "
              "if (Test-Path (Join-Path $pl[0] 'Production Suite Admin.lnk')) { $made += $pl[1] } }; "
              "Write-Output ('made:' + ($made -join ','))")
        try:
            r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True, timeout=60)
            made = next((x[5:] for x in (r.stdout or "").splitlines() if x.startswith("made:")), "")
            if made:
                self.say(f"Production Suite Admin shortcut is on the {made.replace(',', ' and the ')}")
            else:
                self.say("Couldn't add the Production Suite Admin shortcut; open it from " + str(exe))
        except (OSError, subprocess.TimeoutExpired):
            self.say("Couldn't add the Production Suite Admin shortcut; open it from " + str(exe))

    def prune_backups(self):
        root = self.dir / "backups"
        if root.is_dir():
            olds = sorted([p for p in root.iterdir() if (p / "meta.json").exists()], key=lambda p: p.stat().st_mtime)
            for p in olds[:-KEEP_BACKUPS]:
                shutil.rmtree(p, ignore_errors=True)

    def info(self):
        try:
            return json.loads((self.dir / "install-info.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}

    def save_info(self, **kw):
        info = self.info()
        info.update(kw)
        updatefile.safe_write(self.dir / "install-info.json", json.dumps(info, indent=1), encoding="ascii")

    def requirements(self):
        p = self.dir / "requirements-windows.txt"
        return p.read_bytes().decode("utf-8") if p.exists() else ""

    def pip(self, wheels):
        self.say("Installing the packages that came with the update (no internet needed)")
        o = self.owner()
        if o:
            self.fix_owner()
        r = subprocess.run([self.python, "-m", "pip", "install", "--no-index", "--find-links", str(wheels), "--no-warn-script-location",
                            "--force-reinstall", "--no-deps", "-r", str(self.dir / "requirements-windows.txt")],
                           cwd=self.dir, capture_output=True, text=True, timeout=1800, **({"user": o[0], "group": o[1]} if o else {}))
        if r.returncode != 0:
            raise Failed("Installing the packages failed: " + " | ".join((r.stdout + r.stderr).strip().splitlines()[-3:])[:400])

    def keys(self):
        try:
            return json.loads((HERE / "keys.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise Failed("The trusted update keys are missing from updates/run. Start the update again from Admin > Updates.")

    # -------------------------------------------------------------- the two jobs
    def apply(self, zip_path):
        zip_path = Path(zip_path)
        old_v, old_r = updatefile.read_version(self.dir)
        self.state(status="installing", job="apply", file=zip_path.name, started=datetime.now().astimezone().isoformat(timespec="seconds"),
                   fromVersion=old_v, fromRevision=old_r, message="Installing the update")
        self.say(f"Checking {zip_path.name}")
        try:
            m = updatefile.verify(zip_path, self.keys())
        except updatefile.UpdateError as e:
            raise Failed(str(e))
        new_v, new_r = m["version"], int(m["revision"])
        self.state(version=new_v, revision=new_r)
        if new_r <= old_r:
            raise Failed(f"This is rev {new_r}; rev {old_r} is installed. Updates only go forward (use Go back for an older version).")
        req_changed = m.get("requirements") and m["requirements"] != hashlib.sha256(self.requirements().encode()).hexdigest()
        if req_changed and not m["hasPackages"]:
            raise Failed("This update changes the Python packages but doesn't carry them. Use the update file built with packages, or Setup.exe.")
        self.say(f"Signature and files OK: v{new_v} rev {new_r} (installed: v{old_v} rev {old_r})")
        before = self.leaves()
        self.say("Stopping the suite")
        self.services.stop()
        b = None
        stage = "stopped"
        try:
            self.say("Backing up the current program files")
            b = self.backup(old_v, old_r)
            updatefile.safe_write(b / "migrations.json", json.dumps(before, indent=1))
            self.say("Putting the new program files in place")
            stage = "files"
            self.clear_program_files()
            updatefile.extract(zip_path, self.dir, "app")
            self.grant_desktop()
            if m["hasPackages"]:
                stage = "packages"
                staging = self.upd / "wheels-new"
                shutil.rmtree(staging, ignore_errors=True)
                updatefile.extract(zip_path, staging, "wheels")
                self.pip(staging)
                shutil.rmtree(self.dir / "wheels", ignore_errors=True)
                shutil.move(str(staging), str(self.dir / "wheels"))
            stage = "migrate"
            self.say("Updating the database tables")
            self.manage("migrate", "--noinput", "--database", "default")
            self.say("Updating the screens' files")
            self.manage("collectstatic", "--noinput", "--clear")
            wheel_names = [p.name for p in (self.dir / "wheels").glob("*.whl")]
            self.save_info(version=new_v, revision=new_r, packagesHash=updatefile.packages_hash(self.requirements(), wheel_names)
                           if m["hasPackages"] else self.info().get("packagesHash", ""), updated=datetime.now().isoformat(timespec="seconds"))
            self.say("Starting the suite")
            self.services.start()
            ok, msg = self.health(new_r)
            if not ok:
                raise Failed(msg)
            self.say(f"The suite is {msg}")
        except Exception as e:
            why = str(e) if isinstance(e, Failed) else f"{type(e).__name__}: {e}"
            self.say(f"Problem: {why}")
            if not isinstance(e, Failed):
                self.say(traceback.format_exc().strip().splitlines()[-1])
            if b is None:
                self.say("Nothing was changed; starting the suite again")
                self.services.start()
                raise Failed(why)
            self.back_out(b, before, old_r, stage, why, m["hasPackages"])
            raise Failed(why)
        try:
            zip_path.unlink()
        except OSError:
            pass
        self.ensure_shortcuts()
        self.refresh_root_copy(zip_path)
        self.fix_owner()
        self.prune_backups()
        self.state(status="done", message=f"Updated to v{new_v} rev {new_r}.", finished=datetime.now().astimezone().isoformat(timespec="seconds"))
        return True

    def back_out(self, b, before, old_r, stage, why, new_packages=False):
        self.say("Going back to the version that was installed")
        try:
            self.services.stop()
            if stage in ("migrate",):
                self.migrate_back(before)
            self.restore(b)
            meta = json.loads((b / "meta.json").read_text(encoding="utf-8"))
            if new_packages and stage in ("packages", "migrate") and (b / "wheels").is_dir():
                shutil.rmtree(self.dir / "wheels", ignore_errors=True)
                shutil.copytree(b / "wheels", self.dir / "wheels")
                self.pip(self.dir / "wheels")
            self.manage("collectstatic", "--noinput", "--clear", check=False)
            self.save_info(version=meta["version"], revision=meta["revision"], packagesHash=meta.get("packagesHash", ""))
            self.services.start()
            shutil.rmtree(b, ignore_errors=True)  # it is what's installed again, so it's no version to go back to
            ok, msg = self.health(old_r)
            self.say(f"The previous version is back and {msg}" if ok else f"The previous version is back in place, but {msg}")
            self.state(status="failed", message=f"The update didn't install ({why}). The previous version (rev {old_r}) was put back"
                       + ("." if ok else ", but it isn't answering yet: check logs/update.log, or run Setup.exe."),
                       finished=datetime.now().astimezone().isoformat(timespec="seconds"))
        except Exception as e2:
            self.say(f"Going back also failed: {e2}")
            self.state(status="failed", message=f"The update didn't install ({why}), and putting the previous version back failed too ({e2}). "
                       f"The backup is in {b}. Run Setup.exe to repair the install.", finished=datetime.now().astimezone().isoformat(timespec="seconds"))

    def goback(self):
        root = self.dir / "backups"
        backups = sorted([p for p in root.iterdir() if (p / "meta.json").exists()], key=lambda p: p.stat().st_mtime) if root.is_dir() else []
        if not backups:
            raise Failed("There is no earlier version to go back to.")
        b = backups[-1]
        meta = json.loads((b / "meta.json").read_text(encoding="utf-8"))
        cur_v, cur_r = updatefile.read_version(self.dir)
        self.state(status="installing", job="goback", file="", version=meta["version"], revision=meta["revision"], fromVersion=cur_v,
                   fromRevision=cur_r, started=datetime.now().astimezone().isoformat(timespec="seconds"),
                   message=f"Going back to v{meta['version']} rev {meta['revision']}")
        try:
            before = json.loads((b / "migrations.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            before = None
        self.say(f"Going back from v{cur_v} rev {cur_r} to v{meta['version']} rev {meta['revision']}")
        self.services.stop()
        try:
            if before is not None:
                self.migrate_back(before)  # with the current files, which know how to undo their own migrations
            self.say("Putting the previous program files back")
            self.restore(b)
            if (b / "wheels").is_dir() and meta.get("packagesHash") and meta.get("packagesHash") != self.info().get("packagesHash"):
                shutil.rmtree(self.dir / "wheels", ignore_errors=True)
                shutil.copytree(b / "wheels", self.dir / "wheels")
                self.pip(self.dir / "wheels")
            self.manage("collectstatic", "--noinput", "--clear")
            self.save_info(version=meta["version"], revision=meta["revision"], packagesHash=meta.get("packagesHash", ""))
            self.say("Starting the suite")
            self.services.start()
            ok, msg = self.health(meta["revision"])
            if not ok:
                raise Failed(msg)
        except Exception as e:
            why = str(e)
            self.say(f"Problem: {why}")
            self.services.start()
            self.state(status="failed", message=f"Going back didn't finish ({why}). Check logs/update.log, or run Setup.exe.",
                       finished=datetime.now().astimezone().isoformat(timespec="seconds"))
            raise Failed(why)
        shutil.rmtree(b, ignore_errors=True)  # used up: the next Go back goes one further
        self.state(status="done", message=f"Went back to v{meta['version']} rev {meta['revision']}.",
                   finished=datetime.now().astimezone().isoformat(timespec="seconds"))
        return True


# ------------------------------------------------------------------ stopping and starting the suite
class Services:
    def __init__(self, install):
        self.dir = Path(install)

    def _run(self, args):
        try:
            return subprocess.run(args, capture_output=True, text=True, timeout=120).returncode
        except (OSError, subprocess.TimeoutExpired):
            return -1

    def stop(self):
        if WIN:
            for t in (TASK_WEB, TASK_TICKER):
                self._run(["schtasks", "/End", "/TN", t])
            # Ending the tasks stops their cmd.exe. Also stop any of the suite's Python processes still running (by the
            # pid files serve.py writes) and SuiteAdmin.exe (tray icon / Admin windows, any user), whose files would be
            # locked. Not the control service. No Windows Management (WMI) queries: they can hang.
            for name in ("web.pid", "ticker.pid"):
                try:
                    self._run(["taskkill", "/F", "/PID", str(int((self.dir / "logs" / name).read_text().strip()))])
                except (OSError, ValueError):
                    pass
            self._run(["taskkill", "/F", "/IM", "SuiteAdmin.exe"])
            time.sleep(3)
        elif shutil.which("systemctl"):
            self._run(["systemctl", "stop", "production-suite-web", "production-suite-ticker"])

    def start(self):
        if WIN:
            for t in (TASK_WEB, TASK_TICKER):
                self._run(["schtasks", "/Run", "/TN", t])
            # the control service picks up its new program files
            self._run(["schtasks", "/End", "/TN", TASK_CONTROL])
            self._run(["schtasks", "/Run", "/TN", TASK_CONTROL])
        elif shutil.which("systemctl"):
            self._run(["systemctl", "start", "production-suite-web", "production-suite-ticker"])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", required=True)
    sub = ap.add_subparsers(dest="job", required=True)
    a = sub.add_parser("apply")
    a.add_argument("file")
    sub.add_parser("goback")
    sub.add_parser("request", help="do what the suite asked for in updates/run/request.json")
    args = ap.parse_args(argv)
    if args.job == "request":
        try:
            req = json.loads((Path(args.dir) / "updates" / "run" / "request.json").read_text(encoding="utf-8"))
            (Path(args.dir) / "updates" / "run" / "request.json").unlink()
        except (OSError, ValueError):
            print("Nothing to do: no update was requested.")
            return 0
        return main(["--dir", args.dir, *req["args"]])
    run = Run(args.dir)
    lock = run.upd / "run" / "lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        if time.time() - lock.stat().st_mtime < 3600:
            print("Another update is already running.")
            return 3
        lock.unlink()
        return main(argv)
    try:
        if args.job == "apply":
            run.apply(args.file)
        else:
            run.goback()
        return 0
    except Failed as e:
        st = json.loads((run.upd / "state.json").read_text(encoding="utf-8")) if (run.upd / "state.json").exists() else {}
        if st.get("status") == "installing":
            run.state(status="failed", message=str(e), finished=datetime.now().astimezone().isoformat(timespec="seconds"))
        return 1
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
