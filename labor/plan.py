"""What the Scheduler takes from the Labor Budget plan.

- Work calendar: a day the plan marks HOLIDAY or OFF has no production; a day it marks WORK runs the line's
  shifts even if that weekday is normally off. Other days follow the plan's normal work week and the shift's days.
- Crew per line and shift: a suite line is linked to the labor-plan line with the same name. A shift the plan
  doesn't staff on that line (shift turned off, or no positions) is not used for runs on that line.
  Suite shifts are matched to the plan's shift numbers by their order on the Lines & Shifts page (1st, 2nd, 3rd).
- Labor cost of a run: crew wages per hour (each slot's wage, or the position's default wage) times the shift
  hours the run uses, changeover included. Straight-time base wages only: no overtime, cost-of-living raise or benefits.
"""
from datetime import date

from plant.util import from_ms, local


def _num(v, default=0.0):
    try:
        f = float(v)
        return f if f == f and abs(f) < 1e9 else default
    except (TypeError, ValueError):
        return default


class LaborPlanView:
    def __init__(self, data, version=0):
        self.data = data if isinstance(data, dict) else {}
        self.version = version
        cal = self.data.get("calendar") if isinstance(self.data.get("calendar"), dict) else {}
        self.overrides = {str(k): str(v).upper() for k, v in (cal.get("overrides") or {}).items()} if isinstance(cal.get("overrides"), dict) else {}
        wd = cal.get("defaultWorkDays")
        self.work_days = {int(x) for x in wd if str(x).lstrip("-").isdigit()} if isinstance(wd, list) else {1, 2, 3, 4, 5}
        self.lines = {}
        for ln in self.data.get("lines") or []:
            if isinstance(ln, dict) and str(ln.get("name") or "").strip():
                self.lines[str(ln["name"]).strip().lower()] = ln
        self.default_wage = {}
        for p in self.data.get("positionsDefaults") or []:
            if isinstance(p, dict) and p.get("title"):
                self.default_wage[str(p["title"]).strip().lower()] = _num(p.get("defaultWage"))
        slots = self.data.get("slotAssignments")
        self.slots = slots if isinstance(slots, dict) else {}
        self._shift_cache = {}

    @classmethod
    def current(cls):
        from .models import LaborPlan
        row = LaborPlan.objects.filter(pk=1).values("data", "version").first()
        if not row or not row["version"]:
            return None
        from .sync import merge
        return cls(merge(row["data"])[0], row["version"])   # with the suite's lines, shifts and work week

    # ---- calendar
    def day_status(self, d):
        """('WORK'|'OFF'|'HOLIDAY', set_by_date) for a date."""
        key = d.isoformat()
        if key in self.overrides and self.overrides[key] in ("WORK", "OFF", "HOLIDAY"):
            return self.overrides[key], True
        dow = (d.weekday() + 1) % 7  # 0 = Sunday, as the plan stores it
        return ("WORK" if dow in self.work_days else "OFF"), False

    def day_status_ms(self, day_ms):
        return self.day_status(local(from_ms(day_ms)).date())

    def special_days(self, a_ms, b_ms):
        """Holidays and date-specific changes between two times, for the board."""
        a, b = local(from_ms(a_ms)).date(), local(from_ms(b_ms)).date()
        out = {}
        for k, v in self.overrides.items():
            try:
                d = date.fromisoformat(k)
            except ValueError:
                continue
            if a <= d <= b and v in ("WORK", "OFF", "HOLIDAY"):
                out[k] = v
        return out

    # ---- lines and crews
    def line_for(self, machine_name):
        return self.lines.get(str(machine_name or "").strip().lower())

    def shift_info(self, line, n):
        """Crew on labor-plan line `line` for shift number n."""
        key = (line.get("id"), n)
        if key in self._shift_cache:
            return self._shift_cache[key]
        active = (line.get("activeShifts") or {}).get(str(n))
        positions = (line.get("shiftPositions") or {}).get(str(n)) or []
        crew = filled = 0
        cost = 0.0
        roles = []
        for p in positions if active else []:
            if not isinstance(p, dict):
                continue
            title = str(p.get("title") or "").strip()
            count = max(0, int(_num(p.get("reqCount"))))
            if not count:
                continue
            roles.append({"title": title, "count": count})
            for i in range(count):
                slot = self.slots.get(f"{line.get('id')}_{n}_{title}_{i}") or {}
                if isinstance(slot, dict) and str(slot.get("name") or "").strip():
                    filled += 1
                wage = _num(slot.get("wage")) if isinstance(slot, dict) and slot.get("wage") not in (None, "") else \
                    self.default_wage.get(title.lower(), 0.0)
                cost += wage
                crew += 1
        info = {"staffed": bool(active) and crew > 0, "crew": crew, "filled": filled, "costPerHour": round(cost, 2), "roles": roles}
        self._shift_cache[key] = info
        return info


def shift_numbers(shifts):
    """Suite shift id -> labor-plan shift number (1st, 2nd, 3rd ...), by their order on the Lines & Shifts page."""
    return {s["id"]: i + 1 for i, s in enumerate(shifts)}


def machine_labor(plan, cfg):
    """For each suite line linked to the plan: the plan line and the crew on each of the line's shifts."""
    if not plan:
        return {}
    nums = shift_numbers(cfg["shifts"])
    out = {}
    for m in cfg["machines"]:
        ln = plan.line_for(m["name"])
        if not ln:
            continue
        out[m["id"]] = {"line": ln.get("name"), "shifts": {sid: {"n": nums[sid], **plan.shift_info(ln, nums[sid])}
                                                             for sid in m.get("shifts") or [] if sid in nums}}
    return out
