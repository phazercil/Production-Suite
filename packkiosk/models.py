"""Packaging Kiosk › Pallet tags: one row per finished pallet, the tag printed for it and who signed it.

The person logged in on the screen packs the pallet and makes the tag. A second person can verify it, and a
shift lead can sign it as supervisor, each with their own PIN on the screen. A printed tag shows the names of
whoever signed it in the suite, and a blank line to sign by hand for anyone who hasn't yet."""
from django.db import models


class PalletTag(models.Model):
    part = models.CharField(max_length=40, db_index=True)
    description = models.CharField(max_length=80, blank=True, help_text="Brand or media, e.g. MicroGard")
    mo = models.CharField(max_length=40, blank=True, db_index=True, help_text="The MO packed (blank = none)")
    line = models.CharField(max_length=60, help_text="The manufacturing line the pallet came off")
    pallet_no = models.IntegerField()
    qty = models.IntegerField()
    t = models.DateTimeField(db_index=True)
    packed_by = models.CharField(max_length=120)
    packed_by_id = models.IntegerField(null=True, blank=True)
    verified_by = models.CharField(max_length=120, blank=True)
    verified_t = models.DateTimeField(null=True, blank=True)
    supervisor = models.CharField(max_length=120, blank=True)
    supervisor_t = models.DateTimeField(null=True, blank=True)
    prints = models.IntegerField(default=0)
    printed_t = models.DateTimeField(null=True, blank=True)
    voided = models.BooleanField(default=False)
    voided_by = models.CharField(max_length=120, blank=True)
    void_reason = models.CharField(max_length=200, blank=True)
    updated = models.DateTimeField(null=True, blank=True)
    updated_by = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["-t", "-id"]
        db_table = "pack_pallet_tag"

    def __str__(self):
        return f"{self.part} pallet {self.pallet_no}"

    @property
    def tag_no(self):
        return f"PT{self.pk:06d}" if self.pk else ""
