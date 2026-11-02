import os
import sys

from django.apps import AppConfig
from django.conf import settings


class HmiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "hmi"
    verbose_name = "Operator Kiosk"

    def ready(self):
        # In `runserver`, start the ticker in the serving process (not the autoreloader parent).
        if settings.SUITE["TICKER_IN_PROCESS"] and "runserver" in sys.argv and os.environ.get("RUN_MAIN") == "true":
            from .ticker import start_background
            start_background()
