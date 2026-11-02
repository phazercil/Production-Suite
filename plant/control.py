"""The suite's control service: lets Production Suite Admin and the tray icon work without Windows admin rights.

Runs as the system account ("Production Suite Control" scheduled task: python serve.py control), always on,
even while the suite itself is stopped. It listens on 127.0.0.1 only, so only programs on the server itself
can reach it, and it refuses requests a web browser could send (they must carry the X-Suite-Control header
and a JSON body). Standard library only: it never loads Django, so it keeps working if the database is down.

Open to anyone signed in to the server:   status, start, restart, the log viewer, reading the settings (no passwords)
Needs a shift lead PIN (checked by the suite): stop, and every check or change of a connection or setting

    POST /status   /start   /restart   /log {name}   /login {pin, who}   /logout
    POST /stop     /run {args, params, paramFlag}   /port-changed {port}        (PIN)
"""
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WIN = os.name == "nt"
TASKS = {"web": "Production Suite Web", "ticker": "Production Suite Ticker", "updater": "Production Suite Updater",
         "control": "Production Suite Control"}
DEFAULT_PORT = 8765
SESSION_IDLE = 20 * 60
LOGS = ("web.log", "ticker.log", "update.log", "admin.log", "control.log", "web-console.log", "ticker-console.log")
SUITEADMIN = {"info", "cmd", "records-save", "records-undo", "server-save", "pin-check"}
CHECKS = {"db", "dbadmin", "network", "server"}
log = logging.getLogger("plant.control")


def read_env(path):
    out = {}
    try:
        for raw in Path(path).read_text(encoding="utf-8-sig").splitlines():
            m = re.match(r'^\s*([A-Z0-9_]+)\s*=\s*(.*)$', raw)
            if m:
                v = m.group(2).strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                out[m.group(1)] = v
    except OSError:
        pass
    return out


class Suite:
    """Everything the control service does to the installed suite. Tests replace the Windows parts."""

    def __init__(self, folder, python=None):
        self.dir = Path(folder)
        self.python = python or self._python()
        self.lock = threading.Lock()

    def _python(self):
        p = self.dir / ".venv" / ("Scripts/python.exe" if WIN else "bin/python")
        return str(p) if p.exists() else sys.executable

    def env(self):
        return read_env(self.dir / ".env")

    def web_port(self):
        return int(self.env().get("WEB_PORT") or 8000)

    def control_port(self):
        return int(self.env().get("SUITE_CONTROL_PORT") or DEFAULT_PORT)

    # ------------------------------------------------------------------ the suite's processes
    def _cmd(self, args, timeout=120):
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
            return r.returncode, (r.stdout or "") + (r.stderr or "")
        except (OSError, subprocess.TimeoutExpired) as e:
            return -1, str(e)

    def task_state(self, name):
        if not WIN:
            return "n/a"
        code, out = self._cmd(["schtasks", "/Query", "/TN", name, "/FO", "CSV", "/NH"], 30)
        if code != 0:
            return "Missing"
        cols = [c.strip('"') for c in out.strip().splitlines()[-1].split('","')]
        return cols[-1].strip('"') if cols else "Unknown"

    def answering(self, wait=3):
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"http://127.0.0.1:{self.web_port()}/api/version", timeout=wait) as r:
                return json.loads(r.read().decode() or "{}")
        except Exception:
            return None

    def stop(self):
        if WIN:
            for t in (TASKS["web"], TASKS["ticker"]):
                self._cmd(["schtasks", "/End", "/TN", t], 60)
            # Ending the tasks stops their cmd.exe and, with it, the web app and ticker. Any of their Python processes
            # still running are stopped by process id: never this control service itself.
            mine = os.getpid()
            for pid in self.suite_pythons():
                if pid != mine:
                    self._cmd(["taskkill", "/F", "/PID", str(pid)], 30)
        elif shutil.which("systemctl"):
            self._cmd(["systemctl", "stop", "production-suite", "production-suite-ticker"])
        for _ in range(20):
            if not self.answering(1):
                break
            time.sleep(1)

    def suite_pythons(self):
        """Process ids of python.exe running the web app or the ticker, from the pid files serve.py writes."""
        out = []
        for name in ("web.pid", "ticker.pid"):
            try:
                out.append(int((self.dir / "logs" / name).read_text().strip()))
            except (OSError, ValueError):
                pass
        return out

    def start(self):
        if WIN:
            for t in (TASKS["web"], TASKS["ticker"]):
                code, out = self._cmd(["schtasks", "/Run", "/TN", t], 60)
                if code != 0:
                    return {"ok": False, "error": f"Couldn't start '{t}' ({out.strip()[:200]}). Run Setup.exe to repair the install."}
        elif shutil.which("systemctl"):
            self._cmd(["systemctl", "start", "production-suite", "production-suite-ticker"])
        for _ in range(90):
            v = self.answering(2)
            if v:
                return {"ok": True, "msg": f"Running: v{v.get('version')} rev {v.get('revision')} is answering.", "version": v}
            time.sleep(1)
        return {"ok": False, "error": "Started, but it isn't answering yet. Check web-console.log."}

    def status(self):
        v = self.answering()
        return {"ok": True, "answering": v, "port": self.web_port(), "folder": str(self.dir), "host": os.environ.get("COMPUTERNAME", ""),
                "tasks": {k: self.task_state(n) for k, n in TASKS.items() if k != "control"}}

    # ------------------------------------------------------------------ the suite's own commands
    def manage(self, args, params=None, flag="--params", timeout=900):
        """python manage.py <args> (whitelisted by the caller). Params go in a file in logs, which only
        administrators and the system account can read, and are removed afterwards."""
        f = None
        try:
            if params is not None:
                (self.dir / "logs").mkdir(exist_ok=True)
                f = self.dir / "logs" / f"admin-{uuid.uuid4().hex}.json"
                f.write_text(json.dumps(params), encoding="utf-8")
                args = list(args) + [flag, str(f)]
            env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", TICKER_IN_PROCESS="0", DJANGO_SETTINGS_MODULE="suite.settings")
            r = subprocess.run([self.python, "manage.py", *args], cwd=self.dir, env=env, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout)
            lines = ((r.stdout or "") + (r.stderr or "")).splitlines()
            return r.returncode, lines
        except subprocess.TimeoutExpired:
            return -1, [f"That took longer than {timeout} seconds and was stopped."]
        finally:
            if f is not None:
                try:
                    f.unlink()
                except OSError:
                    pass

    @staticmethod
    def result(code, lines):
        hit = [x for x in lines if x.startswith("@@RESULT ")]
        if hit:
            try:
                return json.loads(hit[-1][9:])
            except ValueError:
                pass
        tail = " | ".join(lines[-4:])
        return {"ok": False, "error": f"The suite didn't answer ({tail[:400]})", "msg": f"The suite didn't answer ({tail[:400]})"}

    def tail(self, name, n=200):
        if name not in LOGS:
            return {"ok": False, "error": "Unknown log."}
        p = self.dir / "logs" / name
        if not p.exists():
            return {"ok": True, "text": f"{name} hasn't been written yet."}
        with open(p, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 256 * 1024))
            data = fh.read().decode("utf-8", errors="replace")
        return {"ok": True, "text": "\r\n".join(data.splitlines()[-n:])}

    def port_changed(self, port):
        notes = []
        if WIN:
            code, out = self._cmd(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                                   f"Get-NetFirewallRule -DisplayName 'Production Suite' -ErrorAction Stop | Set-NetFirewallRule -LocalPort {int(port)} -ErrorAction Stop"], 60)
            notes.append(f"Firewall rule moved to port {port}." if code == 0 else "Couldn't move the firewall rule to the new port; allow it by hand.")
            desk = Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop"
            for f in desk.glob("Production Suite - *.url"):
                try:
                    f.write_text(re.sub(r"localhost:\d+", f"localhost:{int(port)}", f.read_text(encoding="ascii")), encoding="ascii")
                except OSError:
                    pass
        return {"ok": True, "msg": " ".join(notes)}


class Sessions:
    """Shift lead PIN sessions for this service, kept in memory (a restart logs everyone out)."""

    def __init__(self):
        self.s, self.fails = {}, []

    def new(self, name, who):
        tok = secrets.token_urlsafe(24)
        self.s[tok] = {"name": name, "who": who, "t": time.time()}
        return tok

    def get(self, tok):
        x = self.s.get(tok or "")
        if not x or time.time() - x["t"] > SESSION_IDLE:
            self.s.pop(tok or "", None)
            return None
        x["t"] = time.time()
        return x

    def limited(self):
        now = time.time()
        self.fails = [t for t in self.fails if now - t < 60]
        return len(self.fails) >= 5


def make_handler(suite, sessions):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ProductionSuiteControl"

        def log_message(self, fmt, *args):
            pass

        def reply(self, code, data):
            body = json.dumps(data, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.reply(405, {"ok": False, "error": "POST only."})

        def do_POST(self):
            # only programs on this server; never a web page (browsers can't send this header without asking first)
            if self.client_address[0] not in ("127.0.0.1", "::1") or self.headers.get("X-Suite-Control") != "1" \
                    or "application/json" not in (self.headers.get("Content-Type") or ""):
                return self.reply(403, {"ok": False, "error": "Not allowed."})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                p = json.loads(self.rfile.read(min(n, 1 << 20)).decode() or "{}") if n else {}
                if not isinstance(p, dict):
                    p = {}
            except ValueError:
                return self.reply(400, {"ok": False, "error": "Bad request."})
            try:
                self.reply(200, self.route(self.path.split("?")[0], p))
            except Exception as e:  # always answer the window
                log.exception("control request %s failed", self.path)
                self.reply(200, {"ok": False, "error": f"{type(e).__name__}: {e}"})

        def lead(self, p):
            return sessions.get(p.get("token"))

        def note(self, who, what):
            try:
                (suite.dir / "logs").mkdir(exist_ok=True)
                with open(suite.dir / "logs" / "admin.log", "a", encoding="utf-8") as f:
                    f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {who}: {what}\n")
            except OSError:
                pass

        def route(self, path, p):
            who = str(p.get("who") or "?")[:60]
            if path == "/status":
                return suite.status()
            if path == "/log":
                return suite.tail(str(p.get("name") or ""))
            if path == "/login":
                if sessions.limited():
                    return {"ok": False, "error": "Too many wrong PINs. Wait a minute and try again."}
                code, lines = suite.manage(["suiteadmin", "pin-check"], {"pin": str(p.get("pin") or "")})
                r = suite.result(code, lines)
                if not r.get("ok"):
                    sessions.fails.append(time.time())
                    return {"ok": False, "error": r.get("error") or "That is not a shift lead PIN."}
                self.note(f"{r['name']} on {who}", "logged in to Production Suite Admin")
                return {"ok": True, "token": sessions.new(r["name"], who), "name": r["name"]}
            if path == "/logout":
                sessions.s.pop(str(p.get("token") or ""), None)
                return {"ok": True}
            if path in ("/start", "/restart"):
                with suite.lock:
                    self.note(who, path[1:] + " the suite")
                    if path == "/restart":
                        suite.stop()
                    return suite.start()
            if path == "/run" and p.get("args") == ["suiteadmin", "info"]:
                with suite.lock:
                    return suite.result(*suite.manage(["suiteadmin", "info"]))
            s = self.lead(p)
            if not s:
                return {"ok": False, "pin": True, "error": "Enter a shift lead PIN first."}
            by = f"{s['name']} on {s['who']}"
            if path == "/stop":
                with suite.lock:
                    self.note(by, "stop the suite")
                    suite.stop()
                    return {"ok": True, "msg": "Stopped. It starts again by itself when the server restarts, or with Start."}
            if path == "/port-changed":
                return suite.port_changed(int(p.get("port") or 0))
            if path == "/run":
                args = [str(a) for a in (p.get("args") or [])]
                ok = (len(args) >= 2 and args[0] == "suiteadmin" and args[1] in SUITEADMIN) or \
                     (len(args) == 3 and args[:2] == ["install_plant", "--check"] and args[2] in CHECKS) or \
                     args in (["migrate", "--noinput", "--database", "default"], ["collectstatic", "--noinput"])
                if not ok:
                    return {"ok": False, "error": "Not available from Production Suite Admin."}
                params = p.get("params")
                if args[0] == "suiteadmin" and isinstance(params, dict):
                    params = dict(params, _as=by)
                with suite.lock:
                    self.note(by, " ".join(args[:3]))
                    code, lines = suite.manage(args, params, str(p.get("paramFlag") or "--params"))
                if args[0] in ("suiteadmin", "install_plant"):
                    return suite.result(code, lines)
                return {"ok": code == 0, "code": code, "lines": lines[-40:]}
            return {"ok": False, "error": "Unknown request."}

    return Handler


def serve(folder, port=None):
    suite = Suite(folder)
    port = port or suite.control_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(suite, Sessions()))
    httpd.daemon_threads = True
    # tell the Admin window and tray icon which port (they can't read the settings file)
    try:
        (suite.dir / "desktop").mkdir(exist_ok=True)
        (suite.dir / "desktop" / "control.port").write_text(str(port), encoding="ascii")
    except OSError:
        pass
    log.info("Production Suite control service on 127.0.0.1:%s", port)
    httpd.serve_forever()
