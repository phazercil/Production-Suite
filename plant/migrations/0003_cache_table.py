from django.core.management import call_command
from django.db import migrations


def create_cache_table(apps, schema_editor):
    call_command("createcachetable", database=schema_editor.connection.alias, verbosity=0)


class Migration(migrations.Migration):
    """Creates the shared cache table (used for the wrong-PIN limit) as part of `migrate`."""

    atomic = False  # table creation commits on its own on MariaDB
    dependencies = [("plant", "0002_plantsettings_ticker_owner_plantsettings_ticker_seen")]
    operations = [migrations.RunPython(create_cache_table, migrations.RunPython.noop)]
