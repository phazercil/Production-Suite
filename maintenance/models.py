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
    notes = models.TextField(blank=True)
    active = models.BooleanField(default=True)
    updated = models.DateTimeField(null=True, blank=True)
    updated_by = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self):
        return f"{self.name} ({self.part_no})" if self.part_no else self.name

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
