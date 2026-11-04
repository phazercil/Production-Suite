"""Plant network lock.

The suite only talks to devices on the plant network:
- screens may only connect from an address on the plant network (PrivateNetworkOnlyMiddleware);
- every address the server itself connects to (machine feeds, S2K, people list, webhook, Grafana,
  MariaDB, asset database) must be on the plant network. The exceptions are cloud services an Admin
  chooses to set up, each used only when its login is saved: Paycom (PAYCOM_SID/TOKEN), Twilio texts, an
  internet mail server, and updates from GitHub (GITHUB_UPDATES_TOKEN, plant/ghupdates.py).

The plant network is 192.168.100.0/24 unless changed in Operator Kiosk > Admin > Network (stored in the
database), or by the installer (PLANT_NETWORKS in .env), or with `python manage.py network --set ...`.
This server itself (127.x / ::1) is always allowed, so the setting can always be fixed from the server.

Only private ranges can be entered (10.x, 172.16-31.x, 192.168.x, 100.64-127.x), so the setting can never
open the suite to the internet. Windows Firewall is kept in step with the same list.
"""
import ipaddress
import logging
import os
from pathlib import Path
import socket
import subprocess
import time
from urllib.parse import urlsplit

log = logging.getLogger("plant")

DEFAULT_NETWORKS = ["192.168.100.0/24"]
# The only address space a plant network setting may use. Public internet ranges are always refused.
PRIVATE_SPACE = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")]
LOOPBACK = [ipaddress.ip_network(n) for n in ("127.0.0.0/8", "::1/128")]
MAX_ENTRIES = 16
FIREWALL_RULE = "Production Suite"
CACHE_SECONDS = 5


class NotPrivate(Exception):
    pass


class BadNetwork(ValueError):
    pass


# ------------------------------------------------------------------ parsing
def parse_networks(values):
    """Turn a list (or comma/line separated text) of ranges or single addresses into networks.
    Raises BadNetwork with a plain explanation for the first entry that can't be used."""
    if isinstance(values, str):
        values = values.replace(";", ",").replace("\n", ",").split(",")
    out = []
    for raw in values or []:
        txt = str(raw).strip()
        if not txt:
            continue
        try:
            net = ipaddress.ip_network(txt, strict=False)
        except ValueError:
            raise BadNetwork(f'"{txt}" is not a network. Use a range like 192.168.100.0/24 or a single address like 192.168.100.25.')
        if net.version != 4:
            raise BadNetwork(f"{txt}: plant screens connect over IPv4, so use an IPv4 range like 192.168.100.0/24.")
        if not any(net.subnet_of(p) for p in PRIVATE_SPACE):
            raise BadNetwork(f"{txt} is not a private network range, so it can't be allowed. Private ranges start with "
                             "10., 172.16. to 172.31., 192.168. or 100.64. to 100.127.")
        if net not in out:
            out.append(net)
    if not out:
        raise BadNetwork("Keep at least one plant network range, for example 192.168.100.0/24.")
    if len(out) > MAX_ENTRIES:
        raise BadNetwork(f"Use {MAX_ENTRIES} ranges or fewer.")
    return out


def describe(net):
    """192.168.100.0/24 -> '192.168.100.0 to 192.168.100.255 (256 addresses)'."""
    if net.num_addresses == 1:
        return f"{net.network_address} only"
    return f"{net.network_address} to {net.broadcast_address} ({net.num_addresses:,} addresses)"


# ------------------------------------------------------------------ the current setting
_cache = {"t": 0.0, "nets": None, "source": None}


def _from_database():
    try:
        from .models import PlantSettings
        net = PlantSettings.objects.filter(pk=1).values_list("network", flat=True).first() or {}
        saved = net.get("allowed")
        if saved:
            return parse_networks(saved)
    except BadNetwork as e:
        log.error("Saved plant network is not usable (%s); using the installer setting instead.", e)
    except Exception:  # database not set up yet (first install) or not reachable
        pass
    return None


def installer_networks():
    """The installer's setting (PLANT_NETWORKS in .env) or the default. Used before the database is reachable,
    for example to check the MariaDB server itself."""
    raw = os.environ.get("PLANT_NETWORKS", "").strip()
    if raw:
        try:
            return parse_networks(raw)
        except BadNetwork as e:
            log.error("PLANT_NETWORKS in .env is not usable (%s); using %s.", e, DEFAULT_NETWORKS[0])
    return parse_networks(DEFAULT_NETWORKS)


def current(with_source=False):
    """The plant network ranges in force now (cached for a few seconds)."""
    now = time.monotonic()
    if _cache["nets"] is None or now - _cache["t"] > CACHE_SECONDS:
        nets, source = _from_database(), "settings"
        if nets is None:
            nets = installer_networks()
            source = "installer" if os.environ.get("PLANT_NETWORKS", "").strip() else "default"
        _cache.update(t=now, nets=nets, source=source)
    return (_cache["nets"], _cache["source"]) if with_source else _cache["nets"]


def invalidate():
    _cache["nets"] = None


# ------------------------------------------------------------------ checks
def _ip(addr):
    try:
        ip = ipaddress.ip_address(str(addr).split("%")[0].strip().strip("[]"))
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip


def is_loopback(addr):
    ip = _ip(addr)
    return bool(ip and any(ip in n for n in LOOPBACK if n.version == ip.version))


def is_allowed_ip(addr, nets=None):
    """True for this server itself and for addresses on the plant network."""
    ip = _ip(addr)
    if ip is None:
        return False
    if any(ip in n for n in LOOPBACK if n.version == ip.version):
        return True
    return any(ip in n for n in (nets if nets is not None else current()) if n.version == ip.version)


is_private_ip = is_allowed_ip  # older name, kept for callers


def _is_private_space(ip):
    return any(ip in n for n in PRIVATE_SPACE if n.version == ip.version) or ip.is_private


def is_private_space(addr):
    """True for any private or loopback address (used for the server's own listening address)."""
    ip = _ip(addr)
    return bool(ip and (_is_private_space(ip) or ip.is_loopback))


def check_host(host, what="That address", nets=None):
    """Raise NotPrivate unless every address the host name resolves to is on the plant network."""
    host = (host or "").strip().strip("[]")
    if not host:
        raise NotPrivate(f"{what} is empty.")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        raise NotPrivate(f"{what} ({host}) can't be found on the network. Use a server name or IP address on the plant network.")
    nets = nets if nets is not None else current()
    addrs = {i[4][0] for i in infos}
    outside = sorted(a for a in addrs if not is_allowed_ip(a, nets))
    if outside:
        ips = [_ip(a) for a in outside]
        if any(ip is None or not _is_private_space(ip) for ip in ips):
            raise NotPrivate(f"{what} ({host}) is on the public internet ({', '.join(outside)}). "
                             "The suite only connects to devices on the plant network.")
        raise NotPrivate(f"{what} ({host}, {', '.join(outside)}) is not on the plant network ({', '.join(map(str, nets))}). "
                         "If it belongs to the plant, a shift lead can add its range in Operator Kiosk > Admin > Network.")
    return addrs


def check_url(url, what="That address", nets=None):
    """A http(s) URL whose host is on the plant network. Returns the URL, trimmed."""
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise NotPrivate(f"{what} must be a web address like http://192.168.100.20:3000/...")
    if parts.username or parts.password:
        raise NotPrivate(f"{what} can't contain a user name or password.")
    check_host(parts.hostname, what, nets)
    return url


# ------------------------------------------------------------------ Windows Firewall
def sync_firewall(nets=None):
    """Make the 'Production Suite' firewall rule allow exactly the plant network. Returns (ok, message).
    Only on Windows; the installer creates the rule and the suite runs as the system account there."""
    nets = nets if nets is not None else current()
    if os.name != "nt":
        return _linux_sync(nets)
    ranges = ",".join(f"'{n}'" for n in nets)  # every entry was validated by ipaddress, so this is safe to quote
    script = (f"$r = Get-NetFirewallRule -DisplayName '{FIREWALL_RULE}' -ErrorAction Stop; "
              f"$r | Set-NetFirewallRule -RemoteAddress @({ranges}) -ErrorAction Stop")
    try:
        p = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"Couldn't update Windows Firewall ({e}). The suite's own address check still applies."
    if p.returncode != 0:
        err = (p.stderr or p.stdout or "").strip().splitlines()
        detail = err[0][:200] if err else f"code {p.returncode}"
        if "No MSFT_NetFirewallRule" in (p.stderr or "") or "ObjectNotFound" in (p.stderr or ""):
            return False, f"There is no firewall rule named '{FIREWALL_RULE}'. Re-run the installer to create it."
        return False, f"Couldn't update Windows Firewall ({detail}). The suite's own address check still applies."
    return True, f"Windows Firewall allows {', '.join(map(str, nets))} on the suite's port."


LINUX_NETWORK_UNIT = Path("/etc/systemd/system/production-suite-network.path")


def _linux_sync(nets):
    """Linux install: ask the root helper (tools/linux_apply.py, started by production-suite-network.path) to put
    the plant network into Apache and firewalld, and wait a few seconds for its answer."""
    import json
    import time

    from django.conf import settings
    if not LINUX_NETWORK_UNIT.exists():
        return None, "No firewall is managed on this server; the suite's own address check still applies."
    folder = Path(settings.BASE_DIR) / "updates"
    folder.mkdir(exist_ok=True)
    res = folder / "network-result.json"
    before = res.stat().st_mtime if res.exists() else 0
    tmp = folder / "network-request.tmp"
    tmp.write_text(json.dumps({"networks": [str(n) for n in nets]}), encoding="utf-8")
    os.replace(tmp, folder / "network-request.json")
    for _ in range(40):
        time.sleep(0.25)
        if res.exists() and res.stat().st_mtime > before:
            try:
                r = json.loads(res.read_text(encoding="utf-8"))
                return bool(r.get("ok")), str(r.get("msg") or "")[:300]
            except ValueError:
                break
    return None, "Sent to Apache and firewalld; they're updated within a few seconds."


def save(nets, by="system", sync=True):
    """Store the plant network and (unless sync=False) update the firewall. Returns (ok, firewall message)."""
    from django.utils import timezone

    from .models import PlantSettings
    PlantSettings.load()
    PlantSettings.objects.filter(pk=1).update(network={"allowed": [str(n) for n in nets], "by": str(by)[:80],
                                                       "at": timezone.now().isoformat(timespec="seconds")})
    invalidate()
    if not sync:
        return None, ""
    return sync_and_record(nets)


def sync_and_record(nets=None):
    """Update the firewall rule and keep the result for the Admin > Network page."""
    from django.db import transaction

    from .models import PlantSettings
    nets = nets if nets is not None else current()
    ok, msg = sync_firewall(nets)
    PlantSettings.load()
    with transaction.atomic():
        st = PlantSettings.objects.select_for_update().get(pk=1)
        net = dict(st.network or {})
        net["firewall"] = {"ok": ok, "msg": msg, "t": int(time.time() * 1000), "nets": [str(n) for n in nets]}
        st.network = net
        st.save(update_fields=["network"])
    PlantSettings.bump()
    if ok is False:
        log.warning("Plant network firewall: %s", msg)
    return ok, msg
