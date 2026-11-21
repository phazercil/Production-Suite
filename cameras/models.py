"""Cameras on the plant network and the machines whose Operator Kiosk shows them. A camera's user name and password
are kept in the server's .env (CAM_<id>_USER / CAM_<id>_PASSWORD), never in the database."""
from django.db import models


class Camera(models.Model):
    KINDS = [("snapshot", "Picture address (JPEG snapshot)"), ("mjpeg", "MJPEG stream address"), ("rtsp", "RTSP stream")]
    name = models.CharField(max_length=80)
    kind = models.CharField(max_length=10, choices=KINDS, default="snapshot")
    url = models.CharField(max_length=500)         # without a user name or password
    fps = models.FloatField(default=1)               # pictures a second on the screens
    rotate = models.IntegerField(default=0)          # 0, 90, 180, 270
    machines = models.ManyToManyField("plant.Machine", blank=True, related_name="cameras")
    sort = models.IntegerField(default=0)
    enabled = models.BooleanField(default=True)
    live = models.BooleanField(default=True)         # RTSP: live video on the screens (cameras/live.py), pictures if not
    has_login = models.BooleanField(default=False)   # a user name / password is in .env
    updated = models.DateTimeField(null=True, blank=True)
    updated_by = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["sort", "name", "id"]

    def __str__(self):
        return self.name

    @property
    def env_user(self):
        return f"CAM_{self.pk}_USER"

    @property
    def env_password(self):
        return f"CAM_{self.pk}_PASSWORD"
