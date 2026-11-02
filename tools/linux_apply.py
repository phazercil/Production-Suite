"""Linux install: applies the plant network to Apache and firewalld, as root.

The suite runs as the unprivileged "psuite" account, so when the plant network changes (Operator Kiosk >
Admin > Network, setup, or start-up) it writes updates/network-request.json. The systemd path unit
production-suite-network.path sees the file and runs this script as root, which:

  - writes /etc/httpd/conf.d/production-suite-allow.inc (the addresses Apache lets in),
  - makes firewalld allow Apache's port from exactly those ranges (rich rules it owns, in the default zone),
  - reloads Apache, and writes updates/network-result.json for Admin > Network.

Only private ranges are ever applied, whatever the request file says. Standard library only.

    python tools/linux_apply.py --dir /opt/production-suite network
"""
import argparse
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ALLOW_INC = Path("/etc/httpd/conf.d/production-suite-allow.inc")
STATE = Path("/etc/production-suite/firewalld-rules.json")
PRIVATE = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")]


def private_nets(raw):
    out = []
    for x in raw if isinstance(raw, list) else []:
        try:
            n = ipaddress.ip_network(str(x).strip(), strict=False)
        except ValueError:
            continue
        if n.version == 4 and any(n.subnet_of(p) for p in PRIVATE) and str(n) not in out:
            out.append(str(n))
    return out


def env_value(folder, key, default=""):
    try:
        for line in (Path(folder) / ".env").read_text(encoding="utf-8-sig").splitlines():
            m = re.match(rf'^\s*{key}\s*=\s*"?([^"]*)"?\s*$', line)
            if m:
                return m.group(1)
    except OSError:
        pass
    return default


def safe_write(path, text):
    """Write without following a link planted where the file goes (this runs as root; updates/ belongs to the suite)."""
    tmp = str(path) + ".tmp"
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, str(path))


def run(args):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=120)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)


def apache_include(nets):
    lines = ["# Written by Production Suite (Operator Kiosk > Admin > Network). Changes here are replaced.",
             "Require local"] + [f"Require ip {n}" for n in nets]
    tmp = ALLOW_INC.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="ascii")
    os.chmod(tmp, 0o644)
    os.replace(tmp, ALLOW_INC)
    if shutil.which("restorecon"):
        run(["restorecon", str(ALLOW_INC)])
    code, out = run(["apachectl", "-t"])
    if code != 0:
        return False, f"Apache's configuration has a problem: {out[-300:]}"
    if run(["systemctl", "is-active", "--quiet", "httpd"])[0] != 0:
        return True, "Apache will allow " + ", ".join(nets) + " when it starts."
    code, out = run(["systemctl", "reload", "httpd"])
    return code == 0, ("Apache allows " + ", ".join(nets) + ".") if code == 0 else f"Couldn't reload Apache: {out[-200:]}"


def firewalld(nets, ports):
    if not shutil.which("firewall-cmd"):
        return None, "firewalld isn't installed; only Apache's own address check applies."
    code, _ = run(["firewall-cmd", "--state"])
    if code != 0:
        return None, "firewalld isn't running; only Apache's own address check applies."
    want = [f'rule family="ipv4" source address="{n}" port port="{p}" protocol="tcp" accept' for n in nets for p in ports]
    try:
        had = json.loads(STATE.read_text())
    except (OSError, ValueError):
        had = []
    for rule in had:
        if rule not in want:
            run(["firewall-cmd", "--permanent", f"--remove-rich-rule={rule}"])
    bad = []
    for rule in want:
        code, out = run(["firewall-cmd", "--permanent", f"--add-rich-rule={rule}"])
        if code != 0 and "ALREADY_ENABLED" not in out:
            bad.append(out[-120:])
    run(["firewall-cmd", "--reload"])
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(want, indent=1))
    if bad:
        return False, "firewalld refused some rules: " + "; ".join(bad)
    return True, f"firewalld allows port {', '.join(ports)} from {', '.join(nets)}."


def apply_network(folder):
    folder = Path(folder)
    req = folder / "updates" / "network-request.json"
    try:
        data = json.loads(req.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    try:
        req.unlink()
    except OSError:
        pass
    nets = private_nets(data.get("networks")) or private_nets([x for x in env_value(folder, "PLANT_NETWORKS", "192.168.100.0/24").split(",")])
    pub = env_value(folder, "PUBLIC_PORT", "80") or "80"
    ports = sorted({pub, "80"} | ({"443"} if Path("/etc/httpd/conf.d/production-suite-ssl.conf").exists() else set()))
    a_ok, a_msg = apache_include(nets)
    f_ok, f_msg = firewalld(nets, ports)
    ok = a_ok and f_ok is not False
    res = {"ok": ok, "msg": f"{a_msg} {f_msg}", "networks": nets, "at": datetime.now().astimezone().isoformat(timespec="seconds")}
    out = folder / "updates" / "network-result.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    safe_write(out, json.dumps(res))
    try:
        st = folder.stat()
        os.chown(out, st.st_uid, st.st_gid)
    except OSError:
        pass
    print(res["msg"])
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", default="/opt/production-suite")
    ap.add_argument("job", choices=["network"])
    a = ap.parse_args(argv)
    return apply_network(a.dir)


if __name__ == "__main__":
    sys.exit(main())
