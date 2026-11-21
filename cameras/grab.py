"""Getting a camera's picture. The server fetches it (the tablets never talk to the cameras, and the camera's
login stays on the server) and every screen watching the camera shares the same picture: at most `fps` fetches a
second per camera, however many screens watch it, and none when nobody does.

  snapshot  a JPEG picture address (most IP cameras have one: Hikvision /ISAPI/Streaming/channels/101/picture,
            Axis /axis-cgi/jpg/image.cgi, Dahua /cgi-bin/snapshot.cgi, Reolink /cgi-bin/api.cgi?cmd=Snap…)
  mjpeg     an MJPEG stream address: one picture is read from it each time
  rtsp      an RTSP stream: one picture is taken with ffmpeg on the server (it must be installed, with H.264/H.265)
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


def every(cam):
    fps = max(0.2, min(5.0, float(cam.fps or 1)))
    if cam.kind == "rtsp":
        fps = min(fps, 1.0)   # each RTSP picture starts ffmpeg
    return 1.0 / fps


def frame(cam, now=None):
    """The camera's latest picture for a screen: {"img", "t", "err"}. Fetched at most once per 1/fps seconds per
    camera; screens asking while a fetch is under way get the last picture."""
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
