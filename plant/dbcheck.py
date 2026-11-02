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
}
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


def test_read_only(host, port, name, user, password, what, nets=None, records=None):
    """Sign in read-only and read the table list. Nothing is created or changed.
    Returns {"ok", "msg", "tables", "writable"}; records = (host, port, name) of the suite's own database."""
    from plant.netsafety import NotPrivate, check_host
    bad = check_fields(host, port, name, user)
    if bad:
        return {"ok": False, "msg": bad}
    try:
        check_host(host, what, nets)
    except NotPrivate as e:
        return {"ok": False, "msg": str(e)}
    if records and (str(host).lower(), str(port), str(name).lower()) == tuple(str(x).lower() for x in records):
        return {"ok": False, "msg": f"That is the suite's own records database. {what} must be a different database; "
                                    "the suite creates its tables only in its own database."}
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
        return {"ok": True, "msg": f"{what} reachable: {n} tables. Connected read-only; nothing was created or changed.",
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
