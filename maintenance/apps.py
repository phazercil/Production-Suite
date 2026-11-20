from django.apps import AppConfig


class MaintenanceConfig(AppConfig):
    name = "maintenance"

    def ready(self):
        from . import actions  # noqa: F401  (registers the request status and inventory commands with the engine)
