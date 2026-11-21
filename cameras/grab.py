"""Getting a camera's picture. The server fetches it (the tablets never talk to the cameras, and the camera's
login stays on the server) and every screen watching the camera shares the same picture: at most `fps` fetches a
second per camera, however many screens watch it, and none when nobody does.

  snapshot  a JPEG picture address (most IP cameras have one: Hikvision /ISAPI/Streaming/channels/101/picture,
            Axis /axis-cgi/jpg/image.cgi, Dahua /cgi-bin/snapshot.cgi, Reolink /cgi-bin/api.cgi?cmd=Snap…)
  mjpeg     an MJPEG stream address: read continuously while a screen watches (up to 10 pictures a second)
  rtsp      an RTSP stream: read continuously by ffmpeg on the server while a screen watches (up to 10 a second;
            ffmpeg must be installed, able to decode the camera's H.264/H.265). Test takes one picture.
Only addresses on the plant network are used (plant/netsafety.py), checked when saved and again on every fetch."""
import io
import shutil
import subprocess
import threading
import time
import urllib.request
from urllib.parse import quote, urlsplit, urlunsplit

from plant import netsafety

MAX_BYTES = 8 * 1024 * 1024
TIMEOUT = 5
SOI, EOI = b"\xff\xd8", b"\xff\xd9"

_cache = {}    # camera id -> {"t": time of the picture, "img": jpeg bytes, "err": str, "tried": time of the last try}
_locks = {}
_guard = threading.Lock()


class CamError(Exception):
    pass


def check_url(kind, url):
    """The address, trimmed, if it is usable for this kind of camera and on the plant network; else CamError."""
    url = (url or "").strip()
    p = urlsplit(url)
    want = ("rtsp", "rtsps") if kind == "rtsp" else ("http", "https")
    if p.scheme not in want or not p.hostname:
        raise CamError("Enter the address like rtsp://192.168.100.60:554/stream1" if kind == "rtsp"
                       else "Enter the address like http://192.168.100.60/ISAPI/Streaming/channels/101/picture")
    if p.username or p.password:
        raise CamError("Leave the user name and password out of the address: type them in their own boxes "
                       "(they're kept in the server's .env, not in the database).")
    try:
        netsafety.check_host(p.hostname, "The camera address")
    except netsafety.NotPrivate as e:
        raise CamError(str(e))
    return url


OPENH264 = ("The server's ffmpeg can't decode this camera's H.264 video: Fedora's own ffmpeg (ffmpeg-free) uses OpenH264, "
            "which only reads the simplest H.264 and most cameras send Main or High. Install the full ffmpeg from RPM Fusion "
            "(sudo dnf install https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm, "
            "then sudo dnf swap ffmpeg-free ffmpeg --allowerasing), or set the camera's stream to H.264 Baseline, "
            "or use the camera's picture (JPEG) address.")


def ffmpeg():
    return shutil.which("ffmpeg")


def credentials(cam):
    from plant import liveconfig
    from plant.envfile import read_env
    if not cam.has_login:
        return "", ""
    v = read_env(liveconfig.env_path())
    return v.get(cam.env_user, ""), v.get(cam.env_password, "")


def _opener(url, user, pw):
    hs = [urllib.request.ProxyHandler({})]   # straight to the camera, never through a proxy
    if user or pw:
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, url, user, pw)
        hs += [urllib.request.HTTPDigestAuthHandler(mgr), urllib.request.HTTPBasicAuthHandler(mgr)]
    return urllib.request.build_opener(*hs)


def _http_error(e):
    code = getattr(e, "code", None)
    if code == 401:
        return CamError("The camera refused the user name or password (401).")
    if code:
        return CamError(f"The camera answered {code} {getattr(e, 'reason', '')}".strip() + ". Check the address.")
    return CamError(f"Couldn't reach the camera ({getattr(e, 'reason', e)}).")


def _snapshot(url, user, pw):
    try:
        with _opener(url, user, pw).open(url, timeout=TIMEOUT) as r:
            data = r.read(MAX_BYTES + 1)
    except Exception as e:  # noqa: BLE001 - urllib raises many kinds; each becomes a plain message
        raise _http_error(e)
    if len(data) > MAX_BYTES:
        raise CamError("The picture is over 8 MB.")
    if not data.startswith(SOI):
        raise CamError("That address didn't send a JPEG picture. For a video stream pick MJPEG or RTSP.")
    return data


def _mjpeg(url, user, pw):
    buf, t0 = b"", time.monotonic()
    try:
        with _opener(url, user, pw).open(url, timeout=TIMEOUT) as r:
            while time.monotonic() - t0 < TIMEOUT + 2 and len(buf) < MAX_BYTES:
                chunk = r.read(64 * 1024)
                if not chunk:
                    break
                buf += chunk
                a = buf.find(SOI)
                b = buf.find(EOI, a + 2) if a >= 0 else -1
                if b > 0:
                    return buf[a:b + 2]
    except Exception as e:  # noqa: BLE001
        raise _http_error(e)
    raise CamError("No picture came from that stream. Check it's an MJPEG address (for H.264 cameras use RTSP).")


def _rtsp(url, user, pw):
    exe = ffmpeg()
    if not exe:
        raise CamError("RTSP needs ffmpeg on the server (on Fedora: sudo dnf install ffmpeg). "
                       "Or use the camera's picture (JPEG) or MJPEG address.")
    p = urlsplit(url)
    if user or pw:
        host = p.hostname + (f":{p.port}" if p.port else "")
        url = urlunsplit((p.scheme, f"{quote(user, safe='')}:{quote(pw, safe='')}@{host}", p.path, p.query, ""))
    cmd = [exe, "-nostdin", "-loglevel", "error", "-rtsp_transport", "tcp", "-i", url,
           "-frames:v", "1", "-q:v", "5", "-f", "image2", "-vcodec", "mjpeg", "pipe:1"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=12)
    except subprocess.TimeoutExpired:
        raise CamError("The RTSP stream didn't send a picture within 12 seconds.")
    if r.returncode != 0 or not r.stdout.startswith(SOI):
        err = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        msg = err[-1][:200] if err else "no picture"
        if user or pw:
            msg = msg.replace(quote(pw, safe=""), "•••") if pw else msg
        if "401" in msg or "Unauthorized" in msg:
            raise CamError("The camera refused the user name or password.")
        if "openh264" in msg.lower() or "openh264" in (r.stderr or b"").decode("utf-8", "replace").lower():
            raise CamError(OPENH264)
        if "ecoder" in msg:
            raise CamError(f"ffmpeg on the server can't decode this stream ({msg}). Install the full ffmpeg (RPM Fusion) or use the picture address.")
        raise CamError(f"ffmpeg couldn't read the stream: {msg}")
    return r.stdout


def _rotate(img, deg):
    if deg not in (90, 180, 270):
        return img
    from PIL import Image
    with Image.open(io.BytesIO(img)) as im:
        out = io.BytesIO()
        im.rotate(-deg, expand=True).save(out, "JPEG", quality=82)
        return out.getvalue()


def fetch(cam):
    """A new picture from the camera now (jpeg bytes), or CamError."""
    url = check_url(cam.kind, cam.url)   # again: the name may point somewhere else now
    user, pw = credentials(cam)
    img = {"snapshot": _snapshot, "mjpeg": _mjpeg, "rtsp": _rtsp}.get(cam.kind, _snapshot)(url, user, pw)
    return _rotate(img, cam.rotate)


MAX_FPS = {"snapshot": 5.0, "mjpeg": 10.0, "rtsp": 10.0}
IDLE = 30          # seconds without a screen watching before a stream reader lets go of the camera


def every(cam):
    fps = max(0.2, min(MAX_FPS.get(cam.kind, 5.0), float(cam.fps or 1)))
    return 1.0 / fps


# ------------------------------------------------------------------ streams: one reader per camera, while watched
class Reader(threading.Thread):
    """Keeps one connection to an RTSP (through ffmpeg) or MJPEG camera while screens watch it, holding only the
    newest picture. Every screen shares it; it lets go of the camera IDLE seconds after the last screen asked."""

    def __init__(self, cam, sig):
        super().__init__(daemon=True, name=f"camera-{cam.pk}")
        self.cam, self.sig = cam, sig
        self.img, self.t, self.err = None, 0.0, ""
        self.seen = time.time()
        self.stop = threading.Event()
        self.proc = None

    def run(self):
        while not self.stop.is_set() and time.time() - self.seen < IDLE:
            try:
                self._rtsp() if self.cam.kind == "rtsp" else self._mjpeg()
                if not self.stop.is_set() and time.time() - self.seen < IDLE:
                    self.err = self.err or "The camera stopped sending pictures; connecting again."
            except CamError as e:
                self.err = str(e)
            except Exception as e:  # noqa: BLE001 - keep the reader alive whatever the camera does
                self.err = f"Camera stream error: {e}"
            if self.stop.is_set() or time.time() - self.seen >= IDLE:
                break             # nobody watching: let go now
            self.stop.wait(5)     # then connect again
        self.close()

    def close(self):
        self.stop.set()
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            p.kill()

    def _frames(self, read):
        """Split a stream of JPEGs into pictures, keeping the newest."""
        buf = b""
        while not self.stop.is_set() and time.time() - self.seen < IDLE:
            chunk = read(64 * 1024)
            if not chunk:
                return
            buf += chunk
            while True:
                a = buf.find(SOI)
                b = buf.find(EOI, a + 2) if a >= 0 else -1
                if b < 0:
                    buf = buf[a:] if a >= 0 else b""
                    if len(buf) > MAX_BYTES:
                        buf = b""
                    break
                self.img, self.t, self.err = buf[a:b + 2], time.time(), ""
                buf = buf[b + 2:]

    def _rtsp(self):
        exe = ffmpeg()
        if not exe:
            raise CamError("RTSP needs ffmpeg on the server (on Fedora: sudo dnf install ffmpeg). "
                           "Or use the camera's picture (JPEG) or MJPEG address.")
        url = check_url(self.cam.kind, self.cam.url)
        user, pw = credentials(self.cam)
        p = urlsplit(url)
        if user or pw:
            host = p.hostname + (f":{p.port}" if p.port else "")
            url = urlunsplit((p.scheme, f"{quote(user, safe='')}:{quote(pw, safe='')}@{host}", p.path, p.query, ""))
        vf = [f"fps={1 / every(self.cam):g}"] + {90: ["transpose=1"], 180: ["hflip", "vflip"], 270: ["transpose=2"]}.get(self.cam.rotate, [])
        cmd = [exe, "-nostdin", "-loglevel", "error", "-rtsp_transport", "tcp", "-i", url, "-an",
               "-vf", ",".join(vf), "-q:v", "5", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]
        import tempfile
        with tempfile.TemporaryFile() as errf:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, stdin=subprocess.DEVNULL)
            try:
                self._frames(self.proc.stdout.read1 if hasattr(self.proc.stdout, "read1") else self.proc.stdout.read)
            finally:
                if self.proc and self.proc.poll() is None:
                    self.proc.kill()
                if self.proc:
                    self.proc.wait(5)
                errf.seek(0)
                err = errf.read().decode("utf-8", "replace")
            if self.stop.is_set() or time.time() - self.seen >= IDLE:
                return
            if "openh264" in err.lower():
                raise CamError(OPENH264)
            lines = [x for x in err.strip().splitlines() if x.strip()]
            msg = lines[-1][:200] if lines else ""
            if pw:
                msg = msg.replace(quote(pw, safe=""), "•••")
            if "401" in msg or "Unauthorized" in msg:
                raise CamError("The camera refused the user name or password.")
            if msg:
                raise CamError(f"ffmpeg couldn't read the stream: {msg}")

    def _mjpeg(self):
        url = check_url(self.cam.kind, self.cam.url)
        user, pw = credentials(self.cam)
        try:
            r = _opener(url, user, pw).open(url, timeout=TIMEOUT)
        except Exception as e:  # noqa: BLE001
            raise _http_error(e)
        with r:
            self._frames(r.read1 if hasattr(r, "read1") else r.read)


_readers = {}


def _sig(cam):
    return (cam.kind, cam.url, cam.fps, cam.rotate, cam.has_login, str(cam.updated))


def _reader(cam):
    with _guard:
        r = _readers.get(cam.pk)
        if r and (r.sig != _sig(cam) or not r.is_alive()):
            r.close()
            r = None
        if r is None:
            r = _readers[cam.pk] = Reader(cam, _sig(cam))
            r.start()
        r.seen = time.time()
        return r


def frame(cam, now=None):
    """The camera's latest picture for a screen: {"img", "t", "err"}. Streams (RTSP, MJPEG) are read by one reader per
    camera while it is watched; picture addresses are fetched at most once per 1/fps seconds per camera. Either way
    every screen shares the same picture."""
    if cam.kind in ("rtsp", "mjpeg"):
        r = _reader(cam)
        if r.img is None and not r.err:
            for _ in range(60):          # the first picture: wait up to 6 s for it
                time.sleep(0.1)
                if r.img is not None or r.err:
                    break
        img = r.img
        if img is not None and cam.kind == "mjpeg" and cam.rotate:
            img = _rotate(img, cam.rotate)
        return {"img": img, "t": r.t, "err": r.err}
    now = time.time() if now is None else now
    with _guard:
        c = _cache.setdefault(cam.pk, {"t": 0, "img": None, "err": "", "tried": 0})
        lock = _locks.setdefault(cam.pk, threading.Lock())
    wait = every(cam) if not c["err"] else max(every(cam), 5.0)   # a camera that isn't answering: try every 5 s
    if now - c["tried"] >= wait and lock.acquire(blocking=False):
        try:
            c["tried"] = now
            try:
                c["img"], c["t"], c["err"] = fetch(cam), time.time(), ""
            except CamError as e:
                c["err"] = str(e)
        finally:
            lock.release()
    elif c["img"] is None and not c["err"]:
        with lock:   # the first picture: wait for the fetch under way
            pass
    return dict(c)


def forget(cam_id):
    with _guard:
        _cache.pop(cam_id, None)
        r = _readers.pop(cam_id, None)
    if r:
        r.close()
