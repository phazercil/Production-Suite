"""Machine speed units (Admin › Machines › Speed unit).

A machine reports its speed in one of:
- "ppm": pleats per minute (pleaters): one part = the part's pleat count;
- "mms": linear millimetres per second of media: one part = its mm of media, from the machine's "mm per part"
  setting or the part's spec (pleats × 2 × pleat height, the media one pleat pack takes);
- "pcs": parts per minute: one part = 1.
per_part() gives the speed-minutes one part takes, so parts per minute = speed / per_part(): it turns speed into
counts (machines without a counter), the run rate and the Scheduler's hourly rate.
"""
UNITS = {"ppm": ("Pleats per minute", "P/m"), "mms": ("Linear mm per second", "mm/s"), "pcs": ("Parts per minute", "parts/min")}
MM_PER_INCH = 25.4


def unit(m):
    u = getattr(m, "speed_unit", None) if not isinstance(m, dict) else m.get("unit")
    return u if u in UNITS else "ppm"


def short(m):
    return UNITS[unit(m)][1]


def name(m):
    return UNITS[unit(m)][0]


def mm_per_part(m, spec):
    fixed = m.get("mmPerPart") if isinstance(m, dict) else getattr(m, "mm_per_part", None)
    if fixed:
        return float(fixed)
    pleats, ph = float((spec or {}).get("pleats") or 0), float((spec or {}).get("ph") or 0)
    return pleats * 2 * ph * MM_PER_INCH if pleats and ph else 0.0


def per_part(m, spec, pleats=None):
    """Speed-minutes one part takes (0 when it can't be told, e.g. no pleat height for an mm/s machine)."""
    u = unit(m)
    if u == "pcs":
        return 1.0
    if u == "mms":
        return mm_per_part(m, spec) / 60.0   # mm/s × 60 = mm per minute
    p = float(pleats or (spec or {}).get("pleats") or 0)
    return p or 24.0


def basis(m, spec, pleats=None):
    """How a part's rate comes from the speed, in words (Scheduler tooltips)."""
    u = unit(m)
    if u == "pcs":
        return "parts per minute × 60"
    if u == "mms":
        mm = mm_per_part(m, spec)
        return f"mm/s × 3600 ÷ {mm:,.0f} mm per part" if mm else "no mm per part (set it in Admin › Machines, or the part's pleat height)"
    return f"P/m × 60 ÷ {float(pleats or (spec or {}).get('pleats') or 0):,.0f} pleats"
