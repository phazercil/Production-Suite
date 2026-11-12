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


class Base(TestCase):
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

class ConnectorTests(Base):
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

    def test_test_data_reads_what_the_wires_would(self):
        bar = Machine.objects.get(name="Barbara")
        c = self.card("machines", "pleater_log", "Barbara readings", [{"col": "machine", "val": "Barbara"}])
        g = next(x for x in connector.groups() if x["key"] == f"m:{bar.pk}")
        seen = {}

        def fake(sql, params, limit=500, db="assets"):
            seen.update(sql=sql, db=db, limit=limit)
            return {"columns": ["orderCol", "ppm"], "rows": [{"orderCol": 42, "ppm": 188}], "ms": 3, "truncated": False}
        card = SourceCard.objects.get(pk=c)
        with mock.patch.object(connector.integrations, "run_saved", side_effect=fake), \
                mock.patch.object(connector.integrations, "kind_of", return_value="mysql"):
            t = connector.test_data(g, card, {"orderCol": "id", "ppm": "ppm"})
        self.assertIn("`id` AS `orderCol`", seen["sql"])
        self.assertIn("WHERE `machine` = 'Barbara'", seen["sql"])
        self.assertIn("ORDER BY `id` DESC LIMIT 5", seen["sql"])
        self.assertEqual(seen["db"], "machines")
        self.assertEqual([x["label"][:5] for x in t["cols"]], ["Newes", "Speed"])
        self.assertEqual(t["rows"], [[42, 188]])
        with self.assertRaises(connector.integrations.ConnError):
            connector.test_data(g, card, {"ppm": "nope"})
        # the button on the box: nothing is saved
        cl = Client(HTTP_HX_REQUEST="true")
        cl.post("/kiosk/do/login", {"pin": "9999"})
        with mock.patch.object(connector.integrations, "run_saved", side_effect=fake), \
                mock.patch.object(connector.integrations, "kind_of", return_value="mysql"):
            r = cl.post("/kiosk/do/connectorTest", {"group": f"m:{bar.pk}", "src": c, "map": json.dumps({"orderCol": "id", "ppm": "ppm"})})
        self.assertContains(r, "Test data")
        self.assertContains(r, "188")
        self.assertContains(r, "Barbara readings")
        bar.refresh_from_db()
        self.assertNotEqual(bar.feed_type, "db")


class TagTableTests(Base):
    """One table with one value per row and a tag: Salvatore_Speed and Salvatore_Count in the same value column."""

    def test_each_input_reads_its_own_tagged_rows(self):
        from hmi import machinedata
        sal, bar = Machine.objects.get(name="Salvatore"), Machine.objects.get(name="Barbara")
        c = self.card("machines", "pleater_log", "Tag log")   # columns: id, logged_at, machine (the tag), ppm (the value)
        where = {"ppm": [{"col": "machine", "val": "Salvatore_Speed"}], "count": [{"col": "machine", "val": "Salvatore_Count"}]}
        r = self.save(group=f"m:{sal.pk}", src=c, map={"orderCol": "id", "ppm": "ppm", "count": "ppm"}, where=where)
        self.assertTrue(r["ok"], r)
        sal.refresh_from_db()
        self.assertEqual(sal.feed_src["slotWhere"], where)
        gs = connector.groups()
        self.assertEqual(next(g for g in gs if g["key"] == f"m:{sal.pk}")["state"]["where"], where)
        # another machine on the same card is told apart by its own speed tag
        r = self.save(group=f"m:{bar.pk}", src=c, map={"orderCol": "id", "ppm": "ppm"}, where={"ppm": [{"col": "machine", "val": "Salvatore_Speed"}]})
        self.assertIn("exactly these rows", r["error"])
        r = self.save(group=f"m:{bar.pk}", src=c, map={"orderCol": "id", "ppm": "ppm"})
        self.assertIn("also reads pleater_log", r["error"])
        r = self.save(group=f"m:{bar.pk}", src=c, map={"orderCol": "id", "ppm": "ppm"}, where={"ppm": [{"col": "machine", "val": ""}]})
        self.assertIn("Type the value", r["error"])
        r = self.save(group=f"m:{bar.pk}", src=c, map={"orderCol": "id", "ppm": "ppm"}, where={"ppm": [{"col": "machine", "val": "Barbara_Speed"}]})
        self.assertTrue(r["ok"], r)
        # the minute reading: each input from the newest row of its own tag
        calls = []

        def fake_latest(db, table, want, order_col, key_col="", key_val="", where=None):
            calls.append((tuple(want), tuple((f["col"], f["val"]) for f in where or [])))
            tag = (where or [{}])[-1].get("val")
            return {w: {"Salvatore_Speed": 186, "Salvatore_Count": 51234}[tag] for w in want}, 7
        with mock.patch.object(machinedata, "latest_row", side_effect=fake_latest):
            out, st = machinedata.fetch_feed(sal, {}, 0)
        self.assertEqual((out["ppm"], out["count"]), (186.0, 51234))
        self.assertEqual(sorted(calls), [(("ppm",), (("machine", "Salvatore_Count"),)), (("ppm",), (("machine", "Salvatore_Speed"),))])

    def test_test_data_per_input_and_value_suggestions(self):
        sal = Machine.objects.get(name="Salvatore")
        c = self.card("machines", "pleater_log", "Tag log")
        from hmi import machinedata
        g = next(x for x in connector.groups() if x["key"] == f"m:{sal.pk}")
        with mock.patch.object(machinedata, "latest_row", side_effect=lambda db, t, want, o, k="", v="", where=None: (
                {w: 99 if where[-1]["val"].endswith("Speed") else 5000 for w in want}, 12)):
            t = connector.test_data(g, SourceCard.objects.get(pk=c), {"orderCol": "id", "ppm": "ppm", "count": "ppm"},
                                    slot_where={"ppm": [{"col": "machine", "val": "Salvatore_Speed"}],
                                                "count": [{"col": "machine", "val": "Salvatore_Count"}]})
        self.assertTrue(t["tags"])
        self.assertEqual([(x["label"][:5], x["value"]) for x in t["now"]], [("Speed", 99), ("Filte", 5000)])
        seen = {}

        def fake(sql, params, limit=500, db="assets"):
            seen["sql"] = sql
            return {"columns": ["v"], "rows": [{"v": "Salvatore_Count"}, {"v": "Salvatore_Speed"}, {"v": None}], "ms": 1, "truncated": False}
        cl = Client(HTTP_HX_REQUEST="true")
        cl.post("/kiosk/do/login", {"pin": "9999"})
        with mock.patch.object(connector.integrations, "run_saved", side_effect=fake), \
                mock.patch.object(connector.integrations, "kind_of", return_value="mysql"):
            r = cl.post("/kiosk/do/connectorValues", {"src": c, "col": "machine"})
        self.assertContains(r, '<option value="Salvatore_Speed">')
        self.assertIn("SELECT DISTINCT `machine`", seen["sql"])


class DisconnectTests(Base):
    def test_taking_every_wire_off_disconnects(self):
        bar = Machine.objects.get(name="Barbara")
        c = self.card("machines", "pleater_log", "Barbara", [{"col": "machine", "val": "Barbara"}])
        self.assertTrue(self.save(group=f"m:{bar.pk}", src=c, map={"orderCol": "id", "ppm": "ppm", "run": "run_bit"})["ok"])
        r = self.save(group=f"m:{bar.pk}", src=c, map={"orderCol": "id"})
        self.assertIn("still needed", r["error"])                 # a required input can't just go
        r = self.save(group=f"m:{bar.pk}", src="", map={})
        self.assertTrue(r["ok"], r)
        self.assertIn("disconnected", r.get("msg") or r["data"]["msg"] if "data" in r else r["msg"])
        bar.refresh_from_db()
        self.assertEqual((bar.feed_type, bar.feed_src, bar.uptime_src), ("none", {}, {}))
        g = next(x for x in connector.groups() if x["key"] == f"m:{bar.pk}")
        self.assertEqual(g["status"], "missing")
        # a machine fed some other way is left alone
        Machine.objects.filter(pk=bar.pk).update(feed_type="http", feed_url="http://10.0.0.5/feed")
        r = self.save(group=f"m:{bar.pk}", map={})
        self.assertEqual(Machine.objects.get(pk=bar.pk).feed_type, "http")
        # S2K's open MOs
        s2k = self.card("s2k", "open_orders")
        self.assertTrue(self.save(group="s2k", src=s2k, map={"mo": "mo_number", "item": "item_number", "qty": "qty_open"})["ok"])
        self.assertTrue(self.save(group="s2k", map={})["ok"])
        self.assertFalse(SavedQuery.objects.filter(name="s2k_mos").exists())
        self.assertNotEqual(engine.settings_obj().settings.get("s2kSource"), "db")


class SecondRunSignalTests(Base):
    def test_named_second_run_signal(self):
        bar = Machine.objects.get(name="Barbara")
        c = self.card("machines", "pleater_log", "Barbara", [{"col": "machine", "val": "Barbara"}])
        mp = {"orderCol": "id", "ppm": "ppm", "run": "run_bit", "run2": "cut_count"}
        r = self.save(group=f"m:{bar.pk}", src=c, map=mp)
        self.assertIn("Name the second run signal", r["error"])
        r = self.save(group=f"m:{bar.pk}", src=c, map=mp, run2Name="  Barbara   folder ")
        self.assertTrue(r["ok"], r)
        bar.refresh_from_db()
        self.assertEqual(bar.feed_src["run2"], {"field": "cut_count", "name": "Barbara folder", "runWhen": "high"})
        g = next(x for x in connector.groups() if x["key"] == f"m:{bar.pk}")
        self.assertEqual((g["state"]["map"]["run2"], g["state"]["run2Name"]), ("cut_count", "Barbara folder"))
        mp.pop("run2")
        self.assertTrue(self.save(group=f"m:{bar.pk}", src=c, map=mp)["ok"])
        bar.refresh_from_db()
        self.assertNotIn("run2", bar.feed_src)


class AttributeTableTests(Base):
    """Part specs from a table with one value per row and a code saying which spec it is (part_attribute: partnumber,
    PAID 30 = length, 31 = width, value)."""
    WHERE = {"len": [{"col": "PAID", "val": "30"}], "wid": [{"col": "PAID", "val": "31"}], "rev": [{"col": "PAID", "val": "5"}]}

    def setUp(self):
        super().setUp()
        sch = {**SCHEMA, "tables": SCHEMA["tables"] + [{"name": "part_attribute", "columns": [
            {"name": n, "type": "varchar"} for n in ("id", "partnumber", "PAID", "userDefinedAttributeName", "value", "uom")]}]}
        p = mock.patch.object(engine.integrations, "schema", return_value=sch)
        p.start()
        self.addCleanup(p.stop)

    def test_each_spec_reads_its_own_code(self):
        c = self.card("assets", "part_attribute")
        mp = {"f_part": "partnumber", "rev": "value", "len": "value", "wid": "value", "media": "uom"}
        r = self.save(group="q:part_spec", src=c, map=mp, where={**self.WHERE, "len": [{"col": "PAID", "val": ""}]})
        self.assertIn("Type the value to keep for: Length", r["error"])
        r = self.save(group="q:part_spec", src=c, map=mp, where={**self.WHERE, "len": [{"col": "nope", "val": "30"}]})
        self.assertIn('no column "nope"', r["error"])
        r = self.save(group="q:part_spec", src=c, map=mp, where=self.WHERE)
        self.assertTrue(r["ok"], r)
        sql = SavedQuery.objects.get(name="part_spec").sql
        self.assertIn("MAX(CASE WHEN `PAID` = '30' THEN `value` END) AS len", sql)
        self.assertIn("MAX(`uom`) AS media", sql)         # an input without its own code: the part's rows as they are
        self.assertIn("GROUP BY `partnumber`", sql)
        g = next(x for x in connector.groups() if x["key"] == "q:part_spec")   # read back for the page
        self.assertEqual(g["status"], "done")
        self.assertEqual(g["state"]["map"], mp)
        self.assertEqual(g["state"]["where"], self.WHERE)
        self.assertEqual(g["filters"], [])                 # the codes aren't mistaken for the card's own filters
        # the query really folds the rows into one row for the part (SQLite reads this MySQL quoting too)
        import sqlite3
        db = sqlite3.connect(":memory:")
        db.execute("create table part_attribute (id int, partnumber varchar, PAID int, userDefinedAttributeName varchar, value varchar, uom varchar)")
        db.executemany("insert into part_attribute values (?,?,?,?,?,?)", [
            (1, "1010", 30, "Length", "9.25", "in"), (2, "1010", 31, "Width", "7.5", "in"), (3, "1010", 5, "Rev", "C", ""),
            (4, "1010", 40, "Pleats", "No", ""), (5, "2020", 30, "Length", "11", "in")])
        cur = db.execute(sql, {"part": "1010"})
        row = dict(zip([d[0] for d in cur.description], cur.fetchone()))
        self.assertEqual((row["rev"], row["len"], row["wid"]), ("C", "9.25", "7.5"))
        self.assertIsNone(cur.fetchone())
        self.assertEqual(db.execute(sql, {"part": "9999"}).fetchall(), [])   # no rows: no row of blanks

    def test_only_part_specs_offer_it_and_the_page_shows_it(self):
        from django.template.loader import render_to_string
        gs = {g["key"]: g for g in connector.groups()}
        self.assertEqual(set(gs["q:part_spec"]["filterable"]), {"rev", "pleats", "len", "wid", "ph", "sb", "tol", "media"})
        self.assertEqual(gs["q:asset_settings"]["filterable"], [])
        html = render_to_string("hmi/k/admin/_cgroup.html", {"g": gs["q:part_spec"], "C": {"groups": list(gs.values())}, "tabCan": True})
        self.assertIn('data-for="len"', html)
        self.assertNotIn('data-for="f_part"', html)
        self.assertIn("PAID = the code for length", html)
        self.assertNotIn('class="sfil"', render_to_string("hmi/k/admin/_cgroup.html", {"g": gs["q:asset_settings"], "C": {}, "tabCan": True}))

    def test_test_data_runs_the_folded_query(self):
        c = SourceCard.objects.get(pk=self.card("assets", "part_attribute"))
        g = next(x for x in connector.groups() if x["key"] == "q:part_spec")
        seen = {}

        def fake(sql, params, limit=500, db="assets"):
            seen["sql"] = sql
            return {"columns": ["rev", "len"], "rows": [{"rev": "C", "len": "9.25"}], "ms": 1, "truncated": False}
        with mock.patch.object(connector.integrations, "run_saved", side_effect=fake), \
                mock.patch.object(connector.integrations, "kind_of", return_value="mysql"):
            t = connector.test_data(g, c, {"f_part": "partnumber", "rev": "value", "len": "value"}, slot_where=self.WHERE)
        self.assertIn("MAX(CASE WHEN `PAID` = '5' THEN `value` END) AS rev", seen["sql"])
        self.assertEqual(t["rows"], [["C", "9.25"]])

    def test_drawing_values_kept_as_text(self):
        self.assertEqual([engine._spec_number(v) for v in (12.5, "9.25", "9.25 in", "±0.06", "1,250", "No", None, "")],
                         [12.5, 9.25, 9.25, 0.06, 1250.0, None, None, None])


class DrawingFileTests(Base):
    """The drawing's rev and a link to its file (e.g. on AWS) from their own table, with Part specs from another."""

    def setUp(self):
        super().setUp()
        sch = {**SCHEMA, "tables": SCHEMA["tables"] + [
            {"name": "part_attribute", "columns": [{"name": n, "type": "varchar"} for n in ("partnumber", "PAID", "value")]},
            {"name": "part_drawing", "columns": [{"name": n, "type": "varchar"} for n in ("partnumber", "revision", "s3_url", "updated")]}]}
        p = mock.patch.object(engine.integrations, "schema", return_value=sch)
        p.start()
        self.addCleanup(p.stop)

    def test_its_own_box_newest_rev_first(self):
        g = next(x for x in connector.groups() if x["key"] == "q:part_drawing")
        self.assertEqual([s["k"] for s in g["slots"]], ["f_part", "url", "rev", "newest"])   # per part: no machine column
        c = self.card("assets", "part_drawing")
        r = self.save(group="q:part_drawing", src=c, map={"f_part": "partnumber", "url": "s3_url", "rev": "revision", "newest": "updated"})
        self.assertTrue(r["ok"], r)
        sql = SavedQuery.objects.get(name="part_drawing").sql
        self.assertIn("`s3_url` AS url", sql)
        self.assertIn("ORDER BY `updated` DESC", sql)
        g = next(x for x in connector.groups() if x["key"] == "q:part_drawing")
        self.assertEqual((g["status"], g["state"]["map"]["newest"]), ("done", "updated"))

    def test_part_specs_save_without_a_rev(self):
        c = self.card("assets", "part_attribute")
        r = self.save(group="q:part_spec", src=c, map={"f_part": "partnumber", "len": "value"}, where={"len": [{"col": "PAID", "val": "30"}]})
        self.assertTrue(r["ok"], r)

    def test_a_refused_save_stays_unsaved(self):
        c = self.card("assets", "part_attribute")
        cl = Client(HTTP_HX_REQUEST="true")
        cl.post("/kiosk/do/login", {"pin": "9999"})
        r = cl.post("/kiosk/do/connectorSave", {"group": "q:part_spec", "src": c, "db": "assets", "table": "part_attribute",
                                                "map": json.dumps({"f_part": "partnumber", "len": "value"}),
                                                "where": json.dumps({"len": [{"col": "PAID", "val": ""}]})})
        self.assertContains(r, "Type the value to keep for: Length")
        self.assertContains(r, "data-saved=")   # what's saved goes along, so the page keeps the wires as unsaved
        self.assertContains(r, "&quot;len&quot;: &quot;value&quot;")

    def run_drawing(self, spec_rows, drawing_rows):
        def fake(sql, params, limit=500, db="assets"):
            return {"columns": [], "rows": drawing_rows if "part_drawing" in sql else spec_rows, "ms": 1, "truncated": False}
        with mock.patch.object(engine.integrations, "run_saved", side_effect=fake), \
                mock.patch.object(engine.integrations, "assets_ready", return_value=True):
            return engine.execute("getDrawing", {"part": "MG4017"}, self.ctx)

    def test_get_updated_drawing_keeps_the_link(self):
        from plant.models import PartSpec
        SavedQuery.objects.create(name="part_spec", sql="SELECT 1 FROM part_attribute", saved_by="x")
        SavedQuery.objects.create(name="part_drawing", sql="SELECT 1 FROM part_drawing", saved_by="x")
        PartSpec.objects.filter(key="MG4017").delete()
        link = "https://drawings.s3.amazonaws.com/MG4017_C.pdf?X-Amz-Signature=" + "a" * 1200
        r = self.run_drawing([{"len": "9.25 in", "rev": "B"}], [{"url": link, "rev": "C"}])
        self.assertIn("Drawing rev C loaded", r["msg"])                   # the drawing file's rev wins
        p = PartSpec.objects.get(key="MG4017")
        self.assertEqual((p.rev, p.len, p.drawing_url), ("C", 9.25, link))
        self.run_drawing([{"len": "9.25"}], [{"url": "javascript:alert(1)", "rev": "D"}])
        p.refresh_from_db()
        self.assertEqual((p.rev, p.drawing_url), ("D", link))             # not a web address: the link on file stays
        # a drawing file but no dimensions for a part not on file: estimated specs aren't saved as the drawing's
        PartSpec.objects.filter(key="MG4017").delete()
        r = self.run_drawing([], [{"url": link, "rev": "C"}])
        self.assertIn("no dimensions", r["msg"])
        self.assertFalse(PartSpec.objects.filter(key="MG4017").exists())

    def test_open_drawing_button(self):
        from django.template.loader import render_to_string
        html = render_to_string("hmi/k/_open_drawing.html", {"p": {"url": "https://x.s3.amazonaws.com/a.pdf?s=1&t=2", "rev": "C", "key": "MG4017"}})
        self.assertIn('href="https://x.s3.amazonaws.com/a.pdf?s=1&amp;t=2" target="_blank" rel="noopener noreferrer"', html)
        self.assertEqual(render_to_string("hmi/k/_open_drawing.html", {"p": {"url": "", "rev": "C"}}).strip(), "")
