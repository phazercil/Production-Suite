"""The database itself keeps one active log per machine and one open request per PM task.

Before the unique keys go on, any duplicates already there are settled:
- a machine with two or more active logs keeps the newest one running; each older one is ended where the next one
  started (its end note says so);
- a PM task with two or more open requests keeps the oldest; the others are completed with a note saying which one
  they duplicated.
"""
from django.db import migrations, models
from django.utils import timezone


def settle(apps, schema_editor):
    ShiftRun = apps.get_model("hmi", "ShiftRun")
    Ticket = apps.get_model("hmi", "Ticket")
    TicketNote = apps.get_model("hmi", "TicketNote")
    by_machine = {}
    for sh in ShiftRun.objects.filter(active=True, machine__isnull=False).order_by("machine_id", "start", "id"):
        by_machine.setdefault(sh.machine_id, []).append(sh)
    for runs in by_machine.values():
        for old, nxt in zip(runs, runs[1:]):
            old.active = False
            old.state = "idle"
            old.end = max(old.start, nxt.start)
            old.end_issues = ((old.end_issues or "") + "\nEnded by an update: another log was active on this machine.").strip()
            old.save(update_fields=["active", "state", "end", "end_issues"])
        last = runs[-1]
        last.active_machine = last.machine_id
        last.save(update_fields=["active_machine"])
    now = timezone.now()
    by_pm = {}
    for t in Ticket.objects.filter(pm__isnull=False).exclude(status="Closed").order_by("pm_id", "t", "id"):
        by_pm.setdefault(t.pm_id, []).append(t)
    for ts in by_pm.values():
        keep = ts[0]
        keep.open_pm = keep.pm_id
        keep.save(update_fields=["open_pm"])
        for t in ts[1:]:
            t.status, t.updated, t.updated_by = "Closed", now, "Suite update"
            t.save(update_fields=["status", "updated", "updated_by"])
            TicketNote.objects.create(ticket=t, t=now, by="Suite update", kind="closed",
                                      text=f"Completed as a duplicate of request #{keep.pk} for the same PM task.")


class Migration(migrations.Migration):
    dependencies = [("hmi", "0018_pm_rolltag_photos_qc_alerts")]

    operations = [
        migrations.AddField("shiftrun", "active_machine", models.PositiveIntegerField(null=True, blank=True, editable=False)),
        migrations.AddField("ticket", "open_pm", models.PositiveIntegerField(null=True, blank=True, editable=False)),
        migrations.RunPython(settle, migrations.RunPython.noop),
        migrations.AlterField("shiftrun", "active_machine", models.PositiveIntegerField(null=True, blank=True, unique=True, editable=False)),
        migrations.AlterField("ticket", "open_pm", models.PositiveIntegerField(null=True, blank=True, unique=True, editable=False)),
    ]
