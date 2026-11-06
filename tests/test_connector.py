"""Connector builder: the data needs, links read back from saved settings, and saving a card."""
import json
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase

from hmi import connector, engine, kadmin
from hmi.models import SavedQuery, SourceCard
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

    def card(self, db, table, title=None, filters=(), columns=(), id=None):
        r = engine.execute("sourceCardSave", {"id": id, "db": db, "table": table, "title": title or table,
                                              "filters": list(filters), "columns": list(columns)}, self.ctx)
        self.assertTrue(r["ok"], r)
        return (r.get("data") or r)["id"]

    def test_every_need_is_listed(self):
        keys = [g["key"] for g in connector.groups()]
        self.assertIn(f"m:{Machine.objects.get(name='Barbara').pk}", keys)
        self.assertIn("s2k", keys)
        for q in ("changeover_steps", "asset_settings", "part_spec", "part_tooling"):
            self.assertIn(f"q:{q}", keys)
        s = connector.summary(connector.groups())
        self.assertIn("Open MOs", [x["title"] for x in s["missing"]])

    def test_machine_box_reads_its_card(self):
        bar = Machine.objects.get(name="Barbara")
        g = f"m:{bar.pk}"
        c = self.card("machines", "pleater_log", "Barbara readings", [{"col": "machine", "val": "Barbara"}])
        r = self.save(group=g, map={"ppm": "ppm", "orderCol": "logged_at"})
        self.assertIn("Drag a column in first", r["error"])          # no card
        r = self.save(group=g, src=c, map={"ppm": "ppm"})
        self.assertIn("Newest row", r["error"])
        r = self.save(group=g, src=c, map={"ppm": "nope", "orderCol": "logged_at"})
        self.assertIn('no column "nope"', r["error"])
        s2k = self.card("s2k", "open_orders")
        r = self.save(group=g, src=s2k, map={"ppm": "mo_number", "orderCol": "qty_open"})
        self.assertFalse(r["ok"])                                    # machine readings never come from S2K
        r = self.save(group=g, src=c, map={"orderCol": "logged_at", "ppm": "ppm", "count": "cut_count", "run": "run_bit"})
        self.assertTrue(r["ok"], r)
        bar.refresh_from_db()
        self.assertEqual(bar.feed_type, "db")
        self.assertEqual({k: bar.feed_src[k] for k in ("db", "table", "orderCol", "keyCol", "keyVal", "source", "where")},
                         {"db": "machines", "table": "pleater_log", "orderCol": "logged_at", "keyCol": "machine", "keyVal": "Barbara",
                          "source": c, "where": [{"col": "machine", "val": "Barbara"}]})
        self.assertEqual((bar.feed_ppm_field, bar.feed_count_field), ("ppm", "cut_count"))
        self.assertEqual(bar.uptime_src, {"type": "feed", "field": "run_bit", "runWhen": "high"})
        gs = connector.groups()
        connector.link_cards(gs)
        g2 = next(x for x in gs if x["key"] == g)
        self.assertEqual((g2["status"], g2["src"], g2["state"]["map"]["count"]), ("done", c, "cut_count"))

    def test_changing_a_cards_filter_moves_its_boxes(self):
        bar = Machine.objects.get(name="Barbara")
        c = self.card("machines", "pleater_log", "Line 1", [{"col": "machine", "val": "B1"}])
        self.assertTrue(self.save(group=f"m:{bar.pk}", src=c, map={"orderCol": "id", "ppm": "ppm"})["ok"])
        r = engine.execute("sourceCardSave", {"id": c, "title": "Barbara", "filters": [{"col": "machine", "val": "Barbara"}]}, self.ctx)
        self.assertTrue(r["ok"], r)
        self.assertIn("Barbara now reads its new filter", r.get("msg") or r["data"]["msg"] if "data" in r else r["msg"])
        bar.refresh_from_db()
        self.assertEqual(bar.feed_src["where"], [{"col": "machine", "val": "Barbara"}])
        # wired columns stay on the card; a used card can't be removed
        r = engine.execute("sourceCardSave", {"id": c, "columns": ["id", "machine"]}, self.ctx)
        self.assertIn("Barbara uses ppm", r["error"])
        r = engine.execute("sourceCardDelete", {"id": c}, self.ctx)
        self.assertIn("Barbara reads this card", r["error"])
        r = engine.execute("sourceCardSave", {"id": c, "filters": [{"col": "machine", "val": ""}]}, self.ctx)
        self.assertIn("Type the value", r["error"])
        r = engine.execute("sourceCardSave", {"id": c, "title": " "}, self.ctx)
        self.assertIn("title", r["error"])

    def test_one_table_card_per_machine(self):
        bar, sal = Machine.objects.get(name="Barbara"), Machine.objects.get(name="Salvatore")
        cb = self.card("machines", "pleater_log", "Barbara", [{"col": "machine", "val": "Barbara"}])
        call = self.card("machines", "pleater_log", "All rows")
        self.assertTrue(self.save(group=f"m:{bar.pk}", src=cb, map={"orderCol": "id", "ppm": "ppm"})["ok"])
        r = self.save(group=f"m:{sal.pk}", src=cb, map={"orderCol": "id", "ppm": "ppm"})
        self.assertIn("Barbara already reads exactly these rows", r["error"])
        r = self.save(group=f"m:{sal.pk}", src=call, map={"orderCol": "id", "ppm": "ppm"})
        self.assertIn("Barbara also reads pleater_log", r["error"])
        cs = self.card("machines", "pleater_log", "Salvatore", [{"col": "machine", "val": "Salvatore"}])
        self.assertTrue(self.save(group=f"m:{sal.pk}", src=cs, map={"orderCol": "id", "ppm": "ppm"})["ok"])
        self.assertTrue(engine.execute("sourceCardDelete", {"id": call}, self.ctx)["ok"])

    def test_s2k_and_query_boxes_save_the_builders_sql(self):
        c = self.card("s2k", "open_orders", "Open orders", [{"col": "status", "val": "OPEN"}])
        r = self.save(group="s2k", src=c, map={"mo": "mo_number", "item": "item_number", "qty": "qty_open"})
        self.assertTrue(r["ok"], r)
        sq = SavedQuery.objects.get(name="s2k_mos")
        self.assertIn("`mo_number` AS mo", sq.sql)
        self.assertIn("WHERE `status` = 'OPEN'", sq.sql)
        self.assertEqual(sq.params["source"], c)
        self.assertEqual(engine.settings_obj().settings["s2kSource"], "db")
        gs = connector.groups()
        connector.link_cards(gs)
        g = next(x for x in gs if x["key"] == "s2k")
        self.assertEqual((g["table"], g["filters"], g["src"]), ("open_orders", [{"col": "status", "val": "OPEN"}], c))
        t = self.card("assets", "tools", "Tooling", [{"col": "tool_type", "val": "Die"}])
        r = self.save(group="q:part_tooling", src=t, map={"f_part": "part_no", "kind": "tool_type", "name": "tool"})
        self.assertTrue(r["ok"], r)
        self.assertIn("AND `tool_type` = 'Die'", SavedQuery.objects.get(name="part_tooling").sql)
        g = next(x for x in connector.groups() if x["key"] == "q:part_tooling")
        self.assertEqual(g["state"]["map"], {"f_part": "part_no", "kind": "tool_type", "name": "tool"})

    def test_links_made_before_cards_get_cards(self):
        bar = Machine.objects.get(name="Barbara")
        bar.feed_type, bar.feed_src = "db", {"db": "machines", "table": "pleater_log", "orderCol": "id", "keyCol": "machine", "keyVal": "Barbara"}
        bar.feed_ppm_field = "ppm"
        bar.save()
        SavedQuery.objects.create(name="s2k_mos", sql="SELECT `mo_number` AS mo, `item_number` AS item, `qty_open` AS qty\nFROM `open_orders`\nWHERE `status` = 'O''PEN'")
        gs = connector.groups()
        cards = connector.link_cards(gs)
        self.assertEqual(sorted((c.table, c.title) for c in cards), [("open_orders", "open_orders · O'PEN"), ("pleater_log", "pleater_log · Barbara")])
        self.assertEqual(len(connector.link_cards(connector.groups())), 2)   # once
        self.assertEqual(SourceCard.objects.get(table="pleater_log").filters, [{"col": "machine", "val": "Barbara"}])

    def test_hand_written_sql_is_marked_custom(self):
        SavedQuery.objects.create(name="part_spec", sql="select a.x as rev from (select 1) a", saved_by="x")
        g = next(x for x in connector.groups() if x["key"] == "q:part_spec")
        self.assertEqual(g["status"], "custom")

    def test_tab_and_cards(self):
        bar = Machine.objects.get(name="Barbara")
        c = self.card("machines", "pleater_log", "Barbara readings", [{"col": "machine", "val": "Barbara"}], ["id", "machine", "ppm"])
        self.assertTrue(self.save(group=f"m:{bar.pk}", src=c, map={"orderCol": "id", "ppm": "ppm"})["ok"])
        cl = Client(HTTP_HX_REQUEST="true")
        cl.post("/kiosk/do/login", {"pin": "9999"})
        with mock.patch.object(connector.integrations, "preview", return_value={"rows": [{"ppm": 180, "machine": "Barbara"}]}), \
                mock.patch.object(kadmin, "tables", return_value={"err": "", "ready": True, "tables": SCHEMA["tables"]}):
            r = cl.get("/kiosk/v/admin?tab=connector")
            self.assertContains(r, f'data-src="{c}" data-db="machines" data-table="pleater_log"')
            self.assertContains(r, "Barbara readings")
            self.assertContains(r, "machine = <b>Barbara</b>")
            self.assertContains(r, 'class="ctag port out" data-col="ppm"')
            self.assertNotContains(r, 'class="ctag port out" data-col="cut_count"')   # not picked for this card
            self.assertContains(r, "Add a source card")
            # a second card for the same table, opened to set it up
            r = cl.post("/kiosk/do/sourceCardAdd", {"db": "machines", "table": "pleater_log"})
            self.assertContains(r, "pleater_log (2)")
            new = SourceCard.objects.get(title="pleater_log (2)").pk
            self.assertContains(r, f'class="wbox srcard editing" data-src="{new}"')
            r = cl.post("/kiosk/do/sourceCardSave", {"id": new, "title": "Salvatore readings", "col": ["id", "ppm"],
                                                     "fcol": ["machine"], "fval": ["Salvatore"]})
            self.assertContains(r, "Card &quot;Salvatore readings&quot; saved")
            self.assertEqual(SourceCard.objects.get(pk=new).filters, [{"col": "machine", "val": "Salvatore"}])
            r = cl.post("/kiosk/do/sourceCardCopy", {"id": new})
            self.assertContains(r, "Salvatore readings (copy)")
            r = cl.post("/kiosk/do/connectorSave", {"group": "s2k", "src": self.card("s2k", "open_orders"),
                                                    "map": json.dumps({"mo": "mo_number", "item": "item_number", "qty": "qty_open"})})
        self.assertContains(r, "Open MOs saved")
        self.assertContains(r, 'data-group="s2k"')
        self.assertNotContains(r, f'data-group="m:{bar.pk}"')
        self.assertContains(r, 'id="csum" hx-swap-oob="true"')
