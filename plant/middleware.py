from django.conf import settings
from django.http import HttpResponse, JsonResponse


class StationKeyMiddleware:
    """When STATION_KEY is set, a screen must present it once (…/?key=KEY); it is then remembered in a cookie.
    Machine feed pushes use X-Feed-Key instead and the Django admin uses its own login."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        key = settings.SUITE["STATION_KEY"]
        path = request.path
        if not key or path.startswith(("/admin", "/static", "/api/feed/", "/api/ingest/", "/api/version")):
            return self.get_response(request)
        given = request.GET.get("key") or request.COOKIES.get("station_key") or request.headers.get("X-Station-Key")
        if given != key:
            if path.startswith("/api/"):
                return JsonResponse({"ok": False, "keyRequired": True, "error": "This screen needs the station key."}, status=401)
            return HttpResponse(
                "<!doctype html><meta name=viewport content='width=device-width'><title>Station key</title>"
                "<body style='font:16px system-ui;background:#1B1F24;color:#E6E8EB;padding:40px'>"
                "<h1 style='font-weight:600'>Station key needed</h1><p>Open this page once as <code>…/?key=YOUR_KEY</code> "
                "using the STATION_KEY from the server's .env. The screen remembers it.</p></body>", status=401)
        resp = self.get_response(request)
        if request.GET.get("key"):
            resp.set_cookie("station_key", key, max_age=60 * 60 * 24 * 365, samesite="Lax")
        return resp


def forwarded_client(meta):
    """Behind the suite's own Apache (BEHIND_PROXY=1, Linux install), the address of the device that made the
    request. Trusted only when the connection itself comes from this server (Apache proxies from 127.0.0.1;
    the suite listens on 127.0.0.1 only), and only Apache's own value: Apache drops any X-Forwarded-For a
    client sends and adds the real address, so the last entry is the one Apache wrote. Returns None when
    the request didn't come through the proxy."""
    import ipaddress
    if not settings.SUITE.get("BEHIND_PROXY"):
        return None
    peer = meta.get("REMOTE_ADDR", "")
    try:
        if not ipaddress.ip_address(peer).is_loopback:
            return None
    except ValueError:
        return None
    fwd = (meta.get("HTTP_X_FORWARDED_FOR") or "").split(",")[-1].strip()
    try:
        return str(ipaddress.ip_address(fwd)) if fwd else None
    except ValueError:
        return "invalid"


class PrivateNetworkOnlyMiddleware:
    """Refuse every request that doesn't come from the plant network (Admin > Network), before anything else runs.
    Uses the socket's own address (REMOTE_ADDR). Forwarded-for headers are ignored because a client can write
    anything in them, except behind the suite's own Apache on Linux (see forwarded_client), where the address
    Apache saw replaces REMOTE_ADDR for everything after this point (PIN limits, the audit log, Admin > Network)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from .netsafety import is_allowed_ip
        fwd = forwarded_client(request.META)
        if fwd:
            request.META["SUITE_PROXY_PEER"] = request.META.get("REMOTE_ADDR", "")
            request.META["REMOTE_ADDR"] = fwd
        addr = request.META.get("REMOTE_ADDR", "")
        if not is_allowed_ip(addr):
            import logging
            logging.getLogger("plant").warning("Refused a connection from %s (not on the plant network): %s", addr, request.path[:200])
            return HttpResponse(f"Production Suite only answers devices on the plant network. This device ({addr[:60] or 'unknown'}) "
                                "is not on it. A shift lead can add its range from a plant screen in Operator Kiosk > Admin > Network.",
                                status=403, content_type="text/plain; charset=utf-8")
        return self.get_response(request)


class SecurityHeadersMiddleware:
    """Tell browsers to load nothing from outside the suite: no internet fonts, scripts, images or
    connections. The only other site a page may show is the plant's Grafana server (Data Kiosk panels)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        resp = self.get_response(request)
        frame = "'none'"
        try:
            from urllib.parse import urlsplit

            from .models import PlantSettings
            from .netsafety import NotPrivate, check_url
            g = (PlantSettings.objects.filter(pk=1).values_list("settings", flat=True).first() or {}).get("grafana") or {}
            if g.get("on") and g.get("url"):
                try:
                    u = urlsplit(check_url(g["url"], "Grafana"))
                    frame = f"{u.scheme}://{u.netloc}"
                except NotPrivate:
                    frame = "'none'"
        except Exception:
            frame = "'none'"
        # frames: the suite's own pages (the 3D viewer in Quality and the kiosk's 3D Model) and Grafana when set up
        frame = "'self'" + ("" if frame == "'none'" else " " + frame)
        resp.setdefault("Content-Security-Policy",
                        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                        f"font-src 'self'; connect-src 'self'; frame-src {frame}; frame-ancestors 'self'; "
                        "form-action 'self'; base-uri 'self'; object-src 'none'")
        resp.setdefault("X-Content-Type-Options", "nosniff")
        resp.setdefault("Referrer-Policy", "same-origin")
        resp.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        return resp
