"""Sensors & custom alarms.

A Sensor is one value the suite reads from somewhere (sensors/kinds.py lists every kind of connection), turned
from its raw reading (volts, mA, PLC counts…) into real units by a straight line through two points, assigned to
a machine (or to the whole plant). Each sensor can have any number of alarm rules (SensorRule). When a rule's
condition holds long enough it opens a SensorAlarm: shown on that machine's kiosk, and, as the rule says, emailed
or texted, turned into a maintenance request and sent to the webhook. Every minute's values go to sensor_values
for Grafana and Reports."""
from django.db import models


class Sensor(models.Model):
    TYPES = [("number", "A number (distance, temperature, pressure…)"), ("onoff", "On / off (a switch or a digital input)")]
    name = models.CharField(max_length=40, unique=True, help_text="Short id used in pushes, formulas and Grafana, e.g. barb_roll")
    label = models.CharField(max_length=80, help_text="What people see, e.g. Media roll diameter")
    machine = models.CharField(max_length=60, blank=True, db_index=True, help_text="The machine it belongs to (blank = plant-wide)")
    kind = models.CharField(max_length=10, default="url")
    config = models.JSONField(default=dict, blank=True, help_text="The connection's own settings (sensors/kinds.py)")
    every = models.IntegerField(default=10, help_text="Seconds between readings (for kinds the server reads)")
    dtype = models.CharField(max_length=6, choices=TYPES, default="number")
    raw_unit = models.CharField(max_length=12, blank=True, help_text="Unit of the raw reading: V, mA, counts…")
    unit = models.CharField(max_length=12, blank=True, help_text="Unit after scaling: mm, in, °F, psi, %…")
    scale = models.BooleanField(default=False, help_text="Convert the raw reading with the two points below")
    raw_lo = models.FloatField(default=0)
    raw_hi = models.FloatField(default=10)
    eng_lo = models.FloatField(default=0)
    eng_hi = models.FloatField(default=10)
    clamp = models.BooleanField(default=True, help_text="Keep the scaled value inside the two points")
    offset = models.FloatField(default=0, help_text="Added after scaling (calibration)")
    decimals = models.IntegerField(default=1)
    smooth = models.IntegerField(default=1, help_text="Average of the last N readings (1 = no averaging)")
    fault_lo = models.FloatField(null=True, blank=True, help_text="A raw reading below this means the sensor is faulty (e.g. 3.6 mA)")
    fault_hi = models.FloatField(null=True, blank=True, help_text="A raw reading above this means the sensor is faulty")
    on_label = models.CharField(max_length=20, default="On")
    off_label = models.CharField(max_length=20, default="Off")
    invert = models.BooleanField(default=False, help_text="On / off sensors: a Low input means On")
    show_kiosk = models.BooleanField(default=True, help_text="Show the live value on the machine's kiosk")
    enabled = models.BooleanField(default=True)
    sort = models.IntegerField(default=0)
    # live state, kept by the server
    raw = models.FloatField(null=True, blank=True)
    value = models.FloatField(null=True, blank=True)
    value_t = models.DateTimeField(null=True, blank=True, help_text="When the last good reading came in")
    read_t = models.DateTimeField(null=True, blank=True, help_text="When the server last tried to read it")
    ok = models.BooleanField(default=True)
    msg = models.CharField(max_length=300, blank=True)
    fault = models.BooleanField(default=False)
    recent = models.JSONField(default=list, blank=True, help_text="[[epoch s, value, raw]], the last 15 minutes")
    state = models.JSONField(default=dict, blank=True, help_text="Per-kind bookkeeping (newest row seen, push time…)")
    updated_by = models.CharField(max_length=120, blank=True)

    class Meta:
        db_table = "sensor_sensors"
        ordering = ["machine", "sort", "label", "id"]

    def __str__(self):
        return self.label or self.name

    def fmt(self, v=None):
        v = self.value if v is None else v
        if v is None:
            return "—"
        if self.dtype == "onoff":
            return self.on_label if v >= 0.5 else self.off_label
        d = max(0, min(6, self.decimals))
        return f"{v:,.{d}f}" + (f" {self.unit}" if self.unit else "")


class SensorRule(models.Model):
    OPS = [("lt", "falls below"), ("le", "is at or below"), ("gt", "rises above"), ("ge", "is at or above"), ("eq", "equals"),
           ("ne", "is not"), ("in", "is between"), ("out", "is outside"), ("on", "turns on"), ("off", "turns off"),
           ("rise", "rises faster than (per minute)"), ("fall", "falls faster than (per minute)"), ("stuck", "doesn't change for (minutes)"),
           ("stale", "has no new reading for (minutes)"), ("fault", "reads as faulty (outside the fault limits)")]
    WHEN = [("always", "Always"), ("running", "Only while the machine is running"), ("shift", "Only while a shift is running on it"),
            ("stopped", "Only while the machine is stopped"), ("noshift", "Only when no shift is running")]
    SEV = [("info", "Notice"), ("warn", "Warning"), ("bad", "Critical")]
    sensor = models.ForeignKey(Sensor, related_name="rules", on_delete=models.CASCADE)
    name = models.CharField(max_length=80, blank=True, help_text="e.g. Roll almost empty")
    op = models.CharField(max_length=6, choices=OPS, default="lt")
    a = models.FloatField(null=True, blank=True)
    b = models.FloatField(null=True, blank=True)
    delay = models.IntegerField(default=0, help_text="Seconds the condition must hold before the alarm")
    deadband = models.FloatField(default=0, help_text="How far back past the limit before the alarm clears")
    clear_delay = models.IntegerField(default=0, help_text="Seconds it must be clear before the alarm ends")
    when = models.CharField(max_length=8, choices=WHEN, default="always")
    severity = models.CharField(max_length=4, choices=SEV, default="warn")
    message = models.CharField(max_length=300, blank=True, help_text="Alarm text; {value} {limit} {limit2} {unit} {sensor} {machine} {raw} {name}")
    latch = models.BooleanField(default=False, help_text="Stays on the kiosk until someone acknowledges it, even after it clears")
    show_kiosk = models.BooleanField(default=True)
    show_all = models.BooleanField(default=False, help_text="Show on every machine's kiosk, not only this one's")
    notify = models.BooleanField(default=False)
    contacts = models.JSONField(default=list, blank=True, help_text="Alert contact ids; empty = everyone set up for this machine")
    notify_after = models.IntegerField(default=0, help_text="Minutes the alarm must last before messages go out")
    repeat = models.IntegerField(default=0, help_text="Send again every N minutes while it lasts (0 = once)")
    notify_clear = models.BooleanField(default=False)
    ticket = models.BooleanField(default=False, help_text="Open a maintenance request")
    ticket_type = models.CharField(max_length=80, blank=True)
    ticket_priority = models.CharField(max_length=10, default="Medium")
    webhook = models.BooleanField(default=True)
    enabled = models.BooleanField(default=True)
    sort = models.IntegerField(default=0)
    # state
    pending_since = models.DateTimeField(null=True, blank=True)
    clearing_since = models.DateTimeField(null=True, blank=True)
    alarm = models.ForeignKey("SensorAlarm", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        db_table = "sensor_rules"
        ordering = ["sort", "id"]

    def __str__(self):
        return self.name or f"{self.sensor} {self.get_op_display()}"


class SensorAlarm(models.Model):
    rule = models.ForeignKey(SensorRule, null=True, on_delete=models.SET_NULL, related_name="alarms")
    sensor = models.CharField(max_length=40, db_index=True)
    label = models.CharField(max_length=80)
    machine = models.CharField(max_length=60, blank=True, db_index=True)
    severity = models.CharField(max_length=4, default="warn")
    text = models.CharField(max_length=300)
    start = models.DateTimeField(db_index=True)
    end = models.DateTimeField(null=True, blank=True, db_index=True)
    value = models.FloatField(null=True, blank=True, help_text="Value when it started")
    worst = models.FloatField(null=True, blank=True, help_text="Furthest past the limit while it lasted")
    latch = models.BooleanField(default=False)
    all_screens = models.BooleanField(default=False)
    show_kiosk = models.BooleanField(default=True)
    acked_by = models.CharField(max_length=120, blank=True)
    acked_t = models.DateTimeField(null=True, blank=True)
    sent = models.IntegerField(default=0, help_text="Rounds of messages sent")
    sent_t = models.DateTimeField(null=True, blank=True)
    ticket_id = models.IntegerField(null=True, blank=True)

    class Meta:
        db_table = "sensor_alarms"
        ordering = ["-start", "-id"]

    @property
    def active(self):
        return self.end is None

    @property
    def on_screen(self):
        """Shown on the kiosk: while it lasts, and a latched one until someone acknowledges it."""
        return self.show_kiosk and not self.acked_t and (self.end is None or self.latch)


class SensorValue(models.Model):
    """One row per sensor per minute, for Grafana and Reports."""
    sensor = models.CharField(max_length=40, db_index=True)
    machine = models.CharField(max_length=60, blank=True, db_index=True)
    t = models.DateTimeField(db_index=True)
    value = models.FloatField(null=True)
    vmin = models.FloatField(null=True)
    vmax = models.FloatField(null=True)
    raw = models.FloatField(null=True)
    n = models.IntegerField(default=0)
    alarm = models.BooleanField(default=False)

    class Meta:
        db_table = "sensor_values"
        ordering = ["t"]
        constraints = [models.UniqueConstraint(fields=["sensor", "t"], name="sensor_value_minute")]
