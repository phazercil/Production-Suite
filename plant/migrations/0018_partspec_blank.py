from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("plant", "0017_mo_families"),
    ]

    operations = [
        migrations.AlterField(model_name="partspec", name="pleats", field=models.FloatField(blank=True, null=True)),
        migrations.AlterField(model_name="partspec", name="len", field=models.FloatField(blank=True, null=True)),
        migrations.AlterField(model_name="partspec", name="wid", field=models.FloatField(blank=True, null=True)),
        migrations.AlterField(model_name="partspec", name="ph", field=models.FloatField(blank=True, null=True)),
        migrations.AlterField(model_name="partspec", name="sb", field=models.FloatField(blank=True, null=True)),
        migrations.AlterField(model_name="partspec", name="tol", field=models.FloatField(blank=True, null=True)),
        migrations.AddField(model_name="partspec", name="hand",
                            field=models.JSONField(blank=True, default=list,
                                                   help_text="Values typed in Parts & specs (kept when the asset database doesn't have them)")),
    ]
