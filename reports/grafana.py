"""Data behind Grafana panels, for the Reports page.

Uses a Grafana service account token (Viewer role is enough) set in Operator Kiosk > Admin > Connections.
The suite asks Grafana for a dashboard's panels and runs a panel's own queries through Grafana's query API
for the report's time range and line, so the numbers are exactly what that panel shows. Grafana must be on
the plant network, like every other connection.
"""
import re

from django.conf import settings

from hmi import integrations
from plant import liveconfig
from plant.models import PlantSettings


class GrafanaError(Exception):
    pass


def _cfg():
    liveconfig.refresh()
    g = PlantSettings.load().settings.get("grafana") or {}
    token = settings.SUITE.get("GRAFANA_TOKEN") or ""
    if not g.get("url"):
        raise GrafanaError("Set the Grafana address in Operator Kiosk > Admin > Connections first.")
    if not token:
        raise GrafanaError("Add a Grafana service account token in Operator Kiosk > Admin > Connections.")
    return g, token


def _call(path, data=None):
    g, token = _cfg()
    try:
        return integrations._request(g["url"].rstrip("/") + path, data=data, headers={"Authorization": f"Bearer {token}"}, timeout=15)
    except integrations.ConnError as e:
        raise GrafanaError(str(e))


def ready():
    try:
        _cfg()
        return True
    except GrafanaError:
        return False


def panels(uid=None):
    g, _ = _cfg()
    uid = uid or g.get("uid")
    if not uid:
        raise GrafanaError("Set the Grafana dashboard UID in Operator Kiosk > Admin > Connections.")
    d = _call(f"/api/dashboards/uid/{uid}")
    out = []

    def walk(ps):
        for p in ps or []:
            if p.get("type") == "row":
                walk(p.get("panels"))
                continue
            if p.get("targets"):
                out.append({"id": p.get("id"), "title": p.get("title") or f"Panel {p.get('id')}", "type": p.get("type"),
                            "datasource": p.get("datasource"), "targets": p.get("targets")})
    walk(((d or {}).get("dashboard") or {}).get("panels"))
    return out


def _subst(v, variables):
    if isinstance(v, str):
        for k, val in variables.items():
            v = re.sub(r"\$\{" + re.escape(k) + r"(:[a-z]+)?\}|\[\[" + re.escape(k) + r"\]\]|\$" + re.escape(k) + r"\b", str(val).replace("\\", "\\\\"), v)
        return v
    if isinstance(v, dict):
        return {k: _subst(x, variables) for k, x in v.items()}
    if isinstance(v, list):
        return [_subst(x, variables) for x in v]
    return v


def panel_data(panel, frm_ms, to_ms, variables):
    """Run one panel's queries. Returns [{name, points: [[t, v]...], last, min, max, avg, sum}] per number series."""
    qs = []
    for i, t in enumerate(panel.get("targets") or []):
        if t.get("hide"):
            continue
        q = _subst(dict(t), variables)
        q.setdefault("refId", f"R{i}")
        q.setdefault("datasource", panel.get("datasource"))
        q.update(intervalMs=60000, maxDataPoints=400)
        qs.append(q)
    if not qs:
        return []
    r = _call("/api/ds/query", {"from": str(int(frm_ms)), "to": str(int(to_ms)), "queries": qs}) or {}
    series = []
    for ref, res in (r.get("results") or {}).items():
        if res.get("error"):
            raise GrafanaError(f"Grafana: {res['error']}")
        for fr in res.get("frames") or []:
            fields = (fr.get("schema") or {}).get("fields") or []
            values = (fr.get("data") or {}).get("values") or []
            tcol = next((i for i, f in enumerate(fields) if f.get("type") == "time"), None)
            for i, f in enumerate(fields):
                if f.get("type") != "number" or i >= len(values):
                    continue
                vals = [v for v in values[i] if isinstance(v, (int, float))]
                pts = []
                if tcol is not None and tcol < len(values):
                    pts = [[t, v] for t, v in zip(values[tcol], values[i]) if isinstance(v, (int, float))]
                    if len(pts) > 300:
                        step = len(pts) / 300
                        pts = [pts[int(k * step)] for k in range(300)]
                name = (f.get("config") or {}).get("displayName") or f.get("name") or ref
                labels = f.get("labels") or {}
                if labels:
                    name += " " + ", ".join(f"{k}={v}" for k, v in labels.items())
                series.append({"name": name, "points": pts, "last": vals[-1] if vals else None, "min": min(vals) if vals else None,
                               "max": max(vals) if vals else None, "avg": round(sum(vals) / len(vals), 3) if vals else None,
                               "sum": round(sum(vals), 3) if vals else None, "n": len(vals)})
    return series[:12]
