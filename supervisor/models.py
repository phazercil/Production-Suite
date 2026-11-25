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


class S2KPost(models.Model):
    """Supervisor › S2K posting: a finished MO run or a packed pallet that someone marked as entered into S2K. The suite
    never writes to S2K (it is read-only); this only records what was keyed in or imported there, and by whom."""
    kind = models.CharField(max_length=6)          # run | pallet | eb (an End Band / Glue Bead station job)
    ref = models.BigIntegerField(db_index=True)    # the MO run's, pallet tag's or station job's id
    seq = models.PositiveIntegerField(default=1, help_text="1 = the first posting; 2, 3… = corrections and reversals (qty is the difference)")
    mo = models.CharField(max_length=40, db_index=True)
    qty = models.IntegerField(default=0)
    t = models.DateTimeField()
    by = models.CharField(max_length=120)

    class Meta:
        db_table = "supervisor_s2k_post"
        unique_together = [("kind", "ref", "seq")]
