from django.apps import AppConfig


class SupervisorConfig(AppConfig):
    name = "supervisor"

    def ready(self):
        from . import build, edit  # noqa: F401  (registers the history correction and builder commands with the engine)
