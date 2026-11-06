"""Records the Operator Kiosk keeps. Table names match the ones Grafana dashboards query
(hmi_ppm, hmi_downtime, hmi_qc, hmi_shifts, hmi_mo_runs, hmi_tickets)."""
from django.db import models

from plant.models import MO, Machine, Person


class ShiftRun(models.Model):
    # SET_NULL: removing a machine keeps its shift history (the `line` name stays on every record)
    machine = models.ForeignKey(Machine, null=True, blank=True, on_delete=models.SET_NULL, related_name="runs")
    line = models.CharField(max_length=60, db_index=True)
    person = models.ForeignKey(Person, null=True, blank=True, on_delete=models.SET_NULL)
    user_name = models.CharField(max_length=120, blank=True)
    shift_name = models.CharField(max_length=60, blank=True)
    start = models.DateTimeField(db_index=True)
    end = models.DateTimeField(null=True, blank=True)
    active = models.BooleanField(default=True, db_index=True)
    mo = models.ForeignKey(MO, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    mo_start = models.DateTimeField(null=True, blank=True)
    count = models.IntegerField(default=0)
    mo_count = models.IntegerField(default=0)
    offer = models.CharField(max_length=20, blank=True, help_text="A log started at a shift change: the person logged in on the one before, asked whether they're working into it")
    stop_what = models.CharField(max_length=120, blank=True, help_text="What stopped, for the stop going on now (e.g. the second run signal's name)")
    acc = models.FloatField(default=0)
    state = models.CharField(max_length=12, default="running")  # running | stopped | changeover | idle
    stop_start = models.DateTimeField(null=True, blank=True)
    co = models.JSONField(null=True, blank=True)
    override = models.JSONField(null=True, blank=True)
    issues = models.TextField(blank=True)
    end_issues = models.TextField(blank=True)
    safety = models.JSONField(default=list)
    pm = models.JSONField(default=list)
    last_count = models.BigIntegerField(null=True, blank=True)
    draw_checked = models.DateTimeField(null=True, blank=True)
    qc_last = models.DateTimeField(null=True, blank=True)
    qc_after_co = models.BooleanField(default=False)   # a QC check is due as soon as the machine runs after a changeover
    qc_due_at = models.DateTimeField(null=True, blank=True)   # when that check became due
    uptime = models.IntegerField(null=True, blank=True)
    down_mins = models.IntegerField(null=True, blank=True)

    class Meta:
        db_table = "hmi_shifts"
        ordering = ["-start"]


class LineState(models.Model):
    """Live feed state for a machine (independent of whether a shift is running)."""
    machine = models.OneToOneField(Machine, on_delete=models.CASCADE, related_name="live")
    next_tick = models.DateTimeField(null=True, blank=True)
    last_tick = models.DateTimeField(null=True, blank=True)
    demo_idx = models.IntegerField(default=0)
    gap = models.IntegerField(default=0)
    feed = models.JSONField(default=dict, blank=True)
    pushed = models.JSONField(null=True, blank=True)
    pushed_at = models.DateTimeField(null=True, blank=True)
    err_state = models.JSONField(default=dict, blank=True)  # where the alarm log table was read up to

    class Meta:
        db_table = "hmi_line_state"


class PpmReading(models.Model):
    machine = models.ForeignKey(Machine, null=True, blank=True, on_delete=models.SET_NULL)
    line = models.CharField(max_length=60, db_index=True)
    shift = models.ForeignKey(ShiftRun, null=True, on_delete=models.SET_NULL, related_name="readings")
    t = models.DateTimeField(db_index=True)
    ppm = models.IntegerField()
    units = models.IntegerField(default=0, help_text="Filters made in this minute")
    planned = models.BooleanField(default=False, help_text="Minute inside a planned break, lunch or start-up (not downtime)")
    run = models.BooleanField(default=True, help_text="Running this minute: the machine's run signal if it has one, otherwise speed above the stop speed")
    recipe = models.CharField(max_length=80, blank=True, default="", help_text="Recipe loaded in the machine this minute")

    class Meta:
        db_table = "hmi_ppm"
        ordering = ["t"]


class Downtime(models.Model):
    shift = models.ForeignKey(ShiftRun, null=True, blank=True, on_delete=models.SET_NULL)
    line = models.CharField(max_length=60, db_index=True)
    mo = models.CharField(max_length=40, blank=True)
    start_time = models.DateTimeField(db_index=True)
    end_time = models.DateTimeField()
    minutes = models.IntegerField()
    reason = models.CharField(max_length=120, null=True, blank=True)
    notes = models.TextField(blank=True)
    by = models.CharField(max_length=120, blank=True)

    class Meta:
        db_table = "hmi_downtime"
        ordering = ["start_time"]


class QCCheck(models.Model):
    shift = models.ForeignKey(ShiftRun, null=True, on_delete=models.SET_NULL)
    t = models.DateTimeField(db_index=True)
    line = models.CharField(max_length=60)
    mo = models.CharField(max_length=40, blank=True)
    item = models.CharField(max_length=40)
    by = models.CharField(max_length=120)
    # the drawing measurements and standard checks have their own columns (Grafana and Reports read them);
    # every item the stage checks (Admin › QC checks) is in `values`
    len = models.FloatField(null=True, blank=True)
    wid = models.FloatField(null=True, blank=True)
    ph = models.FloatField(null=True, blank=True)
    sbl = models.FloatField(null=True, blank=True)
    sbr = models.FloatField(null=True, blank=True)
    glue = models.CharField(max_length=4, blank=True)
    cut = models.CharField(max_length=4, blank=True)
    scrap = models.IntegerField(default=0)
    passed = models.BooleanField()
    values = models.JSONField(default=dict, blank=True)
    fails = models.JSONField(default=list, blank=True)        # keys of the items out of spec
    due_at = models.DateTimeField(null=True, blank=True)
    late_reason = models.TextField(blank=True)
    review = models.CharField(max_length=12, blank=True)      # "" | pending | approved | disapproved (out-of-spec checks)
    reviewed_by = models.CharField(max_length=120, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    scrapped = models.IntegerField(default=0)                 # filters taken off the count when disapproved
    edited_by = models.CharField(max_length=120, blank=True)
    edited_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "hmi_qc"
        ordering = ["t"]


class RollTag(models.Model):
    shift = models.ForeignKey(ShiftRun, null=True, on_delete=models.SET_NULL)
    t = models.DateTimeField()
    line = models.CharField(max_length=60)
    roll = models.CharField(max_length=80)
    material = models.CharField(max_length=120, blank=True)
    lot = models.CharField(max_length=80, blank=True)
    feet = models.IntegerField(null=True, blank=True)
    mo = models.CharField(max_length=40, blank=True)
    count_at = models.IntegerField(default=0)
    by = models.CharField(max_length=120)

    class Meta:
        db_table = "hmi_rolls"
        ordering = ["t"]


class Ticket(models.Model):
    shift = models.ForeignKey(ShiftRun, null=True, blank=True, on_delete=models.SET_NULL)
    t = models.DateTimeField()
    line = models.CharField(max_length=60, blank=True)
    by = models.CharField(max_length=120)
    machine = models.CharField(max_length=60)
    type = models.CharField(max_length=80)
    priority = models.CharField(max_length=10, default="Medium")
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=12, default="Open")
    updated = models.DateTimeField(null=True, blank=True)
    updated_by = models.CharField(max_length=120, blank=True)

    class Meta:
        db_table = "hmi_tickets"
        ordering = ["t"]


class TicketNote(models.Model):
    """A repair note on a maintenance request: what was found, what was done, parts used."""
    ticket = models.ForeignKey(Ticket, related_name="repairs", on_delete=models.CASCADE)
    t = models.DateTimeField()
    by = models.CharField(max_length=120)
    text = models.TextField()
    kind = models.CharField(max_length=12, default="note")   # note | started | closed

    class Meta:
        db_table = "hmi_ticket_notes"
        ordering = ["t", "id"]


class TicketPhoto(models.Model):
    """A picture of what is wrong, taken on a tablet or phone when the ticket is opened (or added later).
    Kept in the records database, so it is backed up with everything else. The server keeps a 1600 px JPEG and a thumbnail."""
    ticket = models.ForeignKey(Ticket, related_name="photos", on_delete=models.CASCADE)
    t = models.DateTimeField()
    by = models.CharField(max_length=120)
    content_type = models.CharField(max_length=20, default="image/jpeg")
    data = models.BinaryField()
    thumb = models.BinaryField(default=b"", blank=True)
    size = models.IntegerField(default=0)
    width = models.IntegerField(default=0)
    height = models.IntegerField(default=0)

    class Meta:
        db_table = "hmi_ticket_photo"
        ordering = ["t", "id"]


class MachineError(models.Model):
    """An error or alarm the machine reported. From a field of its reading (open while the value stays the
    same, closed when it clears) or from rows of its alarm log table (one entry per row)."""
    machine = models.ForeignKey(Machine, null=True, blank=True, on_delete=models.SET_NULL)
    line = models.CharField(max_length=60, db_index=True)
    start = models.DateTimeField(db_index=True)
    end = models.DateTimeField(null=True, blank=True, help_text="Blank while the error is still showing")
    code = models.CharField(max_length=80, blank=True)
    text = models.CharField(max_length=300, blank=True)
    source = models.CharField(max_length=8, default="feed")  # feed | log

    class Meta:
        db_table = "hmi_machine_errors"
        ordering = ["start", "id"]


class ShiftNote(models.Model):
    shift = models.ForeignKey(ShiftRun, null=True, blank=True, on_delete=models.SET_NULL)
    t = models.DateTimeField()
    line = models.CharField(max_length=60, blank=True)
    by = models.CharField(max_length=120)
    text = models.TextField()

    class Meta:
        db_table = "hmi_notes"
        ordering = ["t"]


class MoRun(models.Model):
    shift = models.ForeignKey(ShiftRun, null=True, on_delete=models.SET_NULL)
    line = models.CharField(max_length=60)
    mo = models.CharField(max_length=40)
    item = models.CharField(max_length=40)
    qty = models.IntegerField(default=0)
    scrap = models.IntegerField(default=0)
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()

    class Meta:
        db_table = "hmi_mo_runs"
        ordering = ["start_time"]


class Notice(models.Model):
    """Something an operator should know: the schedule changed, a break was added, a maintenance request moved on.
    `lines` are the machines it is for (empty = every line); each machine's screen marks it read on its own."""
    t = models.DateTimeField()
    text = models.CharField(max_length=300)
    kind = models.CharField(max_length=12, default="schedule")
    read = models.BooleanField(default=False)
    lines = models.JSONField(default=list, blank=True)
    go = models.CharField(max_length=20, blank=True, help_text="Operator page that shows more")

    class Meta:
        db_table = "hmi_notices"
        ordering = ["-t", "-id"]


class NoticeRead(models.Model):
    notice = models.ForeignKey(Notice, related_name="reads", on_delete=models.CASCADE)
    line = models.CharField(max_length=60)
    by = models.CharField(max_length=120, blank=True)
    t = models.DateTimeField()

    class Meta:
        db_table = "hmi_notice_reads"
        unique_together = [("notice", "line")]


class AuditLog(models.Model):
    t = models.DateTimeField(db_index=True)
    by = models.CharField(max_length=120)
    what = models.TextField()

    class Meta:
        db_table = "hmi_audit"
        ordering = ["-t", "-id"]


class Event(models.Model):
    """Outbox for the optional webhook that copies records to another system."""
    t = models.DateTimeField()
    kind = models.CharField(max_length=30)
    data = models.JSONField(default=dict)
    sent = models.BooleanField(default=False, db_index=True)

    class Meta:
        db_table = "hmi_events"


class SavedQuery(models.Model):
    """SQL the server runs against the asset database during a changeover."""
    name = models.CharField(max_length=40, unique=True)
    sql = models.TextField()
    params = models.JSONField(default=dict, blank=True)
    saved_by = models.CharField(max_length=120, blank=True)
    saved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "hmi_saved_queries"


class SourceCard(models.Model):
    """A source card of the connector builder (Admin › Connector builder): one table, the columns picked from it,
    and the rows it keeps (filters). Several cards can read the same table, e.g. one table that logs every machine,
    with a card per machine. The data needs wired to a card take its table and its filters."""
    title = models.CharField(max_length=80)
    db = models.CharField(max_length=12)
    table = models.CharField(max_length=200)
    columns = models.JSONField(default=list, blank=True)   # the columns shown on the card; empty = all of them
    filters = models.JSONField(default=list, blank=True)   # [{"col": .., "val": ..}]: only rows where col = val (all of them)
    sort = models.IntegerField(default=0)
    updated_by = models.CharField(max_length=120, blank=True)
    updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "hmi_source_cards"
        ordering = ["sort", "id"]


class ToolingCache(models.Model):
    """Last tooling the asset database returned for a part on a machine (screws, helix, media, side bands)."""
    part = models.CharField(max_length=40)
    machine = models.CharField(max_length=60)
    groups = models.JSONField(default=list)
    t = models.DateTimeField()

    class Meta:
        db_table = "hmi_tooling_cache"
        unique_together = [("part", "machine")]


class ChangeoverCache(models.Model):
    """Last steps the asset database returned for a part on a machine, used when it is unreachable."""
    part = models.CharField(max_length=40)
    machine = models.CharField(max_length=60)
    steps = models.JSONField(default=list)
    settings = models.JSONField(default=list)
    t = models.DateTimeField()

    class Meta:
        db_table = "hmi_co_cache"
        unique_together = [("part", "machine")]


class DataSource(models.Model):
    """A system that sends data to the suite (Admin > Connections > Incoming data): for example a machine vision
    QC system. It either pushes to /api/ingest/<name> with its own key, or the server collects from its URL."""
    KINDS = (("qc", "QC measurements"), ("readings", "Other readings"))
    name = models.CharField(max_length=60, unique=True)
    kind = models.CharField(max_length=12, choices=KINDS, default="qc")
    line = models.CharField(max_length=60, blank=True, help_text="Fixed line, or blank to take it from the data")
    mode = models.CharField(max_length=8, default="push", help_text="push | poll")
    url = models.CharField(max_length=500, blank=True)
    every = models.IntegerField(default=60, help_text="Seconds between collections (poll)")
    key_hash = models.CharField(max_length=128, blank=True)
    mapping = models.JSONField(default=dict, blank=True)
    enabled = models.BooleanField(default=True)
    last_at = models.DateTimeField(null=True, blank=True)
    last_ok = models.BooleanField(default=True)
    last_msg = models.CharField(max_length=300, blank=True)
    received = models.BigIntegerField(default=0)
    sample = models.JSONField(null=True, blank=True)
    poll_mark = models.CharField(max_length=80, blank=True)
    next_poll = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "hmi_sources"
        ordering = ["name"]


class AutoQC(models.Model):
    """One measurement from an automatic QC system (machine vision). Kept apart from the operators' QC checks."""
    source = models.ForeignKey(DataSource, null=True, on_delete=models.SET_NULL)
    source_name = models.CharField(max_length=60)
    shift = models.ForeignKey(ShiftRun, null=True, on_delete=models.SET_NULL)
    t = models.DateTimeField(db_index=True)
    line = models.CharField(max_length=60, db_index=True)
    mo = models.CharField(max_length=40, blank=True)
    item = models.CharField(max_length=40, blank=True)
    len = models.FloatField(null=True, blank=True)
    wid = models.FloatField(null=True, blank=True)
    ph = models.FloatField(null=True, blank=True)
    sbl = models.FloatField(null=True, blank=True)
    sbr = models.FloatField(null=True, blank=True)
    passed = models.BooleanField()
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "hmi_qc_auto"
        ordering = ["t"]


class InboundRecord(models.Model):
    """Readings from a data source that aren't QC measurements, kept for reports and Grafana."""
    source = models.ForeignKey(DataSource, null=True, on_delete=models.SET_NULL)
    source_name = models.CharField(max_length=60)
    t = models.DateTimeField(db_index=True)
    line = models.CharField(max_length=60, blank=True, db_index=True)
    data = models.JSONField(default=dict)

    class Meta:
        db_table = "hmi_inbound"
        ordering = ["t"]


class AlertContact(models.Model):
    """Someone to text or email when a line is down (Operator Kiosk > Admin > Alerts)."""
    name = models.CharField(max_length=120)
    email = models.CharField(max_length=200, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    lines = models.JSONField(default=list, blank=True, help_text="Line names; empty = every line")
    down_after = models.IntegerField(default=15, help_text="Minutes a line must be down before this person is told")
    notify_back = models.BooleanField(default=True)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = "hmi_alert_contacts"
        ordering = ["name"]


class AlertLog(models.Model):
    t = models.DateTimeField(db_index=True)
    kind = models.CharField(max_length=12)  # down | back | test
    line = models.CharField(max_length=60, blank=True)
    stop_key = models.CharField(max_length=100, blank=True, db_index=True)
    contact = models.CharField(max_length=120, blank=True)
    channel = models.CharField(max_length=8)  # email | sms
    to = models.CharField(max_length=200)
    ok = models.BooleanField(default=True)
    msg = models.CharField(max_length=300, blank=True)

    class Meta:
        db_table = "hmi_alert_log"
        ordering = ["-t"]


class PlannedTime(models.Model):
    """Planned not-running time for each shift run (breaks, lunches, start-up), so Grafana can leave it out
    the same way the suite does: join or filter on these, or use hmi_ppm.planned."""
    shift = models.ForeignKey(ShiftRun, null=True, on_delete=models.CASCADE)
    line = models.CharField(max_length=60, db_index=True)
    start = models.DateTimeField(db_index=True)
    end = models.DateTimeField()
    kind = models.CharField(max_length=60)

    class Meta:
        db_table = "hmi_planned"
        ordering = ["start"]
