"""Checking a MariaDB login without changing anything. Used by setup and by Operator Kiosk > Admin > Connections."""
import re

ERRORS = {
    1045: "MariaDB refused that user name or password.",
    1044: "That user isn't allowed to use this database.",
    1049: "There is no database with that name on the server yet.",
    2002: "Couldn't reach a MariaDB server at that address.",
    2003: "Couldn't reach a MariaDB server at that address and port. Check the address, that MariaDB is running, "
          "and that the firewall on the database server allows port {port}.",
    2005: "That server name couldn't be found on the network. Check the spelling, or use its IP address.",
    2013: "The connection to MariaDB dropped. Check the address and port.",
    1129: "The database server turned this server away before signing in. Either it has blocked this server's address "
          "after too many failed connections (on the database server run: mariadb-admin flush-hosts, or the SQL FLUSH HOSTS), "
          "or the user isn't allowed to connect from this server's address (on the database server: "
          "GRANT SELECT ON <database>.* TO '<user>'@'<this server's address>'), then test again.",
    1040: "That database server has too many connections open right now. Try again in a minute.",
}
# MariaDB sends "host is blocked" (1129) with the wrong packet sequence number (MDEV-21252), so PyMySQL reports
# only this instead of the real message
BLOCKED_SEQ = "Packet sequence number wrong - got 1 expected 0"
HOST_RE = re.compile(r"^[A-Za-z0-9._:-]{1,253}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_$-]{1,64}$")


class NoDriver(Exception):
    pass


def driver():
    try:
        import MySQLdb  # mysqlclient, or PyMySQL standing in for it (see suite/settings.py)
        return MySQLdb
    except ImportError:
        raise NoDriver("The MariaDB driver (PyMySQL) isn't installed in this Python. Re-run setup.")


def explain(e, port):
    code = e.args[0] if getattr(e, "args", None) and isinstance(e.args[0], int) else None
    if code not in ERRORS and "Packet sequence number wrong" in str(e):
        code = 1129
    msg = ERRORS.get(code)
    detail = e.args[1] if getattr(e, "args", None) and len(e.args) > 1 else str(e)
    return (msg.format(port=port) if msg else "MariaDB answered with an error.") + f"  ({detail})", code


def check_fields(host, port, name, user):
    if not HOST_RE.match(str(host or "")):
        return "Use a server name or IP address."
    if not str(port or "").isdigit() or not 0 < int(port) < 65536:
        return "Use a port number like 3306."
    if not NAME_RE.match(str(name or "")):
        return "Type the database name (letters, numbers, _ $ -)."
    if not user or len(str(user)) > 80 or "\n" in str(user):
        return "Type the user name."
    return None


def precheck(host, port, name, user, what, nets=None, records=None, internet=False):
    """What makes a login unusable whether or not the server answers: badly typed fields, an address outside the
    plant network that wasn't allowed, or the suite's own records database. None if it may be saved."""
    from plant.netsafety import NotPrivate, check_host
    bad = check_fields(host, port, name, user)
    if bad:
        return bad
    if not internet:
        try:
            check_host(host, what, nets)
        except NotPrivate as e:
            return (f"{e} If this database really is outside the plant (a cloud or other site's server), "
                    "tick \"This database is outside the plant network\".")
    if records and (str(host).lower(), str(port), str(name).lower()) == tuple(str(x).lower() for x in records):
        return (f"That is the suite's own records database. {what} must be a different database; "
                "the suite creates its tables only in its own database.")
    return None


def test_read_only(host, port, name, user, password, what, nets=None, records=None, internet=False, kind="auto"):
    """Sign in read-only and read the table list. Nothing is created or changed.
    Returns {"ok", "msg", "tables", "writable"}; records = (host, port, name) of the suite's own database."""
    bad = precheck(host, port, name, user, what, nets, records, internet)
    if bad:
        return {"ok": False, "msg": bad}
    from plant import dbkinds
    if kind not in dbkinds.KINDS:   # "auto": a standard port says which kind of database it is
        kind = next((k for k, v in dbkinds.KINDS.items() if v["port"] == int(port)), kind)
    if kind not in dbkinds.KINDS:   # an unusual port: tell from the server's first answer which kind of database it is.
        # (Each look is a connection closed without signing in, which MariaDB counts toward blocking this server's
        # address, so it's only done when the port doesn't say.)
        try:
            kind = dbkinds.detect(host, port)
        except OSError:
            kind = "mysql"   # can't reach it: the MariaDB sign-in below explains why, as it always has
        if not kind:
            return {"ok": False, "msg": f"Something answered on {host}:{port}, but it isn't MariaDB/MySQL, SQL Server or PostgreSQL. "
                                        "Check the port (MariaDB/MySQL 3306, SQL Server 1433, PostgreSQL 5432)."}
    if kind != "mysql":
        r = _test_other(kind, host, port, name, user, password, what)
        return {**r, "kind": kind}
    r = _test_mysql(host, port, name, user, password, what)
    return {**r, "kind": "mysql"}


def _test_other(kind, host, port, name, user, password, what):
    """SQL Server or PostgreSQL: sign in, count the tables the login can read, and check it can't change data."""
    from plant import dbkinds
    label = dbkinds.KINDS[kind]["label"]
    try:
        db = dbkinds.driver(kind)
    except dbkinds.NoDriver as e:
        return {"ok": False, "msg": str(e)}
    try:
        conn = dbkinds.connect(kind, host, port, name, user, password)
    except Exception as e:  # noqa: BLE001 - each driver has its own errors; explain the common ones
        t = str(e)
        low = t.lower()
        if "18456" in t or "login failed" in low or "password authentication failed" in low or "28p01" in low:
            msg = f"{label} refused that user name or password."
        elif "4060" in t or ("database" in low and ("does not exist" in low or "cannot open database" in low or "3d000" in low)):
            msg = f"There is no database named \"{name}\" on that server (or this login can't open it). Nothing is ever created there."
        elif "timed out" in low or "timeout" in low or "unable to connect" in low or "20009" in t:
            msg = f"Couldn't reach {label} at {host}:{port}. Check the address, the port and the server's firewall."
        else:
            msg = f"{label} answered with an error."
        return {"ok": False, "msg": f"{msg}  ({t[:240]})"}
    try:
        cur = conn.cursor()
        sql, params = dbkinds.schema_sql(kind)
        cur.execute(sql, params(name))
        n = len({r[0] for r in cur.fetchall()})
        writable = False
        try:   # only reads the login's own permissions, to warn if it could change data
            if kind == "mssql":
                cur.execute("SELECT COUNT(*) FROM fn_my_permissions(NULL, 'DATABASE') "
                            "WHERE permission_name IN ('INSERT', 'UPDATE', 'DELETE', 'ALTER', 'CONTROL')")
            else:
                cur.execute("SELECT COUNT(*) FROM information_schema.table_privileges WHERE grantee = current_user "
                            "AND privilege_type IN ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE')")
            writable = bool(cur.fetchone()[0])
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "msg": f"{what} reachable ({label}): {n} tables. Connected read-only; nothing was created or changed.",
                "tables": n, "writable": writable}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "msg": f"{label} answered with an error.  ({str(e)[:240]})"}
    finally:
        try:
            conn.rollback()
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _test_mysql(host, port, name, user, password, what):
    try:
        db = driver()
    except NoDriver as e:
        return {"ok": False, "msg": str(e)}
    try:
        conn = db.connect(host=host, port=int(port), user=user, password=password or "", database=name,
                          connect_timeout=8, charset="utf8mb4", init_command="SET SESSION TRANSACTION READ ONLY")
    except db.Error as e:
        msg, code = explain(e, port)
        if code == 1049:
            msg = f"There is no database named \"{name}\" on that server. Check the name; nothing is ever created there."
        return {"ok": False, "msg": msg}
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE()")
        n = cur.fetchone()[0]
        writable = False
        try:  # only reads the account's permissions, to warn if it could change data
            cur.execute("SHOW GRANTS")
            grants = " ".join(str(r[0]) for r in (cur.fetchall() or [])).upper()
            writable = bool(re.search(r"\b(ALL PRIVILEGES|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE)\b", grants))
        except db.Error:
            pass
        return {"ok": True, "msg": f"{what} reachable (MariaDB/MySQL): {n} tables. Connected read-only; nothing was created or changed.",
                "tables": n, "writable": writable}
    except db.Error as e:
        return {"ok": False, "msg": explain(e, port)[0]}
    finally:
        conn.close()


USER_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,80}$")


def setup_with_admin(host, port, admin_user, admin_password, name, user, password):
    """With a MariaDB administrator login (used once, never saved): create the suite's database and its user if
    missing, and give that user full rights on the suite's own database only, so it can create and upgrade its tables.
    Nothing outside that one database is changed. Returns {"ok", "msg", "steps"}."""
    if not NAME_RE.match(str(name or "")):
        return {"ok": False, "msg": "Type the database name (letters, numbers, _ $ -)."}
    if not USER_RE.match(str(user or "")):
        return {"ok": False, "msg": "The suite's user name can use letters, numbers and . _ @ - only."}
    if not admin_user:
        return {"ok": False, "msg": "Type the MariaDB administrator user name (for example root)."}
    try:
        db = driver()
    except NoDriver as e:
        return {"ok": False, "msg": str(e)}
    try:
        conn = db.connect(host=host, port=int(port), user=admin_user, password=admin_password or "", connect_timeout=8, charset="utf8mb4")
    except db.Error as e:
        return {"ok": False, "msg": "The administrator login didn't work: " + explain(e, port)[0]}
    steps = []
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name = %s", [name])
        if not cur.fetchone()[0]:
            cur.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
            steps.append(f"created the database {name}")
        cur.execute("SELECT COUNT(*) FROM mysql.user WHERE user = %s", [user])
        if not cur.fetchone()[0]:
            cur.execute("CREATE USER %s@'%%' IDENTIFIED BY %s", [user, password or ""])
            steps.append(f"created the user {user}")
            hosts = ["%"]
        else:
            cur.execute("SELECT host FROM mysql.user WHERE user = %s", [user])
            hosts = [r[0] for r in cur.fetchall()] or ["%"]
        for h in hosts:
            cur.execute(f"GRANT ALL PRIVILEGES ON `{name}`.* TO %s@%s", [user, h])
        cur.execute("FLUSH PRIVILEGES")
        steps.append(f"gave {user} full rights on {name} (and only on {name})")
        return {"ok": True, "msg": "Done: " + "; ".join(steps) + ". The administrator login was not saved.", "steps": steps}
    except db.Error as e:
        return {"ok": False, "msg": "The administrator login couldn't set it up: " + explain(e, port)[0]}
    finally:
        conn.close()
