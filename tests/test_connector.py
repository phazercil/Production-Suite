"""Connector builder: the data needs, links read back from saved settings, and saving a card."""
import json
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase

from hmi import connector, engine, kadmin
from hmi.models import SavedQuery
from plant.demo import seed_demo
from plant.models import Machine, Person

SCHEMA = {"database": "dashboard", "tables": [
    {"name": "pleater_log", "columns": [{"name": n, "type": "int"} for n in ("id", "logged_at", "machine", "ppm", "cut_count", "run_bit")]},
    {"name": "open_orders", "columns": [{"name": n, "type": "varchar"} for n in ("mo_number", "item_number", "qty_open", "status")]},
    {"name": "tools", "columns": [{"name": n, "type": "varchar"} for n in ("part_no", "tool_type", "tool")]}]}


class ConnectorTests(TestCase):
    def setUp(self):
        cache.clear()
        kadmin._tables_cache.clear()
        seed_demo(reset=True)
        self.ctx = engine.Ctx(user=Person.objects.get(role="admin"))
        for p in (mock.patch.object(connector, "ready_dbs", return_value=["machines", "s2k", "assets"]),
                  mock.patch.object(engine.integrations, "schema", return_value=SCHEMA),
                  mock.patch.object(engine.integrations, "run_saved", return_value={"columns": ["mo", "item", "qty"], "rows": [], "ms": 1, "truncated": False}),
                  mock.patch("hmi.engine._which_db", side_effect=lambda p: p.get("db") or "assets")):
            p.start()
            self.addCleanup(p.stop)

    def save(self, **p):
        return engine.execute("connectorSave", p, self.ctx)

    def test_every_need_is_listed(self):
        keys = [g["key"] for g in connector.groups()]
        self.assertIn(f"m:{Machine.objects.get(name='Barbara').pk}", keys)
        self.assertIn("s2k", keys)
        for q in ("changeover_steps", "asset_settings", "part_spec", "part_tooling"):
            self.assertIn(f"q:{q}", keys)
        s = connector.summary(connector.groups())
        self.assertIn("Open MOs", [x["title"] for x in s["missing"]])

    def test_machine_card_saves_into_the_machine_feed(self):
        bar = Machine.objects.get(name="Barbara")
        g = f"m:{bar.pk}"
        r = self.save(group=g, db="machines", table="pleater_log", map={"ppm": "ppm"})
        self.assertFalse(r["ok"])
        self.assertIn("Newest row", r["error"])
        r = self.save(group=g, db="machines", table="pleater_log", map={"ppm": "nope", "orderCol": "logged_at"})
        self.assertIn('no column "nope"', r["error"])
        r = self.save(group=g, db="s2k", table="pleater_log", map={"ppm": "ppm", "orderCol": "logged_at"})
        self.assertFalse(r["ok"])                                   # machine readings never come from S2K
        r = self.save(group=g, db="machines", table="pleater_log", keyVal="Barbara",
                      map={"orderCol": "logged_at", "keyCol": "machine", "ppm": "ppm", "count": "cut_count", "run": "run_bit"})
        self.assertTrue(r["ok"], r)
        bar.refresh_from_db()
        self.assertEqual(bar.feed_type, "db")
        self.assertEqual({k: bar.feed_src[k] for k in ("db", "table", "orderCol", "keyCol", "keyVal")},
                         {"db": "machines", "table": "pleater_log", "orderCol": "logged_at", "keyCol": "machine", "keyVal": "Barbara"})
        self.assertEqual((bar.feed_ppm_field, bar.feed_count_field), ("ppm", "cut_count"))
        self.assertEqual(bar.uptime_src, {"type": "feed", "field": "run_bit", "runWhen": "high"})
        # read back as the card's current links
        g2 = next(x for x in connector.groups() if x["key"] == g)
        self.assertEqual(g2["status"], "done")
        self.assertEqual(g2["state"]["map"]["count"], "cut_count")

    def test_s2k_and_query_cards_save_the_builders_sql(self):
        r = self.save(group="s2k", db="s2k", table="open_orders", fval="OPEN",
                      map={"mo": "mo_number", "item": "item_number", "qty": "qty_open", "fcol": "status"})
        self.assertTrue(r["ok"], r)
        sq = SavedQuery.objects.get(name="s2k_mos")
        self.assertIn("`mo_number` AS mo", sq.sql)
        self.assertIn("WHERE `status` = 'OPEN'", sq.sql)
        self.assertEqual(engine.settings_obj().settings["s2kSource"], "db")
        g = next(x for x in connector.groups() if x["key"] == "s2k")
        self.assertEqual((g["table"], g["fval"], g["state"]["map"]["fcol"]), ("open_orders", "OPEN", "status"))
        r = self.save(group="q:part_tooling", db="assets", table="tools", map={"f_part": "part_no", "kind": "tool_type", "name": "tool"})
        self.assertTrue(r["ok"], r)
        g = next(x for x in connector.groups() if x["key"] == "q:part_tooling")
        self.assertEqual(g["state"]["map"], {"f_part": "part_no", "kind": "tool_type", "name": "tool"})

    def test_hand_written_sql_is_marked_custom(self):
        SavedQuery.objects.create(name="part_spec", sql="select a.x as rev from (select 1) a", saved_by="x")
        g = next(x for x in connector.groups() if x["key"] == "q:part_spec")
        self.assertEqual(g["status"], "custom")

    def test_tab_and_button(self):
        c = Client(HTTP_HX_REQUEST="true")
        c.post("/kiosk/do/login", {"pin": "9999"})
        with mock.patch.object(connector, "ready_dbs", return_value=["machines"]), \
                mock.patch.object(connector.integrations, "preview", return_value={"rows": [{"cut_count": 5}]}), \
                mock.patch.object(kadmin, "tables", return_value={"err": "", "ready": True, "tables": SCHEMA["tables"]}):
            r = c.get("/kiosk/v/admin?tab=connector&db=machines&table=pleater_log")
        self.assertContains(r, 'data-col="cut_count"')
        self.assertContains(r, "What the suite needs")
        r = c.get("/kiosk/v/admin?tab=connections")
        self.assertContains(r, "Open the connector builder")
        r = c.post("/kiosk/do/connectorSave", {"group": "s2k", "db": "s2k", "table": "open_orders",
                                                "map": json.dumps({"mo": "mo_number", "item": "item_number", "qty": "qty_open"})})
        self.assertContains(r, "Open MOs saved")
