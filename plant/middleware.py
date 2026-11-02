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


class PrivateNetworkOnlyMiddleware:
    """Refuse every request that doesn't come from the plant network (Admin > Network), before anything else runs.
    Uses the socket's own address (REMOTE_ADDR); forwarded-for headers are ignored because a
    client can write anything in them."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from .netsafety import is_allowed_ip
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
        resp.setdefault("Content-Security-Policy",
                        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                        f"font-src 'self'; connect-src 'self'; frame-src {frame}; frame-ancestors 'self'; "
                        "form-action 'self'; base-uri 'self'; object-src 'none'")
        resp.setdefault("X-Content-Type-Options", "nosniff")
        resp.setdefault("Referrer-Policy", "same-origin")
        resp.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        return resp
