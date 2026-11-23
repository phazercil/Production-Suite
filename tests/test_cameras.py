"""Cameras: set up on /cameras/ (plant network only, login kept in .env), shown on a machine's Operator Kiosk."""
import base64
import io
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase

from cameras import grab
from cameras.models import Camera
from plant.demo import seed_demo
from plant.models import Machine

from .test_quality import sup_login


def jpeg():
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (64, 36), (10, 120, 200)).save(b, "JPEG")
    return b.getvalue()


class Cam(BaseHTTPRequestHandler):
    hits = 0

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.headers.get("Authorization") != "Basic " + base64.b64encode(b"op:pw").decode():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="cam"')
            self.end_headers()
            return
        Cam.hits += 1
        d = jpeg()
        if self.path == "/mjpeg":
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=f")
            self.end_headers()
            self.wfile.write(b"--f\r\nContent-Type: image/jpeg\r\n\r\n" + d + b"\r\n--f\r\n")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg" if self.path == "/snap" else "text/html")
        self.end_headers()
        self.wfile.write(d if self.path == "/snap" else b"<html>login</html>")


class CameraTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Cam)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        super().tearDownClass()

    def setUp(self):
        cache.clear()
        seed_demo(reset=True)
        self.env = {}
        p1 = mock.patch("plant.liveconfig.save", side_effect=lambda ch: [self.env.pop(k, None) if v is None else self.env.__setitem__(k, v) for k, v in ch.items()])
        p2 = mock.patch("plant.envfile.read_env", side_effect=lambda path: dict(self.env))
        p1.start(), p2.start()
        self.addCleanup(p1.stop), self.addCleanup(p2.stop)
        self.c = Client()
        self.assertTrue(sup_login(self.c, "9999", "cameras.edit", "/cameras/")["ok"])
        self.bar = Machine.objects.get(name="Barbara")

    def save(self, **p):
        base = {"name": "Barbara infeed", "kind": "snapshot", "url": self.base + "/snap", "user": "op", "password": "pw", "fps": "2",
                "enabled": "1", "machines": [str(self.bar.pk)]}
        return self.c.post("/cameras/do/save", {**base, **p})

    def test_setup_keeps_the_login_in_env_and_shows_on_the_machines_kiosk(self):
        r = self.save()
        self.assertIn("msg=Barbara+infeed+saved", r["Location"])
        cam = Camera.objects.get()
        self.assertTrue(cam.has_login)
        self.assertEqual(self.env, {f"CAM_{cam.pk}_USER": "op", f"CAM_{cam.pk}_PASSWORD": "pw"})
        self.assertNotIn("pw", cam.url)
        r = self.c.get(f"/api/cameras/{cam.pk}/frame")
        self.assertEqual((r.status_code, r["Content-Type"]), (200, "image/jpeg"))
        self.assertTrue(r.content.startswith(b"\xff\xd8"))
        hits = Cam.hits
        self.c.get(f"/api/cameras/{cam.pk}/frame")
        self.assertEqual(Cam.hits, hits)                                    # shared: not fetched again within 1/fps
        self.assertContains(Client().get("/station/Barbara/"), "Cameras")
        self.assertIn(">Test</button>", self.c.get("/cameras/").content.decode())
        # changing it without typing the password keeps it
        self.save(user="", password="", name="Barbara infeed 2", id=str(cam.pk))
        self.assertEqual(self.env[f"CAM_{cam.pk}_PASSWORD"], "pw")
        self.save(id=str(cam.pk), clear_login="1")
        self.assertEqual(self.env, {})
        self.assertFalse(Camera.objects.get().has_login)

    def test_kiosk_page_lists_only_its_lines_cameras(self):
        self.save()
        self.save(name="Salvatore cam", machines=[str(Machine.objects.get(name="Salvatore").pk)])
        from hmi import kviews
        s = mock.Mock(station="Barbara")
        d = kviews.view_data(s, "cameras", None)
        self.assertEqual([c["name"] for c in d["cams"]], ["Barbara infeed"])
        self.assertEqual(d["cams"][0]["ms"], 500)

    def test_refuses_what_it_cant_use(self):
        for url, why in (("http://8.8.8.8/snap", "public internet"), ("http://op:pw@127.0.0.1/snap", "Leave the user name"),
                         ("ftp://127.0.0.1/x", "Enter the address")):
            r = self.save(url=url)
            self.assertIn("err=", r["Location"])
            self.assertIn(why.replace(" ", "+"), r["Location"])
        self.assertFalse(Camera.objects.exists())

    def test_errors_are_plain(self):
        self.save(password="nope")
        cam = Camera.objects.get()
        r = self.c.get(f"/api/cameras/{cam.pk}/frame")
        self.assertEqual(r.status_code, 503)
        self.assertIn("refused the user name or password", r.content.decode())
        self.save(id=str(cam.pk), url=self.base + "/page", password="pw")
        grab.forget(cam.pk)
        with self.assertRaisesMessage(grab.CamError, "didn't send a JPEG"):
            grab.fetch(Camera.objects.get())
        self.save(id=str(cam.pk), url=self.base + "/mjpeg", kind="mjpeg")
        self.assertTrue(grab.fetch(Camera.objects.get()).startswith(b"\xff\xd8"))
        with mock.patch("cameras.grab.ffmpeg", return_value=None):
            c = Camera.objects.get()
            c.kind, c.url = "rtsp", "rtsp://127.0.0.1:554/s"
            with self.assertRaisesMessage(grab.CamError, "needs ffmpeg"):
                grab.fetch(c)
        err = b"[vist#0:0/h264 @ 0x55] [dec:libopenh264 @ 0x55] Terminating thread with return code -1145393733"
        with mock.patch("cameras.grab.ffmpeg", return_value="/usr/bin/ffmpeg"), \
                mock.patch("subprocess.run", return_value=mock.Mock(returncode=1, stdout=b"", stderr=err)):
            with self.assertRaisesMessage(grab.CamError, "RPM Fusion"):
                grab.fetch(c)

    def test_setup_needs_the_permission(self):
        c = Client()
        self.assertFalse(sup_login(c, "1111", "cameras.edit", "/cameras/")["ok"])      # an operator can't
        self.assertNotIn("Add a camera", c.get("/cameras/").content.decode())
        self.assertEqual(c.post("/cameras/do/save", {"name": "x"}).status_code, 200)    # the login page, nothing saved
        self.assertFalse(Camera.objects.exists())

    # RTSP and MJPEG: one reader per camera while watched, the newest picture shared, let go when nobody watches
    def test_mjpeg_reader_keeps_the_newest_picture(self):
        self.save(kind="mjpeg", url=self.base + "/mjpeg", fps="10")
        cam = Camera.objects.get()
        self.assertEqual(grab.every(cam), 0.1)
        f = grab.frame(cam)
        self.assertTrue(f["img"].startswith(b"\xff\xd8"), f["err"])
        grab.forget(cam.pk)

    def test_rtsp_reader_reads_ffmpegs_pictures_and_lets_go(self):
        import os
        import stat
        import sys
        import tempfile
        d = tempfile.mkdtemp()
        fake = os.path.join(d, "ffmpeg")
        frame_hex = jpeg().hex()
        with open(fake, "w") as f:
            f.write(f"#!{sys.executable}\nimport sys, time\nargs = ' '.join(sys.argv)\n"
                    f"assert 'transpose=1' in args and 'fps=10' in args and 'image2pipe' in args, args\n"
                    f"j = bytes.fromhex('{frame_hex}')\nfor i in range(400):\n    sys.stdout.buffer.write(j); sys.stdout.buffer.flush(); time.sleep(0.05)\n")
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
        self.save(kind="rtsp", url="rtsp://127.0.0.1:554/live/0", fps="25", rotate="90", user="", password="")
        cam = Camera.objects.get()
        self.assertEqual(cam.fps, 10)                                  # RTSP goes up to 10 a second
        with mock.patch("cameras.grab.ffmpeg", return_value=fake), mock.patch.object(grab, "IDLE", 2):
            f = grab.frame(cam)
            self.assertTrue(f["img"] and f["img"].startswith(b"\xff\xd8"), f["err"])
            r = grab._readers[cam.pk]
            t1 = r.t
            import time
            time.sleep(0.3)
            self.assertGreater(grab.frame(cam)["t"], t1)               # new pictures keep coming
            self.assertIs(grab._readers[cam.pk], r)                    # the same reader for every screen
            time.sleep(3)                                              # nobody watching: it lets go
            self.assertFalse(r.is_alive())
            self.assertIsNotNone(r.proc is None or r.proc.poll())


class LiveVideoTests(TestCase):
    """Live video: go2rtc set up from Admin › Cameras (cameras/live.py), and Apache's rules for it (tools/linux_apply.py)."""
    def setUp(self):
        seed_demo(reset=True)
        import tempfile
        from pathlib import Path
        self.tmp = Path(tempfile.mkdtemp())
        self.env = {}
        p1 = mock.patch("plant.envfile.read_env", side_effect=lambda path: dict(self.env))
        p2 = mock.patch("cameras.live.folder", return_value=self.tmp / "go2rtc")
        p1.start(), p2.start()
        self.addCleanup(p1.stop), self.addCleanup(p2.stop)
        self.bar = Machine.objects.get(name="Barbara")

    def cam(self, **k):
        c = Camera.objects.create(**{"name": "Barbara live", "kind": "rtsp", "url": "rtsp://127.0.0.1:554/live/0", **k})
        c.machines.set([self.bar])
        return c

    def test_config_has_only_live_rtsp_cameras_with_their_login_and_nothing_else_listening(self):
        from cameras import live
        a = self.cam(has_login=True)
        self.env.update({f"CAM_{a.pk}_USER": "op", f"CAM_{a.pk}_PASSWORD": "p@ss word"})
        self.cam(name="pictures", live=False)
        self.cam(name="off", enabled=False)
        self.cam(name="snap", kind="snapshot", url="http://127.0.0.1/snap")
        st = live.streams()
        self.assertEqual(st, {f"cam{a.pk}": "rtsp://op:p%40ss%20word@127.0.0.1:554/live/0#media=video"})
        cfg = live.config_text(st)
        self.assertIn('listen: "127.0.0.1:8009"', cfg)
        for off in ('rtsp:\n  listen: ""', 'webrtc:\n  listen: ""', 'srtp:\n  listen: ""'):
            self.assertIn(off, cfg)

    def test_keeper_runs_go2rtc_while_a_camera_wants_it(self):
        import os
        import stat
        import sys
        from cameras import live
        fake = self.tmp / "fake-go2rtc"
        fake.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
        k = live.Keeper()
        with mock.patch.object(live, "BIN", fake), mock.patch.object(live, "supported", return_value=True), \
                mock.patch.object(k, "_apache"):
            k.keep()
            self.assertIsNone(k.proc)                                  # no live camera: nothing runs
            c = self.cam()
            k.keep()
            self.assertIsNotNone(k.proc)
            self.assertIsNone(k.proc.poll())
            cfg = self.tmp / "go2rtc" / "go2rtc.yaml"
            self.assertEqual(stat.S_IMODE(cfg.stat().st_mode), 0o600)  # it holds camera logins
            self.assertIn(f"cam{c.pk}:", cfg.read_text())
            self.assertTrue(live.status()["running"])
            p = k.proc
            import json as _json
            sf = self.tmp / "go2rtc" / "status.json"
            sf.write_text(_json.dumps({**_json.loads(sf.read_text()), "t": 1}))   # last said long ago
            self.assertFalse(live.status()["fresh"])
            k.keep()
            self.assertIs(k.proc, p)                                   # unchanged: left running
            self.assertTrue(live.status()["fresh"] and live.status()["running"])   # and still says so
            c.url = "rtsp://127.0.0.1:554/live/1"
            c.save()
            k.keep()
            self.assertIsNot(k.proc, p)                                # changed: started again with the new address
            self.assertIsNotNone(p.poll())
            Camera.objects.all().delete()
            q = k.proc
            k.keep()
            self.assertIsNone(k.proc)                                  # nobody wants it: stopped
            self.assertIsNotNone(q.poll())
            self.assertFalse(live.status()["running"])

    def test_kiosk_tile_plays_live_video_for_live_cameras(self):
        self.cam()
        self.cam(name="pictures", live=False)
        from hmi import kviews
        d = kviews.view_data(mock.Mock(station="Barbara"), "cameras", None)
        self.assertEqual([(c["name"], c["live"]) for c in d["cams"]], [("Barbara live", True), ("pictures", False)])
        from django.template.loader import render_to_string
        html = render_to_string("hmi/k/v_cameras.html", d)
        self.assertEqual(html.count("<video data-live="), 1)

    def test_apache_rules_only_pass_a_suite_camera_from_the_plant_network(self):
        import importlib.util
        from pathlib import Path
        spec = importlib.util.spec_from_file_location("linux_apply", Path(__file__).resolve().parent.parent / "tools" / "linux_apply.py")
        la = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(la)
        site = self.tmp / "production-suite.conf"
        site.write_text("<VirtualHost *:80>\n    ProxyPass / http://127.0.0.1:8008/\n    <Location />\n    </Location>\n</VirtualHost>\n")
        other = self.tmp / "production-suite-allow.conf"
        other.write_text("# not a site\n")
        changed = la.video_include(self.tmp)
        self.assertEqual(changed, [site])
        text = site.read_text()
        self.assertLess(text.index("</Location>"), text.index("IncludeOptional conf.d/production-suite-video.inc"))   # after <Location />
        self.assertEqual(la.video_include(self.tmp), [])                # only once
        rules = (self.tmp / "production-suite-video.inc").read_text()
        self.assertIn('ProxyPass "http://127.0.0.1:8009/api/ws" upgrade=websocket', rules)
        self.assertIn("Include conf.d/production-suite-allow.inc", rules)
        self.assertIn('Require expr "%{QUERY_STRING} =~ /^src=cam[0-9]+$/"', rules)
        la.video_undo(changed, self.tmp)
        self.assertNotIn("production-suite-video.inc", site.read_text())
        self.assertFalse((self.tmp / "production-suite-video.inc").exists())
