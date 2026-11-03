"""Pictures on maintenance tickets: what is wrong, taken with the tablet or phone camera.

The screen shrinks each picture to at most 1600 px and re-saves it as JPEG before sending (a few hundred KB),
so the server only has to check it really is a picture of sensible size. Pictures are kept in the records
database with the ticket, so backups include them; closed tickets' pictures are removed after the days set in
Admin > Rules (photoDays, 0 = keep).
"""
import struct
from datetime import timedelta

MAX_BYTES = 3 * 1024 * 1024
MAX_PER_TICKET = 6
MAX_SIDE = 8000


class PhotoError(Exception):
    pass


def _jpeg_size(b):
    i = 2
    while i + 9 < len(b):
        if b[i] != 0xFF:
            i += 1
            continue
        marker = b[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg = struct.unpack(">H", b[i + 2:i + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", b[i + 5:i + 9])
            return w, h
        i += 2 + seg
    return None


def check(data):
    """(content type, width, height) for a JPEG or PNG picture; PhotoError for anything else."""
    if not data:
        raise PhotoError("The picture is empty.")
    if len(data) > MAX_BYTES:
        raise PhotoError("That picture is too large (over 3 MB).")
    if data[:3] == b"\xff\xd8\xff":
        size, ctype = _jpeg_size(data), "image/jpeg"
    elif data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        size, ctype = struct.unpack(">II", data[16:24]), "image/png"
    else:
        raise PhotoError("Send a JPEG or PNG picture.")
    if not size or not (0 < size[0] <= MAX_SIDE and 0 < size[1] <= MAX_SIDE):
        raise PhotoError("That picture couldn't be read.")
    return ctype, size[0], size[1]


def add(ticket, data, by, when):
    from .models import TicketPhoto
    ctype, w, h = check(data)
    if ticket.photos.count() >= MAX_PER_TICKET:
        raise PhotoError(f"A ticket can have up to {MAX_PER_TICKET} pictures.")
    return TicketPhoto.objects.create(ticket=ticket, t=when, by=str(by)[:120], content_type=ctype, data=bytes(data),
                                      size=len(data), width=w, height=h)


def purge(now, days):
    """Remove pictures of tickets closed more than `days` days ago. Returns how many were removed."""
    from django.db.models import Q

    from .models import TicketPhoto
    if not days or days <= 0:
        return 0
    cut = now - timedelta(days=days)
    n, _ = (TicketPhoto.objects.filter(ticket__status="Closed")
            .filter(Q(ticket__updated__lt=cut) | Q(ticket__updated__isnull=True, ticket__t__lt=cut)).delete())
    return n
