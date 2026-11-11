"""Running through lunch is set per shift (Machine.lunch_shifts) instead of once per line (run_lunch). A line that
ran through lunch keeps doing so on every shift it runs."""
from django.db import migrations, models


def carry_over(apps, schema_editor):
    Machine = apps.get_model("plant", "Machine")
    for m in Machine.objects.filter(run_lunch=True):
        m.lunch_shifts = sorted(m.shifts.values_list("id", flat=True))
        m.save(update_fields=["lunch_shifts"])


def back(apps, schema_editor):
    Machine = apps.get_model("plant", "Machine")
    for m in Machine.objects.all():
        if m.lunch_shifts:
            m.run_lunch = True
            m.save(update_fields=["run_lunch"])


class Migration(migrations.Migration):

    dependencies = [
        ("plant", "0014_extra_break_cancel"),
    ]

    operations = [
        migrations.AddField(
            model_name="machine",
            name="lunch_shifts",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Ids of the shifts on which this line keeps running through lunch (relief cover): the schedule doesn't pause it for breaks named lunch on those shifts",
            ),
        ),
        migrations.RunPython(carry_over, back),
        migrations.RemoveField(
            model_name="machine",
            name="run_lunch",
        ),
    ]
