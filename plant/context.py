# The sections of the suite, in the top bar (key, address, name)
MODES = [("operator", "/operator/", "Operator Kiosk"), ("packaging", "/packaging/", "Packaging Kiosk"), ("stations", "/stations/", "Stations"), ("schedule", "/schedule/", "Schedule"), ("scheduler", "/scheduler/", "Scheduler"), ("data", "/data/", "Data Kiosk"),
         ("reports", "/reports/", "Reports"), ("labor", "/labor/", "Labor Budget"), ("supervisor", "/supervisor/", "Supervisor"),
         ("quality", "/quality/", "Quality"),
         ("maintenance", "/maintenance/", "Maintenance"), ("admin", "/admin/", "Admin")]


# who may open each mode (Admin › Permissions); modes without one are open to everyone on the plant network
MODE_PERM = {"packaging": "section.packaging", "stations": "section.stations", "supervisor": "section.supervisor", "quality": "section.quality",
             "maintenance": "section.maintenance", "admin": "section.admin", "labor": "section.labor"}
MORE = {"schedule", "reports", "labor", "admin"}   # back-office modes (the Labor page's own bar still uses MODES and this)

# The top bar by workflow: a group with one mode is a link, with more a menu (key, name, modes in it)
GROUPS = [("production", "Production", ["operator", "packaging", "stations"]),
          ("planning", "Planning", ["scheduler", "schedule"]),
          ("oversight", "Oversight", ["supervisor", "reports", "data", "labor"]),
          ("quality", "Quality", ["quality"]),
          ("maintenance", "Maintenance", ["maintenance"]),
          ("admin", "Admin", ["admin"])]


def groups_for(modes, current=""):
    """The top bar's groups for the modes this person may open: [{key, label, items: [(key, href, label)], on}].
    Groups with nothing to open are left out; a mode in no group gets its own entry, so nothing disappears."""
    have = {m[0]: m for m in modes}
    out, placed = [], set()
    for key, label, keys in GROUPS:
        items = [have[k] for k in keys if k in have]
        placed.update(k for k in keys if k in have)
        if items:
            out.append({"key": key, "label": label, "items": items, "on": any(k == current for k, _, _ in items)})
    for m in modes:
        if m[0] not in placed:
            out.append({"key": m[0], "label": m[2], "items": [m], "on": m[0] == current})
    return out


def _health_warning(me):
    """The bar's warning for people who look after the system: machine data isn't being read (the minute ticker has
    stopped). Read from the last time the ticker reported in; nothing is shown to everyone else."""
    from plant import perms
    if not me or not (perms.can(me, "section.admin") or perms.can(me, "section.supervisor")):
        return None
    try:
        from django.utils import timezone

        from plant.models import PlantSettings
        seen = PlantSettings.objects.filter(pk=1).values_list("ticker_seen", flat=True).first()
    except Exception:  # noqa: BLE001 - before the first migration
        return None
    if seen is None:
        return {"text": "Machine data isn't being read: the minute ticker hasn't run yet.", "href": "/admin/health/"}
    mins = (timezone.now() - seen).total_seconds() / 60
    if mins >= 3:
        return {"text": f"Machine data isn't being read: the minute ticker last ran {int(mins)} minutes ago.", "href": "/admin/health/"}
    return None


def _me(request):
    """The person logged in on this screen, read only (the idle logout is the pages' own job)."""
    try:
        pid = request.session.get("pid")
        if not pid:
            return None
        from plant.models import Person
        return Person.objects.filter(pk=pid, active=True).first()
    except Exception:  # noqa: BLE001
        return None


def _pin_lens():
    """The PIN keypad sends by itself once a PIN is as long as everyone's: only when every PIN is the same length."""
    from django.core.cache import cache
    try:
        v = cache.get("pin_lens")
        if v is None:
            from plant.models import Person
            lens = sorted({len(p) for p in Person.objects.filter(active=True).exclude(pin="").values_list("pin", flat=True) if p.isdigit()})
            v = str(lens[0]) if len(lens) == 1 else ""
            cache.set("pin_lens", v, 60)
        return v
    except Exception:  # noqa: BLE001
        return ""


def suite(request):
    """Shell context: which mode is open and whether the screen is locked to one mode (?kiosk=1)."""
    kiosk = request.GET.get("kiosk")
    if kiosk is not None:
        request.session["kiosk_lock"] = kiosk not in ("0", "")
    from django.conf import settings
    try:
        from suite.thin import sse_on
        sse = sse_on()
    except Exception:  # noqa: BLE001 - before the first migration
        sse = False
    me = _me(request)
    modes = MODES
    if me:   # role-aware: someone signed in sees the modes they can open
        from plant import perms
        modes = [m for m in MODES if m[0] not in MODE_PERM or MODE_PERM[m[0]] not in perms.PERMS or perms.can(me, MODE_PERM[m[0]])]
    role = ""
    if me:
        from plant.perms import ROLES
        role = dict(ROLES).get(me.role, me.role)
    from django.utils import timezone
    path = request.path or "/"
    current = next((k for k, href, _ in MODES if path.startswith(href)), "")
    return {"suite_groups": groups_for(modes, current), "suite_warn": _health_warning(me),
            "suite_now": int(timezone.now().timestamp() * 1000), "suite_tz": settings.TIME_ZONE,
            "suite_pin_lens": _pin_lens(), "suite_sse": sse, "kiosk_lock": request.session.get("kiosk_lock", False), "suite_modes": modes,
            "suite_main": [m for m in modes if m[0] not in MORE], "suite_more": [m for m in modes if m[0] in MORE],
            "suite_me": me, "suite_role": role,
            "suite_rev": f"v{settings.SUITE['VERSION']} · rev {settings.SUITE['REVISION']}", "suite_revision": settings.SUITE["REVISION"], "suite_version": settings.SUITE["VERSION"]}
