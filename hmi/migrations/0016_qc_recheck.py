from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("hmi", "0015_qc_issue"),
    ]

    operations = [
        migrations.AddField(
            model_name="qccheck",
            name="recheck_due",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="qccheck",
            name="recheck",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="rechecked", to="hmi.qccheck"),
        ),
    ]
