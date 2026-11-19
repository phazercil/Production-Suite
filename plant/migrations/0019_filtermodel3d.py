from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("plant", "0018_partspec_blank"),
    ]

    operations = [
        migrations.CreateModel(
            name="FilterModel3D",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("pid", models.CharField(help_text="The viewer's id for the part (its part number, cleaned)", max_length=120, unique=True)),
                ("part", models.CharField(db_index=True, max_length=120)),
                ("data", models.JSONField(default=dict)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("by", models.CharField(blank=True, max_length=80)),
            ],
            options={"ordering": ["part"]},
        ),
    ]
