from django.db import models


BUDGETS = {"A": 1, "B": 2}   # budget A is the plant's plan (the Scheduler uses it); B is a second budget to compare


class LaborPlan(models.Model):
    """A labor & production budget (the Labor Calculator's whole plan, as it saves it): row 1 is budget A, row 2
    budget B. `version` goes up on every save, so two screens can't silently overwrite each other."""
    data = models.JSONField(default=dict, blank=True)
    version = models.BigIntegerField(default=0)
    updated_by = models.CharField(max_length=120, blank=True)
    updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "labor_plan"

    @classmethod
    def load(cls, budget="A"):
        obj, _ = cls.objects.get_or_create(pk=BUDGETS.get(budget, 1))
        return obj


class LaborPlanVersion(models.Model):
    """Earlier saves, so a mistake can be undone (the last 200 of each budget are kept)."""
    budget = models.CharField(max_length=1, default="A", db_index=True)
    version = models.BigIntegerField()
    data = models.JSONField(default=dict)
    saved_by = models.CharField(max_length=120, blank=True)
    saved_at = models.DateTimeField()

    class Meta:
        db_table = "labor_plan_version"
        ordering = ["-version"]
