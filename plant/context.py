# The sections of the suite, in the top bar (key, address, name)
MODES = [("operator", "/operator/", "Operator Kiosk"), ("scheduler", "/scheduler/", "Scheduler"), ("data", "/data/", "Data Kiosk"),
         ("reports", "/reports/", "Reports"), ("labor", "/labor/", "Labor Budget"), ("supervisor", "/supervisor/", "Supervisor"),
         ("quality", "/quality/", "Quality"),
         ("maintenance", "/maintenance/", "Maintenance"), ("admin", "/admin/", "Admin")]


def suite(request):
    """Shell context: which mode is open and whether the screen is locked to one mode (?kiosk=1)."""
    kiosk = request.GET.get("kiosk")
    if kiosk is not None:
        request.session["kiosk_lock"] = kiosk not in ("0", "")
    from django.conf import settings
    return {"kiosk_lock": request.session.get("kiosk_lock", False), "suite_modes": MODES,
            "suite_rev": f"v{settings.SUITE['VERSION']} · rev {settings.SUITE['REVISION']}", "suite_revision": settings.SUITE["REVISION"], "suite_version": settings.SUITE["VERSION"]}
