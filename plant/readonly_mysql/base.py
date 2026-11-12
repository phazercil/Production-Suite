"""Django's MariaDB/MySQL backend for the read-only outside databases (asset, S2K, machine data).

Django refuses MariaDB older than 10.5 (MySQL older than 8.0.11) because it needs them to create and migrate its
own tables. The suite only ever reads these databases with plain SELECTs, so an older server is fine there; the
suite's own records database keeps the stock backend and its version check."""
from django.db.backends.mysql.base import DatabaseWrapper as MySQLWrapper


class DatabaseWrapper(MySQLWrapper):
    def check_database_version_supported(self):
        pass   # read-only SELECTs only: no minimum version
