"""Pictures on maintenance tickets: what is wrong, taken with the tablet or phone camera.

The screen sends the picture as the camera took it. The server checks it is a picture, turns it upright, drops
the camera's metadata and keeps a 1600 px JPEG (a few hundred KB) plus a small thumbnail for lists. Pictures are kept in the records
database with the ticket, so backups include them; closed tickets' pictures are removed after the days set in
Admin > Rules (photoDays, 0 = keep).
"""
import io
from datetime import timedelta

MAX_BYTES = 25 * 1024 * 1024          # a picture as the tablet's camera took it
MAX_PER_TICKET = 6
MAX_SIDE = 1600                        # the copy that is kept
THUMB_SIDE = 320                       # the copy shown in lists
MAX_PIXELS = 60_000_000                # refuse "decompression bomb" files before decoding them


class PhotoError(Exception):
    pass


def process(data):
    """The kept copy and a thumbnail of an uploaded picture, both JPEG, turned upright per the camera's EXIF
    orientation, with the camera's metadata (GPS position, serial numbers) left out.
    Returns (jpeg, thumb_jpeg, width, height)."""
    import warnings

    from PIL import Image, ImageOps, UnidentifiedImageError
    if not data:
        raise PhotoError("The picture is empty.")
    if len(data) > MAX_BYTES:
        raise PhotoError("That picture is too large (over 25 MB).")
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            im = Image.open(io.BytesIO(data))
            if im.format not in ("JPEG", "PNG", "WEBP", "HEIF", "MPO", "GIF", "BMP", "TIFF"):
                raise PhotoError("Send a picture (JPEG, PNG or WebP).")
            im.load()
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise PhotoError("That file isn't a picture the server can read.")
    im = ImageOps.exif_transpose(im)
    if im.mode not in ("RGB", "L"):
        bg = Image.new("RGB", im.size, (255, 255, 255))
        im = im.convert("RGBA")
        bg.paste(im, mask=im.split()[-1])
        im = bg
    im = im.convert("RGB")
    im.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=82, optimize=True)
    th = im.copy()
    th.thumbnail((THUMB_SIDE, THUMB_SIDE), Image.LANCZOS)
    tout = io.BytesIO()
    th.save(tout, "JPEG", quality=75, optimize=True)
    return out.getvalue(), tout.getvalue(), im.width, im.height


def add(ticket, data, by, when):
    from .models import TicketPhoto
    if ticket.photos.count() >= MAX_PER_TICKET:
        raise PhotoError(f"A ticket can have up to {MAX_PER_TICKET} pictures.")
    jpeg, thumb, w, h = process(data)
    return TicketPhoto.objects.create(ticket=ticket, t=when, by=str(by)[:120], content_type="image/jpeg", data=jpeg, thumb=thumb,
                                      size=len(jpeg), width=w, height=h)


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
