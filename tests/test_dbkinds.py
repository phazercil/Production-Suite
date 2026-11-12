"""Databases other than MariaDB: telling the kind apart, each dialect's SQL, and the driver connection."""
import socket
import struct
import threading
from unittest import mock

from django.test import SimpleTestCase, TestCase

from hmi import connector, kadmin
from plant import dbkinds


def fake_server(reply, greet=None):
    """A one-shot TCP server: sends `greet` on connect (like MariaDB), else answers the first packet with `reply`."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]

    def run():
        srv.settimeout(10)
        for _ in range(8):
            try:
                c, _ = srv.accept()
            except OSError:
                return
            with c:
                c.settimeout(3)
                if greet:
                    c.sendall(greet)
                    continue
                try:
                    data = c.recv(64)
                except OSError:
                    continue
                r = reply(data)
                if r:
                    c.sendall(r)
    threading.Thread(target=run, daemon=True).start()
    return port


class DetectTests(SimpleTestCase):
    def test_mariadb_greets_first(self):
        port = fake_server(None, greet=b"\x4a\x00\x00\x00\x0a" + b"10.11.6-MariaDB\x00")
        self.assertEqual(dbkinds.detect("127.0.0.1", port, timeout=3), "mysql")

    def test_mariadb_refusing_the_address_still_counts(self):
        port = fake_server(None, greet=b"\x40\x00\x00\x00\xff\x6a\x04Host 'x' is not allowed")
        self.assertEqual(dbkinds.detect("127.0.0.1", port, timeout=3), "mysql")

    def test_postgres_answers_the_ssl_request(self):
        port = fake_server(lambda d: b"N" if d[:8] == struct.pack("!ii", 8, 80877103) else None)
        self.assertEqual(dbkinds.detect("127.0.0.1", port, timeout=3), "postgres")

    def test_sql_server_answers_the_prelogin(self):
        port = fake_server(lambda d: b"\x04\x01\x00\x25\x00\x00\x01\x00" + b"\x00" * 29 if d[:1] == b"\x12" else None)
        self.assertEqual(dbkinds.detect("127.0.0.1", port, timeout=3), "mssql")


class DialectTests(SimpleTestCase):
    def test_builders_write_each_dialect_and_read_it_back(self):
        b = {"table": "erp.orders", "mo": "MO No", "item": "item", "qty": "q]ty", "fcol": "status", "fval": "O'PEN"}
        sqls = {k: kadmin.s2k_build_sql(b if k != "mysql" else {**b, "table": "orders"}, kind=k) for k in dbkinds.KINDS}
        self.assertIn("[MO No] AS mo", sqls["mssql"])
        self.assertIn("FROM [erp].[orders]", sqls["mssql"])
        self.assertIn('"MO No" AS mo', sqls["postgres"])
        self.assertIn('FROM "erp"."orders"', sqls["postgres"])
        self.assertIn("`MO No` AS mo", sqls["mysql"])
        for k, sql in sqls.items():
            p = connector.parse_sql(sql)
            self.assertEqual((p["map"]["qty"], p["fval"]), ("q]ty", "O'PEN"), k)
        sql = kadmin.build_codata_sql("part_tooling", {"table": "tools", "f_part": "part_no", "kind": "type", "name": "tool"}, kind="mssql")
        self.assertIn("WHERE [part_no] = :part", sql)

    def test_first_rows(self):
        self.assertEqual(dbkinds.top("mssql", "* FROM [t] ORDER BY [x] DESC", 1), "SELECT TOP 1 * FROM [t] ORDER BY [x] DESC")
        self.assertEqual(dbkinds.top("postgres", '* FROM "t"', 20), 'SELECT * FROM "t" LIMIT 20')


class OutsideConnectionTests(TestCase):
    def test_sql_server_settings_open_a_driver_connection_per_query(self):
        from django.db import connections
        from django.test import override_settings  # noqa: F401
        from hmi import integrations, machinedata
        from plant import liveconfig
        cfg = {"ENGINE": "django.db.backends.mysql", "HOST": "sql1", "PORT": "1433", "NAME": "Plant", "USER": "ro", "PASSWORD": "pw",
               "OPTIONS": {}, "SUITE_INTERNET": False, "SUITE_KIND": "mssql"}
        cur = mock.MagicMock()
        cur.description = [("Pleats Per Min",), ("stamp",)]
        cur.fetchone.return_value = (190, "2026-11-04 12:00")
        conn = mock.MagicMock()
        conn.cursor.return_value = cur
        with mock.patch.dict(connections.settings, {"machines": cfg}), mock.patch.object(liveconfig, "refresh"), \
                mock.patch("plant.netsafety.check_host"), mock.patch.object(dbkinds, "connect", return_value=conn) as connect, \
                mock.patch.object(machinedata, "columns", return_value=["Pleats Per Min", "stamp", "line"]):
            row, mark = machinedata.latest_row("machines", "dbo2.readings", ["Pleats Per Min"], "stamp", "line", "Barbara")
        self.assertEqual(row["Pleats Per Min"], 190)
        self.assertEqual(connect.call_args[0][:2], ("mssql", "sql1"))
        sql = cur.execute.call_args[0][0]
        self.assertEqual(sql, "SELECT TOP 1 [Pleats Per Min], [stamp] FROM [dbo2].[readings] WHERE [line] = %s ORDER BY [stamp] DESC")
        conn.close.assert_called()        # closed again after the query
        self.assertEqual(integrations.kind_of("machines"), "mysql")   # outside the patch: back to the default

    def test_connection_test_names_the_kind(self):
        from plant.dbcheck import test_read_only
        with mock.patch.object(dbkinds, "detect", return_value="mssql"), mock.patch("plant.dbcheck._test_other",
                                                                              return_value={"ok": True, "msg": "fine"}) as t:
            r = test_read_only("sql1.example", "1433", "Plant", "ro", "pw", "The S2K database", internet=True)
        self.assertEqual((r["ok"], r["kind"]), (True, "mssql"))
        self.assertEqual(t.call_args[0][0], "mssql")

    def test_standard_ports_are_not_probed(self):
        """Each probe is a connection closed without signing in, which MariaDB counts toward blocking the host."""
        from plant.dbcheck import test_read_only
        with mock.patch.object(dbkinds, "detect") as det, mock.patch("plant.dbcheck._test_mysql", return_value={"ok": True, "msg": "fine"}):
            r = test_read_only("db1.example", "3306", "pim", "ro", "pw", "The asset database", internet=True)
        det.assert_not_called()
        self.assertEqual(r["kind"], "mysql")
        with mock.patch.object(dbkinds, "detect", return_value="mysql") as det, mock.patch("plant.dbcheck._test_mysql", return_value={"ok": True, "msg": "fine"}):
            test_read_only("db1.example", "3399", "pim", "ro", "pw", "The asset database", internet=True)
        det.assert_called_once()                                  # an unusual port: looked at once

    def test_a_blocked_host_is_explained(self):
        from plant.dbcheck import explain
        for e in (Exception(1129, "Host '10.0.0.5' is blocked because of many connection errors"),
                  Exception("Packet sequence number wrong - got 1 expected 0")):   # what PyMySQL says for it (MDEV-21252)
            msg, code = explain(e, 3306)
            self.assertEqual(code, 1129)
            self.assertIn("flush-hosts", msg)

    def test_an_unreachable_server_is_not_called_the_wrong_kind(self):
        import socket
        with mock.patch.object(socket, "create_connection", side_effect=ConnectionRefusedError(111, "refused")):
            with self.assertRaises(OSError):
                dbkinds.detect("db1.example", 3399, timeout=1)


class OldReadOnlyServerTests(TestCase):
    """An outside read-only database on an older MariaDB (e.g. 10.3) is fine: only the suite's own database needs
    Django's minimum (10.5), since only that one is written and migrated."""
    def test_version_check_only_for_the_suite_s_own_database(self):
        from django.db import NotSupportedError
        from django.db.backends.mysql.base import DatabaseWrapper as Stock

        from plant.dbconf import read_only_mysql
        from plant.readonly_mysql.base import DatabaseWrapper as ReadOnly
        cfg = read_only_mysql(lambda k, d=None: {"ASSET_DB_HOST": "10.0.0.25", "ASSET_DB_NAME": "pim"}.get(k, d), "ASSET_DB")
        self.assertEqual(cfg["ENGINE"], "plant.readonly_mysql")
        settings = {"NAME": "pim", "USER": "", "PASSWORD": "", "HOST": "", "PORT": "", "OPTIONS": {}, "TIME_ZONE": None,
                    "CONN_MAX_AGE": 0, "CONN_HEALTH_CHECKS": False, "AUTOCOMMIT": True, "ATOMIC_REQUESTS": False, "TEST": {}}
        for cls, ok in ((ReadOnly, True), (Stock, False)):
            w = cls(dict(settings), alias="x")
            w.__dict__["mysql_is_mariadb"] = True
            w.__dict__["mysql_server_info"] = "10.3.22-MariaDB"
            w.get_database_version = lambda: (10, 3, 22)
            if ok:
                w.check_database_version_supported()
            else:
                with self.assertRaises(NotSupportedError):
                    w.check_database_version_supported()
