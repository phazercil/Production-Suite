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

    def test_setup_needs_the_permission(self):
        c = Client()
        self.assertFalse(sup_login(c, "1111", "cameras.edit", "/cameras/")["ok"])      # an operator can't
        self.assertNotIn("Add a camera", c.get("/cameras/").content.decode())
        self.assertEqual(c.post("/cameras/do/save", {"name": "x"}).status_code, 200)    # the login page, nothing saved
        self.assertFalse(Camera.objects.exists())
