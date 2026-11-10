from django.apps import AppConfig


class SupervisorConfig(AppConfig):
    name = "supervisor"

    def ready(self):
        from . import edit  # noqa: F401  (registers the history correction commands with the engine)
