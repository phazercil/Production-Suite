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

TASK_WEB, TASK_TICKER = "Production Suite Web", "Production Suite Ticker"
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
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    def say(self, msg):
        line = f"{datetime.now():%H:%M:%S} {msg}"
        self.lines.append(line)
        try:
            with open(self.dir / "logs" / "update.log", "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d} {line}\n")
        except OSError:
            pass
        print(line, flush=True)
        self.state()

    # -------------------------------------------------------------- helpers
    def manage(self, *args, check=True, timeout=1800):
        env = dict(os.environ, PYTHONUTF8="1", DJANGO_SETTINGS_MODULE="suite.settings", TICKER_IN_PROCESS="0")
        r = subprocess.run([self.python, "manage.py", *args], cwd=self.dir, env=env, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
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
            shutil.copy2(self.dir / rel, dst)
        if (self.dir / "wheels").is_dir():
            shutil.copytree(self.dir / "wheels", b / "wheels")
        info = self.info()
        meta = {"version": version, "revision": revision, "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "packagesHash": info.get("packagesHash", ""), "requirements": self.requirements()}
        (b / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        return b

    def clear_program_files(self):
        for p in list(self.dir.iterdir()):
            if updatefile.preserved(p.name):
                continue
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()

    def restore(self, b):
        self.clear_program_files()
        shutil.copytree(b / "app", self.dir, dirs_exist_ok=True)

    def ensure_shortcuts(self):
        """Desktop shortcut to Production Suite Admin, for servers updated here rather than with Setup.exe."""
        exe = self.dir / "desktop" / "SuiteAdmin.exe"
        public = os.environ.get("PUBLIC")
        if not WIN or not exe.exists() or not public:
            return
        lnk = Path(public) / "Desktop" / "Production Suite Admin.lnk"
        if lnk.exists():
            return
        q = lambda s: str(s).replace("'", "''")  # noqa: E731
        ps = (f"$s = New-Object -ComObject WScript.Shell; $l = $s.CreateShortcut('{q(lnk)}'); $l.TargetPath = '{q(exe)}'; "
              f"$l.WorkingDirectory = '{q(self.dir)}'; $l.IconLocation = '{q(self.dir / 'desktop' / 'suite.ico')}'; "
              "$l.Description = 'Production Suite server settings: connections, start, stop and restart'; $l.Save()")
        try:
            subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, timeout=60)
            self.say("Added the Production Suite Admin shortcut to the desktop")
        except (OSError, subprocess.TimeoutExpired):
            pass

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
        (self.dir / "install-info.json").write_text(json.dumps(info, indent=1), encoding="ascii")

    def requirements(self):
        p = self.dir / "requirements-windows.txt"
        return p.read_bytes().decode("utf-8") if p.exists() else ""

    def pip(self, wheels):
        self.say("Installing the packages that came with the update (no internet needed)")
        r = subprocess.run([self.python, "-m", "pip", "install", "--no-index", "--find-links", str(wheels), "--no-warn-script-location",
                            "--force-reinstall", "--no-deps", "-r", str(self.dir / "requirements-windows.txt")],
                           cwd=self.dir, capture_output=True, text=True, timeout=1800)
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
            (b / "migrations.json").write_text(json.dumps(before, indent=1), encoding="utf-8")
            self.say("Putting the new program files in place")
            stage = "files"
            self.clear_program_files()
            updatefile.extract(zip_path, self.dir, "app")
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
            # the task stops cmd.exe; also stop the Python processes it started (their command line names the folder)
            folder = str(self.dir).rstrip("\\").replace("'", "''")
            ps = ("Get-CimInstance Win32_Process -Filter \"Name = 'python.exe' OR Name = 'cmd.exe'\" | Where-Object { $_.CommandLine -like '*"
                  + folder + "*' -and ($_.CommandLine -like '*serve.py*' -or $_.CommandLine -like '*run-web.cmd*' -or $_.CommandLine -like '*run-ticker.cmd*') }"
                  " | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
            self._run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps])
            time.sleep(3)
        elif shutil.which("systemctl"):
            self._run(["systemctl", "stop", "production-suite", "production-suite-ticker"])

    def start(self):
        if WIN:
            for t in (TASK_WEB, TASK_TICKER):
                self._run(["schtasks", "/Run", "/TN", t])
        elif shutil.which("systemctl"):
            self._run(["systemctl", "start", "production-suite", "production-suite-ticker"])


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
