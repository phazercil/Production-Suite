"""The Inkjet change kind, and Setup folded into Changeover (the plant calls it all changeovers)."""
from django.db import migrations, models


def setup_to_changeover(apps, schema_editor):
    apps.get_model("instructions", "Instruction").objects.filter(kind="setup").update(kind="changeover")


class Migration(migrations.Migration):

    dependencies = [
        ("instructions", "0001_work_instructions"),
    ]

    operations = [
        migrations.RunPython(setup_to_changeover, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="instruction",
            name="kind",
            field=models.CharField(
                choices=[
                    ("changeover", "Changeover"),
                    ("inkjet", "Inkjet change"),
                    ("operation", "Operation"),
                    ("quality", "Quality check"),
                    ("safety", "Safety"),
                    ("maintenance", "Maintenance"),
                ],
                default="changeover",
                max_length=12,
            ),
        ),
    ]
