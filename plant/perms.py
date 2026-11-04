"""Permissions: what each role, and each person, may do (Admin › Permissions).

Every feature that needs more than a login checks one permission here. A role gets a permission by default
(DEFAULTS); Admin › Permissions can turn it on or off for a role, and allow or deny it for one person, which
wins over their role. The Admin role always keeps "Manage permissions", so nobody can lock the plant out.
Stored in PlantSettings.config["perms"]: {"roles": {perm: [roles]}, "people": {person id: {perm: true|false}}}.
"""
from collections import OrderedDict

ROLES = [("operator", "Operator"), ("maintenance", "Maintenance"), ("lead", "Shift lead"), ("admin", "Admin")]
SUP = ["maintenance", "lead", "admin"]
LEAD = ["lead", "admin"]
ADMIN = ["admin"]

# key, label, what it covers, roles that have it by default
PERMS = OrderedDict((k, {"key": k, "group": g, "label": l, "help": h, "roles": r}) for g, items in (
    ("Floor", [
        ("floor.override", "Supervisor override", "Take over a screen with the Supervisor button to change finished entries.", SUP),
        ("downtime.reason", "Any downtime reason", "Type a reason that isn't on the list, or change a reason someone else picked.", SUP),
        ("qc.edit", "Edit finished QC checks", "Change a QC check after it was saved.", SUP),
        ("qc.approve", "Approve out-of-spec QC", "Approve or disapprove a QC check that failed, and scrap filters from the count.", SUP),
        ("runs.override", "Override a running shift", "Change the speed or count settings of a shift that is running.", LEAD),
        ("tickets.work", "Work maintenance requests", "Add repair notes and close maintenance requests.", SUP),
    ]),
    ("People", [
        ("team.manage", "Manage the team", "Add people and change names, PINs, roles (up to shift lead), training and status.", LEAD),
        ("team.import", "Import people", "Run the Paycom import and the people-list pull.", LEAD),
    ]),
    ("Planning", [
        ("schedule.edit", "Change the schedule", "Move, add and change MOs, lines and shifts in the Scheduler; pull MOs from S2K.", LEAD),
        ("labor.view", "Open the Labor Budget", "See the Labor Budget, including wages.", LEAD),
        ("labor.edit", "Change the Labor Budget", "Save changes to the Labor Budget.", LEAD),
        ("costs.view", "See labor costs", "Labor cost per run in the Scheduler and Reports.", LEAD),
    ]),
    ("Admin", [
        ("lists.edit", "Day-to-day lists", "Downtime reasons, checklists, maintenance lists, QC checks, shifts & breaks, parts & specs, rules.", LEAD),
        ("machines.edit", "Machines", "Add, rename and remove machines; their data sources.", ADMIN),
        ("connections.edit", "Connections", "Databases, accounts, Grafana, S2K, Paycom, incoming data, changeover data queries.", ADMIN),
        ("alerts.edit", "Alerts", "Who is told when a line is down; mail server and Twilio.", ADMIN),
        ("network.edit", "Network", "The plant network and the firewall.", ADMIN),
        ("updates.install", "Updates", "Install updates, go back, updates from GitHub.", ADMIN),
        ("backup.manage", "Backup", "Copy settings, restore defaults, clear data.", ADMIN),
        ("permissions.manage", "Manage permissions", "This page. The Admin role always has it.", ADMIN),
    ]),
    ("Sections", [
        ("section.admin", "Open Admin", "The Admin section (what can be changed there follows the permissions above).", LEAD),
        ("section.supervisor", "Open Supervisor", "The Supervisor section.", LEAD),
        ("section.maintenance", "Open Maintenance", "The Maintenance section.", SUP),
    ]),
) for k, l, h, r in items)

# Admin tabs and the permission that changes them
TAB_PERM = {"machines": "machines.edit", "connections": "connections.edit", "codata": "connections.edit", "shifts": "lists.edit",
            "reasons": "lists.edit", "checklists": "lists.edit", "maintenance": "lists.edit", "qc": "lists.edit", "parts": "lists.edit",
            "rules": "lists.edit", "alerts": "alerts.edit", "network": "network.edit", "updates": "updates.install",
            "backup": "backup.manage", "permissions": "permissions.manage"}
LOCKED = {("permissions.manage", "admin")}   # (permission, role) that can't be turned off


def _cfg():
    from plant.models import PlantSettings
    return (PlantSettings.load().config or {}).get("perms") or {}


def role_has(key, role, cfg=None):
    if (key, role) in LOCKED:
        return True
    cfg = _cfg() if cfg is None else cfg
    roles = (cfg.get("roles") or {}).get(key)
    return role in (roles if isinstance(roles, list) else PERMS[key]["roles"])


def person_rule(key, person_id, cfg=None):
    """True (allowed for this person), False (denied for this person) or None (follows their role)."""
    cfg = _cfg() if cfg is None else cfg
    v = ((cfg.get("people") or {}).get(str(person_id)) or {}).get(key)
    return v if isinstance(v, bool) else None


def can(person, key, cfg=None):
    if not person or key not in PERMS or not getattr(person, "active", True):
        return False
    if getattr(person, "system", False):   # the server's own desktop tool (run by whoever administers the server)
        return True
    cfg = _cfg() if cfg is None else cfg
    if (key, getattr(person, "role", "")) in LOCKED:
        return True
    rule = person_rule(key, getattr(person, "pk", None), cfg)
    return rule if rule is not None else role_has(key, getattr(person, "role", ""), cfg)


def all_for(person):
    """{perm key: allowed} for one person (one settings read)."""
    cfg = _cfg()
    return {k: can(person, k, cfg) for k in PERMS}


def who_can(key, people=None):
    from plant.models import Person
    cfg = _cfg()
    people = people if people is not None else Person.objects.filter(active=True)
    return [p for p in people if can(p, key, cfg)]


class PermView(dict):
    """For templates: S.perms.lists_edit, S.perms.floor_override ..."""
    def __init__(self, person):
        super().__init__({k.replace(".", "_"): v for k, v in all_for(person).items()} if person else {})

    def __missing__(self, k):
        return False
