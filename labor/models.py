from django.db import models


class LaborPlan(models.Model):
    """The labor & production budget (the Labor Calculator's whole plan, as it saves it). One row.
    `version` goes up on every save, so two screens can't silently overwrite each other."""
    data = models.JSONField(default=dict, blank=True)
    version = models.BigIntegerField(default=0)
    updated_by = models.CharField(max_length=120, blank=True)
    updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "labor_plan"

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class LaborPlanVersion(models.Model):
    """Earlier saves, so a mistake can be undone (the last 200 are kept)."""
    version = models.BigIntegerField()
    data = models.JSONField(default=dict)
    saved_by = models.CharField(max_length=120, blank=True)
    saved_at = models.DateTimeField()

    class Meta:
        db_table = "labor_plan_version"
        ordering = ["-version"]
