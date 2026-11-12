"""The kinds of database the suite can read from (asset, S2K and machine data databases): MariaDB / MySQL,
Microsoft SQL Server and PostgreSQL. Each one speaks its own network protocol and its own SQL dialect.

- detect() tells them apart from the server's first answer on the network: MariaDB/MySQL greets every new
  connection first; PostgreSQL answers an "SSL request" with one letter; SQL Server answers a TDS "pre-login".
  Nothing is logged in for this and no data is sent.
- connect() opens a read-only connection with the matching driver: PyMySQL (or mysqlclient) for MariaDB/MySQL,
  pymssql for SQL Server, pg8000 for PostgreSQL. Every driver uses %s placeholders, so the suite's queries
  (written with :name parameters) work the same on all three.
- quote(), table() and top() write identifiers and "first N rows" the way each one wants them.
The suite's own records stay in its MariaDB (or SQLite) database; this is only for the outside databases.
"""
import socket
import struct

KINDS = {
    "mysql": {"label": "MariaDB / MySQL", "port": 3306},
    "mssql": {"label": "Microsoft SQL Server", "port": 1433},
    "postgres": {"label": "PostgreSQL", "port": 5432},
}
DEFAULT_SCHEMA = {"mssql": "dbo", "postgres": "public"}


class NoDriver(Exception):
    pass


# ---------------------------------------------------------------- telling them apart
def _mysql(host, port, timeout):
    """MariaDB/MySQL sends a greeting (protocol 10) or an error packet (0xff) as soon as you connect."""
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(min(2.5, timeout))
        try:
            head = s.recv(5)
        except socket.timeout:
            return False
    return len(head) == 5 and head[4] in (0x0a, 0xff) and int.from_bytes(head[:3], "little") > 0


def _postgres(host, port, timeout):
    """PostgreSQL answers an SSLRequest with 'S' (TLS available) or 'N' (no TLS)."""
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.sendall(struct.pack("!ii", 8, 80877103))
        return s.recv(1) in (b"S", b"N")


def _mssql(host, port, timeout):
    """SQL Server answers a TDS pre-login packet with a tabular result (type 0x04)."""
    # one VERSION option (6 bytes) and a terminator, then the option data
    opts = bytes([0x00, 0x00, 0x06, 0x00, 0x06, 0xff]) + bytes([0x09, 0x00, 0x00, 0x00, 0x00, 0x00])
    pkt = bytes([0x12, 0x01]) + struct.pack(">H", 8 + len(opts)) + bytes([0, 0, 1, 0]) + opts
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.sendall(pkt)
        head = s.recv(8)
    return len(head) >= 1 and head[0] == 0x04


def detect(host, port, timeout=5):
    """'mysql', 'mssql' or 'postgres', or None when nothing recognisable answered."""
    port = int(port)
    order = ["mysql", "postgres", "mssql"]
    for k, v in KINDS.items():   # try what the port suggests first
        if v["port"] == port:
            order.remove(k)
            order.insert(0, k)
    probe = {"mysql": _mysql, "postgres": _postgres, "mssql": _mssql}
    failed = None
    for k in order:
        try:
            if probe[k](host, port, timeout):
                return k
            failed = False
        except ValueError:
            failed = False
        except OSError as e:
            if failed is None and k == order[0]:
                raise   # can't reach it at all: say so now (the caller explains), not after every probe
            continue
    return None


# ---------------------------------------------------------------- SQL dialect
def quote(kind, name):
    name = str(name)
    if kind == "mssql":
        return "[" + name.replace("]", "]]") + "]"
    if kind == "postgres":
        return '"' + name.replace('"', '""') + '"'
    return "`" + name.replace("`", "``") + "`"


def table(kind, name):
    """A table name as the schema browser lists it: plain for the default schema, "schema.table" otherwise."""
    if kind in ("mssql", "postgres") and "." in str(name):
        sch, tbl = str(name).split(".", 1)
        return f"{quote(kind, sch)}.{quote(kind, tbl)}"
    return quote(kind, name)


def top(kind, select_rest, n):
    """`SELECT <select_rest>` limited to n rows: SELECT TOP n … for SQL Server, … LIMIT n for the others."""
    n = int(n)
    if kind == "mssql":
        return f"SELECT TOP {n} {select_rest}"
    return f"SELECT {select_rest} LIMIT {n}"


def schema_sql(kind):
    """(sql, params-from-database-name) listing every table and column the login can read."""
    if kind == "mssql":
        return ("SELECT CASE WHEN TABLE_SCHEMA = 'dbo' THEN TABLE_NAME ELSE TABLE_SCHEMA + '.' + TABLE_NAME END, COLUMN_NAME, DATA_TYPE "
                "FROM INFORMATION_SCHEMA.COLUMNS ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION", lambda name: [])
    if kind == "postgres":
        return ("SELECT CASE WHEN table_schema = 'public' THEN table_name ELSE table_schema || '.' || table_name END, column_name, data_type "
                "FROM information_schema.columns WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
                "ORDER BY table_schema, table_name, ordinal_position", lambda name: [])
    return ("SELECT table_name, column_name, data_type FROM information_schema.columns WHERE table_schema=%s "
            "ORDER BY table_name, ordinal_position", lambda name: [name])


# ---------------------------------------------------------------- connections
def driver(kind):
    try:
        if kind == "mssql":
            import pymssql
            return pymssql
        if kind == "postgres":
            import pg8000.dbapi
            return pg8000.dbapi
        import MySQLdb  # mysqlclient, or PyMySQL standing in for it (see suite/settings.py)
        return MySQLdb
    except ImportError:
        raise NoDriver(f"The {KINDS.get(kind, {}).get('label', kind)} driver isn't installed on this server. "
                       "Run the installer again (it brings every driver with it).")


def errors(kind):
    """The driver's own error class, for messages."""
    return getattr(driver(kind), "Error", Exception)


def connect(kind, host, port, name, user, password, timeout=8, read_timeout=60):
    """A read-only DB-API connection. The login should only be allowed to read; on top of that the suite only
    ever sends single SELECT statements, and MariaDB/MySQL and PostgreSQL sessions are set to read-only."""
    db = driver(kind)
    if kind == "mssql":
        return db.connect(server=host, port=str(port), user=user, password=password or "", database=name,
                          login_timeout=timeout, timeout=read_timeout, appname="Production Suite", charset="UTF-8")
    if kind == "postgres":
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE   # encrypt when the server offers it (like psql's default)
        try:
            conn = db.connect(host=host, port=int(port), user=user, password=password or "", database=name, timeout=timeout,
                              ssl_context=ctx, application_name="Production Suite")
        except db.InterfaceError as e:
            if "SSL" not in str(e) and "ssl" not in str(e):
                raise
            conn = db.connect(host=host, port=int(port), user=user, password=password or "", database=name, timeout=timeout,
                              application_name="Production Suite")
        cur = conn.cursor()
        cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        cur.close()
        conn.commit()
        return conn
    return db.connect(host=host, port=int(port), user=user, password=password or "", database=name, connect_timeout=timeout,
                      read_timeout=read_timeout, charset="utf8mb4", init_command="SET SESSION TRANSACTION READ ONLY")


class Outside:
    """A SQL Server or PostgreSQL database, used the way the suite uses a Django connection: `with conn.cursor()
    as c:` opens a connection with the right driver, runs the query and closes it again."""

    def __init__(self, kind, settings_dict):
        self.kind, self.vendor, self.settings_dict = kind, kind, dict(settings_dict)

    def cursor(self):
        return _Cursor(self)

    def close(self):
        pass


class _Cursor:
    def __init__(self, outside):
        self.o = outside
        self.conn = self.cur = None

    def __enter__(self):
        s = self.o.settings_dict
        opts = s.get("OPTIONS") or {}
        self.conn = connect(self.o.kind, s["HOST"], s.get("PORT") or KINDS[self.o.kind]["port"], s.get("NAME", ""), s.get("USER", ""),
                            s.get("PASSWORD", ""), timeout=opts.get("connect_timeout", 8), read_timeout=opts.get("read_timeout", 60))
        self.cur = self.conn.cursor()
        return self.cur

    def __exit__(self, *exc):
        for x in (self.cur, self.conn):
            try:
                if x is self.conn:
                    try:
                        x.rollback()   # read-only: never commit anything
                    except Exception:  # noqa: BLE001
                        pass
                x.close()
            except Exception:  # noqa: BLE001 - closing a broken connection
                pass
        return False


def literal(kind, v):
    """A text value written into SQL (the suite's builders only; values typed by an Admin)."""
    v = str(v)
    if kind == "mysql":
        v = v.replace("\\", "\\\\")
    return "'" + v.replace("'", "''") + "'"
