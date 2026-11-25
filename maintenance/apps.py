from django.apps import AppConfig


class MaintenanceConfig(AppConfig):
    name = "maintenance"

    def ready(self):
        from . import actions, cmms  # noqa: F401  (register the request, inventory and work-order commands with the engine)
