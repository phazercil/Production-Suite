from django.db import models


class HistoryEdit(models.Model):
    """One field of a past record changed (or a record deleted or split) in Supervisor › History."""
    t = models.DateTimeField(db_index=True)
    by = models.CharField(max_length=120)
    kind = models.CharField(max_length=12, db_index=True)   # log | downtime | qc | co | morun
    obj_id = models.BigIntegerField(db_index=True)
    line = models.CharField(max_length=60, blank=True)
    field = models.CharField(max_length=60)
    old = models.TextField(blank=True)
    new = models.TextField(blank=True)
    why = models.TextField(blank=True)

    class Meta:
        db_table = "supervisor_history_edit"
        ordering = ["-t", "-id"]
