"""Database settings for the read-only outside databases (asset database, S2K database).

The suite never writes to these. Four things make sure of it:
- every connection is opened in read-only mode (SET SESSION TRANSACTION READ ONLY), so MariaDB/MySQL itself
  refuses changes to tables;
- only a single SELECT (or WITH ... SELECT) can run, and statements that lock rows or write files are refused;
- the database router never sends a model, a save or a migration to them;
- the installer only connects and reads the table list; it never creates a database or a table there.
"""

READ_ONLY_DBS = ("assets", "s2k", "machines")


def read_only_mysql(env, prefix):
    """DATABASES entry for ASSET_DB_*, S2K_DB_* or MACHINE_DB_* from .env, or None if not set."""
    if not env(f"{prefix}_HOST"):
        return None
    return {
        "ENGINE": "django.db.backends.mysql",
        "HOST": env(f"{prefix}_HOST"),
        "PORT": env(f"{prefix}_PORT", "3306"),
        "NAME": env(f"{prefix}_NAME", ""),
        "USER": env(f"{prefix}_USER", ""),
        "PASSWORD": env(f"{prefix}_PASSWORD", ""),
        "OPTIONS": {"charset": "utf8mb4", "connect_timeout": 5, "read_timeout": 15 if prefix == "MACHINE_DB" else 60,
                    "init_command": "SET SESSION TRANSACTION READ ONLY"},
        "CONN_MAX_AGE": 0,
        "TEST": {"MIRROR": "default"},  # tests never touch the real outside databases
    }


class SuiteRouter:
    """All of the suite's own tables live in the default database. Nothing is ever written or migrated elsewhere."""

    def db_for_read(self, model, **hints):
        return "default"

    def db_for_write(self, model, **hints):
        return "default"

    def allow_relation(self, a, b, **hints):
        return True

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        return db == "default"
