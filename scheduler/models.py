from django.db import models

from plant.models import MO, STAGES, Machine


class Job(models.Model):
    """One stage of one MO placed on a machine. The engine works out the actual start and end
    from the requested start, the machine's shifts, earlier jobs and (for later stages) Stage 1 output."""
    mo = models.ForeignKey(MO, on_delete=models.CASCADE, related_name="jobs")
    op = models.CharField(max_length=2, choices=STAGES)
    machine = models.ForeignKey(Machine, on_delete=models.CASCADE, related_name="jobs")
    req_start = models.DateTimeField()
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "sched_job"
        unique_together = [("mo", "op")]
        ordering = ["req_start", "id"]

    @property
    def key(self):
        return f"{self.mo_id}__{self.op}"
