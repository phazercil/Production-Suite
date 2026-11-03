"""Master data shared by every part of the suite: machines, shifts, parts, people and orders.

The Scheduler plans MOs onto machines; the Operator Kiosk runs them; the Data Kiosk shows them.
All three read and write these same tables, so there is nothing to sync between them.
"""
import copy
import re

from django.db import models

STAGES = [
    ("s1", "Stage 1 · Pleating"),
    ("eb", "Stage 2 · End Bands"),
    ("ac", "Arts & Crafts"),
    ("gb", "Glue Bead"),
]
STAGE_ORDER = [s for s, _ in STAGES]
SPEED_MODES = {
    "s1": [("old", "Cost sheet PPM (current machine)"), ("new", "Cost sheet PPM (new machine)"), ("fixed", "Fixed PPM")],
    "eb": [("sheet", "Cost sheet Stage 2 rate"), ("mask", "Mask qty × 30 (3rd/4th bander)"), ("fixed", "Fixed filters/hr")],
    "ac": [("sheet", "Cost sheet Stage 3 rate"), ("fixed", "Fixed filters/hr")],
    "gb": [("fixed", "Fixed filters/hr")],
}
FEED_TYPES = [("demo", "Demo replay"), ("http", "Read from URL"), ("push", "Pushed to server"), ("influx", "InfluxDB query"),
              ("db", "Database table"), ("none", "Not connected")]
PALETTE = ["#c46c3c", "#5e9951", "#3f7fb5", "#8c5bb0", "#b5933a", "#3a9a98", "#b54a6a", "#6b7a8a"]


class Shift(models.Model):
    name = models.CharField(max_length=60)
    start = models.CharField(max_length=5, help_text="HH:MM")
    end = models.CharField(max_length=5, help_text="HH:MM; earlier than start runs past midnight")
    days = models.JSONField(default=list, help_text="Weekdays worked, 0=Sun … 6=Sat")
    sort = models.IntegerField(default=0)

    class Meta:
        ordering = ["sort", "id"]
        db_table = "plant_shift"

    def __str__(self):
        return f"{self.name} {self.start}–{self.end}"


class ShiftBreak(models.Model):
    shift = models.ForeignKey(Shift, related_name="breaks", on_delete=models.CASCADE)
    label = models.CharField(max_length=60, default="Break")
    start = models.CharField(max_length=5)
    end = models.CharField(max_length=5)

    class Meta:
        ordering = ["start", "id"]
        db_table = "plant_shift_break"


class Machine(models.Model):
    """A line or station. Stage 1 machines are the pleaters that have an operator kiosk."""
    name = models.CharField(max_length=60, unique=True)
    code = models.CharField(max_length=12, blank=True, help_text="S2K process code, e.g. BARB")
    stage = models.CharField(max_length=2, choices=STAGES, default="s1")
    speed_mode = models.CharField(max_length=8, default="fixed")
    ppm = models.FloatField(default=180, help_text="Stage 1: rated / fixed pleats per minute")
    rate = models.FloatField(default=0, help_text="Other stages: filters per hour when the sheet has no rate")
    scrap_pct = models.FloatField(default=5)
    downtime_pct = models.FloatField(default=10)
    changeover_min = models.FloatField(default=30)
    color = models.CharField(max_length=7, default=PALETTE[0])
    active = models.BooleanField(default=True, help_text="Out of service machines are hidden from Start Shift")
    shifts = models.ManyToManyField(Shift, blank=True, related_name="machines")
    sort = models.IntegerField(default=0)
    # machine data feed (Stage 1)
    feed_type = models.CharField(max_length=6, choices=FEED_TYPES, default="none")
    feed_url = models.CharField(max_length=300, blank=True)
    feed_query = models.TextField(blank=True, default="", help_text="InfluxDB query for this machine's readings ({machine} = its name)")
    feed_poll = models.IntegerField(default=10)
    feed_ppm_field = models.CharField(max_length=80, default="ppm")
    feed_count_field = models.CharField(max_length=80, blank=True, help_text="Filters cut (the machine's counter)")
    feed_recipe_field = models.CharField(max_length=80, blank=True, help_text="Recipe loaded in the machine")
    # "Database table" source: {"db": "machines", "table": .., "orderCol": newest row by, "keyCol": .., "keyVal": .., "maxAge": minutes}
    feed_src = models.JSONField(default=dict, blank=True)
    # Second place the run signal (High = running, Low = stopped) comes from, for uptime and downtime:
    # {"type": "" (use the speed) | "feed" (a field of the reading above) | "db" (a column of a table), "field": ..,
    #  "db", "table", "orderCol", "keyCol", "keyVal", "maxAge", "runWhen": "high" | "low"}
    uptime_src = models.JSONField(default=dict, blank=True)
    # Errors and alarms from the machine, kept so each downtime shows what the machine reported:
    # {"type": "" | "feed" (fields of the reading: "field" = code, "textField") | "db" (an alarm log table: "db", "table",
    #  "orderCol", "keyCol", "keyVal", "timeCol", "codeCol", "textCol"), "ok": values that mean no error}
    error_src = models.JSONField(default=dict, blank=True)
    demo_scale = models.FloatField(default=1)
    demo_offset = models.IntegerField(default=0)

    class Meta:
        ordering = ["sort", "id"]
        db_table = "plant_machine"

    def __str__(self):
        return self.name

    @property
    def is_pleater(self):
        return self.stage == "s1"


class PartSpec(models.Model):
    """Drawing specs the QC check measures against. Prefixes like 1MG/2MG share the MG spec."""
    key = models.CharField(max_length=40, unique=True)
    pleats = models.FloatField()
    len = models.FloatField()
    wid = models.FloatField()
    ph = models.FloatField()
    sb = models.FloatField()
    tol = models.FloatField(default=0.06)
    rev = models.CharField(max_length=10, default="A")
    media = models.CharField(max_length=80, blank=True)

    class Meta:
        ordering = ["key"]
        db_table = "plant_part_spec"

    def as_dict(self):
        return {"key": self.key, "pleats": self.pleats, "len": self.len, "wid": self.wid, "ph": self.ph, "sb": self.sb,
                "tol": self.tol, "rev": self.rev, "media": self.media}


class CostItem(models.Model):
    """One row of the filter cost sheet: pleat counts and stage rates per part."""
    aq = models.CharField(max_length=40, db_index=True)
    cust = models.CharField(max_length=40, blank=True, db_index=True)
    cat = models.CharField(max_length=40)
    pieces = models.FloatField(null=True, blank=True)
    pleats = models.FloatField()
    ppm = models.FloatField(null=True, blank=True)
    nppm = models.FloatField(null=True, blank=True)
    s2 = models.FloatField(null=True, blank=True)
    s3 = models.FloatField(null=True, blank=True)
    mask = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ["id"]
        db_table = "plant_cost_item"

    @property
    def uid(self):
        return f"{self.aq}|{self.cust}|{self.cat}"

    def as_dict(self):
        return {"uid": self.uid, "aq": self.aq, "cust": self.cust, "cat": self.cat, "pieces": self.pieces, "pleats": self.pleats,
                "ppm": self.ppm, "nppm": self.nppm, "s2": self.s2, "s3": self.s3, "mask": self.mask}


class Person(models.Model):
    ROLES = [("operator", "Operator"), ("maintenance", "Maintenance"), ("admin", "Shift lead")]
    name = models.CharField(max_length=120)
    pin = models.CharField(max_length=12, blank=True)
    role = models.CharField(max_length=12, choices=ROLES, default="operator")
    trained = models.ManyToManyField(Machine, blank=True, related_name="trained_people")
    active = models.BooleanField(default=True)
    source = models.CharField(max_length=10, default="local")  # local | paycom | list
    eecode = models.CharField(max_length=40, blank=True, db_index=True)
    ext_id = models.CharField(max_length=60, blank=True, db_index=True)
    dept = models.CharField(max_length=120, blank=True)
    position = models.CharField(max_length=120, blank=True)
    left_list = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]
        db_table = "plant_person"

    def __str__(self):
        return self.name

    @property
    def is_lead(self):
        return self.role == "admin"


class MO(models.Model):
    """A manufacturing order. The Scheduler sets `line`, `seq` and the plan times for the Stage 1 queue."""
    number = models.CharField(max_length=40, unique=True)
    part = models.CharField(max_length=40)
    qty = models.IntegerField(default=0)
    due = models.DateField(null=True, blank=True)
    pleats = models.FloatField(null=True, blank=True, help_text="Only when the part is not on the cost sheet")
    ops = models.JSONField(default=dict, help_text='Stages this order needs, e.g. {"s1":true,"eb":true}')
    item_uid = models.CharField(max_length=120, blank=True, null=True, help_text="Chosen cost sheet row when several match")
    produced = models.IntegerField(default=0)
    scrap = models.IntegerField(default=0)
    status = models.CharField(max_length=5, default="OPEN")  # OPEN | CLOSE
    hot = models.BooleanField(default=False)
    process = models.CharField(max_length=12, blank=True)
    mfg = models.CharField(max_length=12, default="MFGOUT")
    source = models.CharField(max_length=10, default="manual")  # s2k | import | manual | floor | demo
    added = models.DateField(auto_now_add=True)
    example = models.BooleanField(default=False)
    # Stage 1 queue, written by the scheduler
    line = models.ForeignKey(Machine, null=True, blank=True, on_delete=models.SET_NULL, related_name="queue")
    seq = models.IntegerField(default=0)
    plan_start = models.DateTimeField(null=True, blank=True)
    plan_end = models.DateTimeField(null=True, blank=True)
    plan_co = models.FloatField(default=0)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["due", "number"]
        db_table = "plant_mo"
        verbose_name = "MO"

    def __str__(self):
        return f"MO {self.number} · {self.part}"


def part_key(item):
    """1MG4108 and 2MG4108 share the MG4108 spec."""
    return re.sub(r"^\d(?=[A-Z])", "", str(item or "").strip().upper())


DEFAULT_CONFIG = {
    "reasons": ["Material / media change", "Jam or misfeed", "Changeover", "Maintenance", "Break / lunch", "Quality hold", "No operator",
                "Glue system", "Waiting on schedule", "Other"],
    "safety": ["Guards in place, E-stop tested", "Glue pot at temperature", "Blade and score wheels inspected", "Work area clear, floor dry",
               "PPE on: glasses, gloves, hearing"],
    "pm": ["Lubricate pleater chain", "Clean glue nozzles", "Air pressure 80–90 psi", "Empty scrap bins", "Inspect conveyor belts"],
    "coSteps": ["Stop line and lock out", "Remove old media, labels and scrap", "Load new media roll and scan the roll tag",
                "Set pleat height and pleat count from the drawing", "Adjust side band guides to width", "Check glue temperature and bead",
                "Run first article and complete a QC check", "Lead operator sign-off"],
    "issueTypes": ["Pleater jam", "Blade / cutter", "Glue system", "Side band applicator", "Electrical / sensor", "Air / pneumatics",
                   "Guarding / safety", "Other"],
    "equipment": ["Glue system", "Conveyor", "Compressor", "Other"],
    "rules": {"stopPpm": 5, "minDown": 2, "coMins": 45, "eff": 85, "adminTimeout": 10, "reasonAfter": 10, "startGrace": 15,
              "photoDays": 365},
}
DEFAULT_SETTINGS = {
    "autoQC": True, "qcEvery": 30, "s2kUrl": "", "s2kSource": "url", "s2kEvery": 0, "peopleUrl": "", "peopleEvery": 0, "webhook": "", "useDb": False,
    "grafana": {"on": False, "url": "", "uid": "", "slug": "pleat-lines", "org": 1, "ppm": 2, "uptime": 4, "count": 6, "refresh": "10s",
                "lineVar": "line"},
    "paycom": {"auto": False, "every": 60, "dept": "", "position": "", "pinFrom": "code", "last": None, "msg": ""},
    "kiosk": {"layout": "panels", "range": "now-12h", "cols": 3, "rotate": 0,
              "panels": [{"title": "Pleater speed", "panel": 2}, {"title": "Uptime", "panel": 4}, {"title": "Units", "panel": 6}]},
}
DEFAULT_PLANNER = {"coSamePart": False, "coFirst": True}


class PlantSettings(models.Model):
    """One row holding lists, rules and connection settings. Edited from Operator › Admin and Scheduler › Lines."""
    config = models.JSONField(default=dict)
    settings = models.JSONField(default=dict)
    planner = models.JSONField(default=dict)
    catalog_source = models.JSONField(default=dict, blank=True)
    conn = models.JSONField(default=dict, blank=True)
    network = models.JSONField(default=dict, blank=True, help_text="Plant network ranges screens may connect from (Admin > Network)")
    demo = models.BooleanField(default=False)
    plan_at = models.DateTimeField(null=True, blank=True)
    version = models.BigIntegerField(default=0, help_text="Bumped on every change so screens know to refresh")
    ticker_owner = models.CharField(max_length=80, blank=True, help_text="Process currently running the minute ticker")
    ticker_seen = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "plant_settings"
        verbose_name_plural = "plant settings"

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        changed = False
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg.update(obj.config or {})
        cfg["rules"] = {**DEFAULT_CONFIG["rules"], **(obj.config or {}).get("rules", {})}
        st = copy.deepcopy(DEFAULT_SETTINGS)
        for k, v in (obj.settings or {}).items():
            if isinstance(v, dict) and isinstance(st.get(k), dict):
                st[k].update(v)
            else:
                st[k] = v
        pl = {**DEFAULT_PLANNER, **(obj.planner or {})}
        if cfg != obj.config or st != obj.settings or pl != obj.planner:
            obj.config, obj.settings, obj.planner, changed = cfg, st, pl, True
        if changed:
            obj.save()
        return obj

    @classmethod
    def bump(cls):
        cls.objects.filter(pk=1).update(version=models.F("version") + 1)
