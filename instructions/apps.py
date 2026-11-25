from django.apps import AppConfig


class InstructionsConfig(AppConfig):
    name = "instructions"
    verbose_name = "Work instructions"

    def ready(self):
        from . import cmds  # noqa: F401  (registers the work-instruction commands with the engine)
