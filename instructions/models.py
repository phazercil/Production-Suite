"""Digital work instructions (Quality › Work instructions): machine- and part-specific setup, changeover and
operating instructions with photos, videos and approved process parameters, under revision control.

Changeover and Inkjet change instructions are also the kiosk's changeover checklist: the approved one for the machine and
the part being changed to gives the steps operators tick off and the settings shown above them.

An Instruction is the document (WI-001 …). Each change is a Revision: written as a draft, approved by someone with
"Approve work instructions", and then it is the one the kiosks show; the revision it replaces becomes obsolete
(kept, read-only). Operators acknowledge each approved revision they're shown (Ack)."""
from django.db import models


class Instruction(models.Model):
    KINDS = [("setup", "Setup"), ("changeover", "Changeover"), ("inkjet", "Inkjet change"), ("operation", "Operation"), ("quality", "Quality check"), ("safety", "Safety"),
             ("maintenance", "Maintenance")]
    doc_no = models.CharField(max_length=20, unique=True)
    title = models.CharField(max_length=160)
    kind = models.CharField(max_length=12, choices=KINDS, default="setup")
    machines = models.ManyToManyField("plant.Machine", blank=True, related_name="instructions", help_text="None: every machine")
    parts = models.CharField(max_length=400, blank=True, help_text="Part numbers it's for, comma-separated; * matches anything (MG4062*, AQ12*). Blank: every part")
    active = models.BooleanField(default=True)
    created = models.DateTimeField()
    created_by = models.CharField(max_length=120)

    class Meta:
        ordering = ["doc_no"]

    def __str__(self):
        return f"{self.doc_no} {self.title}"

    def current(self):
        """The approved revision: the one the kiosks show."""
        return self.revisions.filter(status="approved").order_by("-rev").first()

    def draft(self):
        return self.revisions.filter(status="draft").order_by("-rev").first()

    def part_patterns(self):
        return [p.strip().upper() for p in self.parts.split(",") if p.strip()]


class Revision(models.Model):
    STATUSES = [("draft", "Draft"), ("approved", "Approved"), ("obsolete", "Obsolete")]
    instruction = models.ForeignKey(Instruction, related_name="revisions", on_delete=models.CASCADE)
    rev = models.PositiveIntegerField()
    status = models.CharField(max_length=10, choices=STATUSES, default="draft", db_index=True)
    steps = models.JSONField(default=list, blank=True, help_text="[{text, caution, media: [media ids]}]")
    params = models.JSONField(default=list, blank=True, help_text="[{name, target, min, max, unit}]: approved process parameters")
    change_note = models.TextField(blank=True, help_text="What changed and why")
    author = models.CharField(max_length=120)
    created = models.DateTimeField()
    updated = models.DateTimeField()
    approved_by = models.CharField(max_length=120, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    obsolete_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["instruction", "-rev"]
        unique_together = [("instruction", "rev")]

    @property
    def label(self):
        return f"{self.instruction.doc_no} rev {self.rev}"


class Media(models.Model):
    """A photo or video for an instruction's steps. Never changed once added (a revision that drops it leaves it for
    the revisions that still use it). Photos are kept in the database (backed up with it); videos are files under
    media/instructions/ in the suite's folder."""
    instruction = models.ForeignKey(Instruction, related_name="media", on_delete=models.CASCADE)
    kind = models.CharField(max_length=6, choices=[("image", "Photo"), ("video", "Video")])
    content_type = models.CharField(max_length=60)
    data = models.BinaryField(default=b"", blank=True)
    thumb = models.BinaryField(default=b"", blank=True)
    file = models.CharField(max_length=200, blank=True, help_text="Videos: the file's name under media/instructions/")
    name = models.CharField(max_length=200, blank=True)
    size = models.BigIntegerField(default=0)
    t = models.DateTimeField()
    by = models.CharField(max_length=120)

    class Meta:
        ordering = ["t", "id"]


class Ack(models.Model):
    """Someone read an approved revision (Read and understood, on the kiosk)."""
    revision = models.ForeignKey(Revision, related_name="acks", on_delete=models.CASCADE)
    person = models.ForeignKey("plant.Person", null=True, blank=True, on_delete=models.SET_NULL)
    who = models.CharField(max_length=120)
    machine = models.CharField(max_length=60, blank=True)
    t = models.DateTimeField()

    class Meta:
        unique_together = [("revision", "who")]
        ordering = ["-t"]
