"""Guard for JSON endpoints: an unexpected error is logged with its traceback and the screen gets a plain
message it can show, never an HTML error page or a half-applied change (each view runs its own transaction)."""
import functools
import logging

from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.http import JsonResponse

log = logging.getLogger("plant")


class BadRequest(Exception):
    """Input the server can't use. The message is shown to the person as-is."""


def json_api(view):
    @functools.wraps(view)
    def wrap(request, *a, **kw):
        try:
            return view(request, *a, **kw)
        except BadRequest as e:
            return JsonResponse({"ok": False, "error": str(e)}, status=400)
        except RequestDataTooBig:
            mb = settings.DATA_UPLOAD_MAX_MEMORY_SIZE // (1024 * 1024)
            return JsonResponse({"ok": False, "error": f"That is too large to send to the server (over {mb} MB). Nothing was changed."}, status=400)
        except Exception:
            log.exception("API error on %s", request.path)
            return JsonResponse({"ok": False, "error": "The server hit an error and nothing was changed. Try again; if it keeps happening, "
                                 "tell the shift lead (details are in the server log)."}, status=500)
    return wrap


def to_int(v, what):
    try:
        return int(v)
    except (TypeError, ValueError):
        raise BadRequest(f"Missing or invalid {what}.")
