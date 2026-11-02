def suite(request):
    """Shell context: which mode is open and whether the screen is locked to one mode (?kiosk=1)."""
    kiosk = request.GET.get("kiosk")
    if kiosk is not None:
        request.session["kiosk_lock"] = kiosk not in ("0", "")
    from django.conf import settings
    return {"kiosk_lock": request.session.get("kiosk_lock", False),
            "suite_rev": f"v{settings.SUITE['VERSION']} · rev {settings.SUITE['REVISION']}", "suite_revision": settings.SUITE["REVISION"]}
