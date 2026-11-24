"""Live camera video for Operator Kiosk › Cameras, at the camera's own frame rate.

go2rtc (bin/go2rtc_linux_amd64, MIT, https://github.com/AlexxIT/go2rtc) passes each RTSP camera's video on to the
tablets' browsers untouched: the server doesn't decode anything, and the browser plays it (Media Source Extensions
over a websocket). The suite's ticker (keep(), every few seconds) runs it:

  - only on Linux x86-64, and only while at least one RTSP camera is set to live video;
  - listening on 127.0.0.1:8009 alone, with its RTSP and WebRTC servers off;
  - with streams cam<id> written from Admin › Cameras into updates/go2rtc/go2rtc.yaml (logins from .env, the file
    readable by the suite's account only).
Apache passes /video/ws?src=cam<id> to it from the plant network only (tools/linux_apply.py writes those rules), so
nothing else of go2rtc can be reached. Without the Apache rules (Windows, a test server) the tiles show pictures."""
import json
import logging
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from django.conf import settings

log = logging.getLogger("plant")
PORT = 8009
BIN = Path(settings.BASE_DIR) / "bin" / "go2rtc_linux_amd64"


def folder():
    return Path(settings.BASE_DIR) / "updates" / "go2rtc"


def supported():
    return sys.platform.startswith("linux") and platform.machine().lower() in ("x86_64", "amd64") and BIN.exists()


def source(cam, user="", pw=""):
    """The camera's address for go2rtc, with its login, video only."""
    p = urlsplit(cam.url)
    url = cam.url
    if user or pw:
        host = p.hostname + (f":{p.port}" if p.port else "")
        url = urlunsplit((p.scheme, f"{quote(user, safe='')}:{quote(pw, safe='')}@{host}", p.path, p.query, ""))
    return url + "#media=video"


def streams():
    """{"cam<id>": source} for every RTSP camera that is on and set to live video, on the plant network."""
    from . import grab
    from .models import Camera
    out = {}
    for c in Camera.objects.filter(enabled=True, kind="rtsp", live=True):
        try:
            grab.check_url(c.kind, c.url)
        except grab.CamError as e:
            log.warning("Live video: %s skipped (%s)", c.name, e)
            continue
        out[f"cam{c.pk}"] = source(c, *grab.credentials(c))
    return out


def config_text(st):
    q = json.dumps   # a JSON string is a YAML string
    lines = ["# Written by Production Suite (cameras/live.py) from Admin › Cameras. Changes here are replaced.",
             "api:", f"  listen: {q(f'127.0.0.1:{PORT}')}",   # no "origin": go2rtc then only takes a page from the same
             # host Apache forwards (ProxyPreserveHost), so another site open on a plant PC can't pull camera video
             "rtsp:", '  listen: ""', "webrtc:", '  listen: ""', "srtp:", '  listen: ""',
             "log:", "  level: warn", "streams:"]
    lines += [f"  {k}: {q(v)}" for k, v in sorted(st.items())] or []
    return "\n".join(lines) + "\n"


def _pdeathsig():
    """go2rtc goes when the ticker goes (Linux)."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").prctl(1, signal.SIGTERM)
    except Exception:  # noqa: BLE001
        pass


class Keeper:
    def __init__(self):
        self.proc = None
        self.sig = None
        self.asked = 0          # when Apache's live-video rules were last asked for
        self.started = 0

    def _status(self, **kw):
        d = folder()
        try:
            d.mkdir(parents=True, exist_ok=True)
            (d / "status.json").write_text(json.dumps({"t": int(time.time()), **kw}), encoding="utf-8")
        except OSError:
            pass

    def stop(self):
        p, self.proc, self.sig = self.proc, None, None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()

    def _stale(self):
        """A go2rtc left running by an earlier ticker (it holds the port): stop it."""
        pid_f = folder() / "go2rtc.pid"
        try:
            pid = int(pid_f.read_text())
            if Path(f"/proc/{pid}/cmdline").read_bytes().find(b"go2rtc") >= 0:
                os.kill(pid, signal.SIGTERM)
                time.sleep(1)
        except (OSError, ValueError):
            pass

    def keep(self):
        st = streams()
        if not st or not supported():
            if self.proc:
                self.stop()
            self._status(on=bool(st), running=False, streams=len(st),
                         err="" if not st else "Live video needs the suite on a Linux x86-64 server; the screens show pictures.")
            return
        sig = config_text(st)
        if self.proc and self.proc.poll() is None and self.sig == sig:
            self._status(on=True, running=True, streams=len(st), err="", pid=self.proc.pid)   # still running: say so
            self._apache()
            return
        if self.proc and self.proc.poll() is not None:
            err = self._log_tail() or f"go2rtc stopped (code {self.proc.returncode})"
            self.proc = None
            self._status(on=True, running=False, streams=len(st), err=err)
            if time.time() - self.started < 30:
                return          # it stopped right away: try again in half a minute, not every few seconds
        self.stop()
        if not self.started:
            self._stale()
        d = folder()
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        exe = d / "go2rtc"
        if not exe.exists() or exe.stat().st_size != BIN.stat().st_size or exe.stat().st_mtime < BIN.stat().st_mtime:
            tmp = d / "go2rtc.tmp"
            shutil.copyfile(BIN, tmp)
            os.chmod(tmp, 0o700)
            os.replace(tmp, exe)
        cfg = d / "go2rtc.yaml"
        fd = os.open(str(cfg) + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(sig)
        os.replace(str(cfg) + ".tmp", cfg)
        lf = d / "go2rtc.log"
        if lf.exists() and lf.stat().st_size > 5_000_000:
            lf.unlink()
        out = open(lf, "ab")
        try:
            self.proc = subprocess.Popen([str(exe), "-config", str(cfg)], cwd=str(d), stdout=out, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, preexec_fn=_pdeathsig, close_fds=True)
        finally:
            out.close()
        self.sig, self.started = sig, time.time()
        (d / "go2rtc.pid").write_text(str(self.proc.pid))
        log.info("Live video: go2rtc started for %d camera(s)", len(st))
        self._status(on=True, running=True, streams=len(st), err="", pid=self.proc.pid)
        self._apache()

    def _log_tail(self):
        try:
            lines = (folder() / "go2rtc.log").read_text(encoding="utf-8", errors="replace").strip().splitlines()
            return lines[-1][-300:] if lines else ""
        except OSError:
            return ""

    def _apache(self):
        """Linux install: ask the root helper for Apache's live-video rules until they're in place (it answers in
        updates/network-result.json). The first time, and then at most every 10 minutes."""
        if time.time() - self.asked < 600:
            return
        res = Path(settings.BASE_DIR) / "updates" / "network-result.json"
        try:
            if json.loads(res.read_text(encoding="utf-8")).get("video"):
                return
        except (OSError, ValueError):
            pass
        if not Path("/etc/systemd/system/production-suite-network.path").exists():
            return          # not the Linux install (Apache isn't the suite's): pictures only
        self.asked = time.time()
        from plant import netsafety
        try:
            netsafety.sync_and_record()
        except Exception:  # noqa: BLE001
            log.exception("Live video: couldn't ask for Apache's rules")


_keeper = Keeper()


def keep():
    _keeper.keep()


def status():
    """For Admin › Cameras: is live video running, and is Apache passing it on?"""
    try:
        s = json.loads((folder() / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        s = {}
    try:
        r = json.loads((Path(settings.BASE_DIR) / "updates" / "network-result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        r = {}
    s["apache"] = r.get("video")
    s["apacheMsg"] = r.get("videoMsg", "")
    s["supported"] = supported()
    s["fresh"] = bool(s.get("t") and time.time() - s["t"] < 60)
    s["ago"] = round((time.time() - s["t"]) / 60) if s.get("t") else None
    return s
