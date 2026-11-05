from django.db import models

from plant.models import MO, STAGES, Machine


class Job(models.Model):
    """One stage of one MO placed on a machine. The engine works out the actual start and end
    from the requested start, the machine's shifts, earlier jobs and (for later stages) Stage 1 output."""
    mo = models.ForeignKey(MO, on_delete=models.CASCADE, related_name="jobs")
    op = models.CharField(max_length=2, choices=STAGES)
    machine = models.ForeignKey(Machine, on_delete=models.CASCADE, related_name="jobs")
    req_start = models.DateTimeField()
    # A stage can be split in two (the Scheduler's split button): part 1 stops at a time of day ("time", split_at),
    # after a quantity ("qty", split_qty) or when the operator ends the MO at the kiosk ("operator", Stage 1 only);
    # part 2 makes the rest, wherever it is placed.
    part = models.PositiveSmallIntegerField(default=1)
    split_mode = models.CharField(max_length=8, blank=True)
    split_at = models.DateTimeField(null=True, blank=True)
    split_qty = models.FloatField(null=True, blank=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "sched_job"
        unique_together = [("mo", "op", "part")]
        ordering = ["req_start", "id"]

    @property
    def key(self):
        return job_key(self.mo_id, self.op, self.part)


def job_key(mo_id, op, part=1):
    return f"{mo_id}__{op}" if int(part or 1) == 1 else f"{mo_id}__{op}__{part}"
