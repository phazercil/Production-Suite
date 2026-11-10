import logging

from django.apps import AppConfig

log = logging.getLogger("plant")


def _fit_text(sender, instance, **kwargs):
    """Safety net for MariaDB strict mode: text longer than its column would fail the whole save (and roll back
    an import). Anything that still gets here too long is cut to fit and logged. MO and part numbers are
    checked earlier and rejected instead, so they are never cut."""
    for f in sender._meta.concrete_fields:
        if f.get_internal_type() == "CharField" and f.max_length:
            v = getattr(instance, f.attname)
            if isinstance(v, str) and len(v) > f.max_length:
                log.warning("%s.%s cut to %s characters", sender.__name__, f.name, f.max_length)
                setattr(instance, f.attname, v[: f.max_length])


class PlantConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "plant"

    def ready(self):
        from django.apps import apps
        from django.db.models.signals import pre_save
        for label in ("plant", "hmi", "scheduler"):
            for model in apps.get_app_config(label).get_models():
                pre_save.connect(_fit_text, sender=model, dispatch_uid=f"fit-text-{model.__name__}")
        from . import schedule
        schedule.connect()
