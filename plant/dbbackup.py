"""Backups of the suite's own database (Admin › Backup › Back up the database now).

MariaDB: a standard SQL dump (DROP/CREATE TABLE and INSERTs, like mariadb-dump), written by the suite itself from one
consistent snapshot, so it works on Windows and Linux without the MariaDB client tools and never puts the password on
a command line. Restore it with:   gunzip -c FILE.sql.gz | mariadb production_suite   (Windows: 7-Zip, then
mariadb production_suite < FILE.sql). SQLite: a copy of the database file made with SQLite's own backup.
Files go to backups/database/ in the suite's folder (kept by updates, never shipped); the newest KEEP are kept.
Only the suite's own records: the read-only asset, S2K and machine databases are never touched.
"""
import gzip
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.db import connections

KEEP = 20
NAME_RE = re.compile(r"^production_suite-\d{8}-\d{6}\.(sql|sqlite3)\.gz$")
ROWS_PER_INSERT = 500


class BackupError(Exception):
    pass


def folder():
    return Path(settings.BASE_DIR) / "backups" / "database"


def engine():
    return "mariadb" if connections["default"].vendor == "mysql" else "sqlite"


def listing():
    f = folder()
    out = []
    if f.is_dir():
        for p in sorted(f.iterdir(), reverse=True):
            if NAME_RE.match(p.name):
                st = p.stat()
                out.append({"name": p.name, "size": st.st_size, "at": datetime.fromtimestamp(st.st_mtime).astimezone()})
    return out


def path_of(name):
    """The file of a backup listed here (never anything else)."""
    if not NAME_RE.match(str(name or "")):
        return None
    p = folder() / name
    return p if p.is_file() else None


def _prune():
    for x in listing()[KEEP:]:
        (folder() / x["name"]).unlink(missing_ok=True)


def make():
    """Back up the suite's database now. Returns the listing entry of the new file."""
    f = folder()
    f.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    ext = "sql" if engine() == "mariadb" else "sqlite3"
    final = f / f"production_suite-{stamp}.{ext}.gz"
    tmp = final.with_name(final.name + ".part")
    try:
        if ext == "sql":
            _dump_mariadb(tmp)
        else:
            _copy_sqlite(tmp)
        os.replace(tmp, final)
    except BackupError:
        raise
    except Exception as e:  # noqa: BLE001 - the database's own error, in words an Admin can act on
        raise BackupError(f"The backup failed: {str(e)[:300]}")
    finally:
        tmp.unlink(missing_ok=True)
    _prune()
    return next(x for x in listing() if x["name"] == final.name)


def _copy_sqlite(dest):
    plain = dest.with_suffix(".tmp")
    try:
        db = connections["default"]
        db.ensure_connection()
        with sqlite3.connect(plain) as b:
            db.connection.backup(b)   # SQLite's online backup: a consistent copy while the suite runs
        with open(plain, "rb") as i, gzip.open(dest, "wb", 6) as o:
            while chunk := i.read(1 << 20):
                o.write(chunk)
    finally:
        plain.unlink(missing_ok=True)


def _q(name):
    return "`" + str(name).replace("`", "``") + "`"


def _dump_mariadb(dest):
    """Every table of the suite's database, structure and rows, from one consistent snapshot (InnoDB)."""
    import MySQLdb   # PyMySQL or mysqlclient, whichever the suite runs on (suite/settings.py)
    d = settings.DATABASES["default"]
    conn = MySQLdb.connect(host=d.get("HOST") or "127.0.0.1", port=int(d.get("PORT") or 3306), user=d.get("USER") or "",
                           password=d.get("PASSWORD") or "", database=d["NAME"], charset="utf8mb4")
    lit = getattr(conn, "escape", None) or (lambda v: conn.literal(v).decode("utf-8", "surrogateescape"))
    try:
        c = conn.cursor()
        c.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        c.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
        c.execute("SELECT VERSION()")
        version = c.fetchone()[0]
        c.execute("SHOW FULL TABLES WHERE Table_type = 'BASE TABLE'")
        tables = [r[0] for r in c.fetchall()]
        with gzip.open(dest, "wt", 6, encoding="utf-8", errors="surrogateescape", newline="\n") as out:
            out.write(f"-- Production Suite database backup: {d['NAME']}, {datetime.now().astimezone().isoformat(timespec='seconds')}\n"
                      f"-- Server {version}. Restore: gunzip -c THIS_FILE | mariadb {d['NAME']}\n\n"
                      "SET NAMES utf8mb4;\nSET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0;\n"
                      "SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO';\n\n")
            for t in tables:
                c.execute(f"SHOW CREATE TABLE {_q(t)}")
                create = c.fetchone()[1]
                out.write(f"--\n-- {t}\n--\nDROP TABLE IF EXISTS {_q(t)};\n{create};\n")
                c.execute(f"SELECT * FROM {_q(t)}")
                cols = ", ".join(_q(x[0]) for x in c.description)
                while rows := c.fetchmany(ROWS_PER_INSERT):
                    vals = ",\n".join("(" + ",".join(lit(v) for v in r) + ")" for r in rows)
                    out.write(f"INSERT INTO {_q(t)} ({cols}) VALUES\n{vals};\n")
                out.write("\n")
            out.write("SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS;\nSET SQL_MODE=@OLD_SQL_MODE;\n-- end of backup\n")
        c.execute("COMMIT")
    finally:
        conn.close()
