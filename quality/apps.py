from django.apps import AppConfig


class QualityConfig(AppConfig):
    name = "quality"

    def ready(self):
        from . import cmds  # noqa: F401  (registers the tolerance command with the engine)
