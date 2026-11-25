"""Maintenance › Inventory: the parts and supplies maintenance keeps, and every one used on a machine (with the
request it was used for, when there is one), added to stock or counted."""
from django.db import models


class InventoryItem(models.Model):
    name = models.CharField(max_length=120)
    part_no = models.CharField(max_length=60, blank=True)
    location = models.CharField(max_length=80, blank=True)            # shelf, bin, cabinet
    unit = models.CharField(max_length=20, default="each")
    qty = models.FloatField(default=0)                                  # on hand now (below zero: used more than was counted)
    min_qty = models.FloatField(null=True, blank=True)                 # reorder at or below this
    unit_cost = models.FloatField(null=True, blank=True)               # what one costs ($): repair costs use it
    notes = models.TextField(blank=True)
    active = models.BooleanField(default=True)
    updated = models.DateTimeField(null=True, blank=True)
    updated_by = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self):
        return f"{self.name} ({self.part_no})" if self.part_no else self.name

    @property
    def reserved(self):
        return sum(r.qty for r in self.reservations.all() if r.status == "reserved")

    @property
    def free(self):
        """On hand less what's reserved for requests."""
        return self.qty - self.reserved

    @property
    def low(self):
        return self.qty < 0 or (self.min_qty is not None and self.qty <= self.min_qty)


class InventoryMove(models.Model):
    """One change to an item's stock: used on a machine (for a request or not), added, or counted."""
    KINDS = [("used", "Used"), ("added", "Added"), ("count", "Counted"), ("undone", "Used, taken back"), ("back", "Taken back")]
    item = models.ForeignKey(InventoryItem, related_name="moves", on_delete=models.CASCADE)
    kind = models.CharField(max_length=8, choices=KINDS)
    qty = models.FloatField()                     # used / added: how many; count: the difference the count made
    machine = models.CharField(max_length=60, blank=True, db_index=True)
    asset = models.ForeignKey("maintenance.Asset", null=True, blank=True, related_name="parts_used", on_delete=models.SET_NULL)
    unit_cost = models.FloatField(null=True, blank=True)   # the item's cost when it was used (so later price changes don't rewrite it)
    ticket = models.ForeignKey("hmi.Ticket", null=True, blank=True, related_name="parts_used", on_delete=models.SET_NULL)
    note = models.CharField(max_length=200, blank=True)
    t = models.DateTimeField(db_index=True)
    by = models.CharField(max_length=120)

    class Meta:
        ordering = ["-t", "-id"]


class PMPart(models.Model):
    """A part a PM task uses each time (its parts list): what to have in stock before it falls due, and what using
    the request's parts records when the PM is done."""
    task = models.ForeignKey("hmi.PMTask", related_name="parts", on_delete=models.CASCADE)
    item = models.ForeignKey(InventoryItem, related_name="pm_uses", on_delete=models.CASCADE)
    qty = models.FloatField(default=1)

    class Meta:
        unique_together = [("task", "item")]
        ordering = ["item__name", "id"]


class Asset(models.Model):
    """Maintenance › Assets: the plant's equipment as a tree. A machine (or other equipment) at the top, its
    assemblies and components under it, and instruments (gauges, scales, thermometers) that are calibrated. Requests,
    PMs, parts used and replacements can name the asset, so each one has its own history."""
    KINDS = [("machine", "Machine / equipment"), ("assembly", "Assembly"), ("component", "Component"), ("instrument", "Instrument / gauge")]
    name = models.CharField(max_length=120)
    parent = models.ForeignKey("self", null=True, blank=True, related_name="children", on_delete=models.CASCADE)
    machine = models.CharField(max_length=60, db_index=True, help_text="The machine or equipment (as on requests) it belongs to")
    kind = models.CharField(max_length=12, choices=KINDS, default="component")
    tag = models.CharField(max_length=40, blank=True, help_text="Asset tag or number")
    make = models.CharField(max_length=80, blank=True)
    model = models.CharField(max_length=80, blank=True)
    serial = models.CharField(max_length=80, blank=True)
    installed = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    active = models.BooleanField(default=True)
    # calibration (instruments)
    cal_every = models.PositiveIntegerField(null=True, blank=True, help_text="Calibrate every so many days (blank: not calibrated)")
    cal_spec = models.CharField(max_length=160, blank=True, help_text="Range and tolerance, e.g. 0–300 mm ± 0.05")
    cal_last = models.DateField(null=True, blank=True)
    cal_status = models.CharField(max_length=10, blank=True, help_text="pass | adjusted | fail: the last calibration's result")
    updated = models.DateTimeField(null=True, blank=True)
    updated_by = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["machine", "name", "id"]

    def __str__(self):
        return self.name

    @property
    def cal_due(self):
        from datetime import timedelta
        if not self.cal_every:
            return None
        return (self.cal_last + timedelta(days=self.cal_every)) if self.cal_last else None

    def path(self):
        out, a, seen = [], self, set()
        while a and a.pk not in seen:
            seen.add(a.pk)
            out.append(a.name)
            a = a.parent
        return " › ".join(reversed(out))


class AssetEvent(models.Model):
    """Something that happened to an asset beyond its requests: installed, replaced, removed, rebuilt, or a note."""
    KINDS = [("installed", "Installed"), ("replaced", "Replaced"), ("removed", "Removed"), ("rebuilt", "Rebuilt"), ("note", "Note")]
    asset = models.ForeignKey(Asset, related_name="events", on_delete=models.CASCADE)
    kind = models.CharField(max_length=10, choices=KINDS, default="note")
    text = models.TextField(blank=True)
    ticket = models.ForeignKey("hmi.Ticket", null=True, blank=True, related_name="asset_events", on_delete=models.SET_NULL)
    t = models.DateTimeField(db_index=True)
    by = models.CharField(max_length=120)

    class Meta:
        ordering = ["-t", "-id"]


class Calibration(models.Model):
    """One calibration of an instrument: what it read before (as found) and after (as left), and the result."""
    RESULTS = [("pass", "Pass"), ("adjusted", "Adjusted"), ("fail", "Fail · taken out of use")]
    asset = models.ForeignKey(Asset, related_name="calibrations", on_delete=models.CASCADE)
    day = models.DateField()
    result = models.CharField(max_length=10, choices=RESULTS)
    as_found = models.CharField(max_length=200, blank=True)
    as_left = models.CharField(max_length=200, blank=True)
    standard = models.CharField(max_length=120, blank=True, help_text="Reference standard used (its ID or certificate)")
    cert = models.CharField(max_length=80, blank=True, help_text="Certificate number (an outside lab's)")
    notes = models.TextField(blank=True)
    t = models.DateTimeField()
    by = models.CharField(max_length=120)

    class Meta:
        ordering = ["-day", "-id"]


class Meter(models.Model):
    """A count a PM can fall due on, besides days, run hours and filters made: knife cycles, glue gun hours, a
    gauge read by hand. Read by hand (Maintenance › Assets › meter), or from a machine's data each minute."""
    SOURCES = [("manual", "Read by hand"), ("field", "From the machine's data (a field)")]
    machine = models.CharField(max_length=60, db_index=True)
    asset = models.ForeignKey(Asset, null=True, blank=True, related_name="meters", on_delete=models.SET_NULL)
    name = models.CharField(max_length=80)
    unit = models.CharField(max_length=20, default="cycles")
    source = models.CharField(max_length=8, choices=SOURCES, default="manual")
    field = models.CharField(max_length=80, blank=True, help_text="The machine data field holding the meter (a running total)")
    value = models.FloatField(default=0)
    read_at = models.DateTimeField(null=True, blank=True)
    read_by = models.CharField(max_length=120, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["machine", "name", "id"]

    def __str__(self):
        return f"{self.machine} · {self.name}"


class MeterReading(models.Model):
    meter = models.ForeignKey(Meter, related_name="readings", on_delete=models.CASCADE)
    value = models.FloatField()
    t = models.DateTimeField(db_index=True)
    by = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["-t", "-id"]


class Reservation(models.Model):
    """Parts set aside for a request (a PM's parts list reserves itself when its request opens): on hand minus
    reserved is what's free. Using the parts on the request, or completing it, ends the reservation."""
    STATUSES = [("reserved", "Reserved"), ("used", "Used"), ("released", "Released")]
    item = models.ForeignKey(InventoryItem, related_name="reservations", on_delete=models.CASCADE)
    ticket = models.ForeignKey("hmi.Ticket", related_name="reservations", on_delete=models.CASCADE)
    qty = models.FloatField()
    status = models.CharField(max_length=10, choices=STATUSES, default="reserved", db_index=True)
    t = models.DateTimeField()
    by = models.CharField(max_length=120)

    class Meta:
        ordering = ["t", "id"]


class TicketLabor(models.Model):
    """Hours someone worked on a request. Costed at the maintenance labor rate (Admin › Rules), not anyone's wage."""
    ticket = models.ForeignKey("hmi.Ticket", related_name="labor", on_delete=models.CASCADE)
    who = models.CharField(max_length=120)
    hours = models.FloatField()
    day = models.DateField()
    note = models.CharField(max_length=200, blank=True)
    t = models.DateTimeField()
    by = models.CharField(max_length=120)

    class Meta:
        ordering = ["day", "id"]


class MaintWindow(models.Model):
    """Planned maintenance time on a machine: the Scheduler plans no production in it, and the kiosk counts it as
    planned (not downtime). From a request or a PM, or on its own."""
    machine = models.ForeignKey("plant.Machine", related_name="maint_windows", on_delete=models.CASCADE)
    start = models.DateTimeField(db_index=True)
    end = models.DateTimeField()
    ticket = models.ForeignKey("hmi.Ticket", null=True, blank=True, related_name="windows", on_delete=models.SET_NULL)
    note = models.CharField(max_length=160, blank=True)
    cancelled = models.BooleanField(default=False)
    t = models.DateTimeField()
    by = models.CharField(max_length=120)

    class Meta:
        ordering = ["start", "id"]
