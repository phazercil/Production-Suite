"""Linux install: applies the plant network to Apache and firewalld, as root.

The suite runs as the unprivileged "psuite" account, so when the plant network changes (Operator Kiosk >
Admin > Network, setup, or start-up) it writes updates/network-request.json. The systemd path unit
production-suite-network.path sees the file and runs this script as root, which:

  - writes /etc/httpd/conf.d/production-suite-allow.inc (the addresses Apache lets in),
  - writes /etc/httpd/conf.d/production-suite-video.inc (live camera video: only /video/ws?src=cam<number> goes
    on to go2rtc at 127.0.0.1:8009, from the plant network) and makes the suite's Apache site include it,
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
CONF_D = Path("/etc/httpd/conf.d")
VIDEO_INC = "production-suite-video.inc"
VIDEO_PORT = 8009   # an http_port_t port, so SELinux already lets Apache connect to it
VIDEO_RULES = """# Written by Production Suite (cameras/live.py, tools/linux_apply.py). Changes here are replaced.
# Live camera video: Apache passes only /video/ws?src=cam<number> on to go2rtc (127.0.0.1:{port}), and only from the
# plant network, so nothing else of go2rtc (its settings, other addresses) can be reached.
<Location "/video/ws">
    ProxyPass "http://127.0.0.1:{port}/api/ws" upgrade=websocket timeout=3600
    <RequireAll>
        <RequireAny>
            Include conf.d/production-suite-allow.inc
        </RequireAny>
        Require expr "%{{QUERY_STRING}} =~ /^src=cam[0-9]+$/"
    </RequireAll>
</Location>
"""
INCLUDE_LINE = f"    IncludeOptional conf.d/{VIDEO_INC}"
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


def own(path, folder):
    """Give a file or folder this script made to whoever owns the install folder (the suite's account)."""
    try:
        st = Path(folder).stat()
        os.chown(str(path), st.st_uid, st.st_gid, follow_symlinks=False)
    except OSError:
        pass


def run(args):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=120)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)


def video_include(conf_d=CONF_D):
    """Write the live-video rules and add them to the end of each of the suite's Apache sites (after its
    <Location />, so their address rules win for /video/ws). Returns the sites changed."""
    conf_d = Path(conf_d)
    tmp = conf_d / (VIDEO_INC + ".tmp")
    tmp.write_text(VIDEO_RULES.format(port=VIDEO_PORT), encoding="ascii")
    os.chmod(tmp, 0o644)
    os.replace(tmp, conf_d / VIDEO_INC)
    changed = []
    for site in sorted(conf_d.glob("production-suite*.conf")):
        try:
            text = site.read_text(encoding="utf-8")
        except OSError:
            continue
        if "127.0.0.1:8008" not in text or "</VirtualHost>" not in text or VIDEO_INC in text:
            continue
        text = text.replace("</VirtualHost>", INCLUDE_LINE + "\n</VirtualHost>")
        stmp = site.with_suffix(".conf.tmp")
        stmp.write_text(text, encoding="utf-8")
        os.chmod(stmp, 0o644)
        os.replace(stmp, site)
        changed.append(site)
    if shutil.which("restorecon"):
        run(["restorecon", str(conf_d / VIDEO_INC)] + [str(x) for x in changed])
    return changed


def video_undo(changed, conf_d=CONF_D):
    """Apache refused the configuration: take the live-video rules back out."""
    for site in changed:
        try:
            site.write_text(site.read_text(encoding="utf-8").replace(INCLUDE_LINE + "\n", ""), encoding="utf-8")
        except OSError:
            pass
    try:
        (Path(conf_d) / VIDEO_INC).unlink()
    except OSError:
        pass


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
    video, v_msg = True, "Live camera video is passed on (/video/ws)."
    try:
        changed = video_include()
    except OSError as e:
        changed, video, v_msg = [], False, f"Couldn't write the live video rules: {e}"
    a_ok, a_msg = apache_include(nets)
    if not a_ok and video:
        video_undo(changed)            # never leave Apache broken for live video: try again without it
        video, v_msg = False, "Live camera video isn't passed on: Apache refused its rules. " + a_msg
        a_ok, a_msg = apache_include(nets)
    f_ok, f_msg = firewalld(nets, ports)
    ok = a_ok and f_ok is not False
    res = {"ok": ok, "msg": f"{a_msg} {f_msg}", "networks": nets, "video": video, "videoMsg": v_msg,
           "at": datetime.now().astimezone().isoformat(timespec="seconds")}
    out = folder / "updates" / "network-result.json"
    if not out.parent.exists():   # make it the suite's folder, not root's, or the suite can't write requests to it
        out.parent.mkdir(parents=True, exist_ok=True)
        own(out.parent, folder)
    safe_write(out, json.dumps(res))
    own(out, folder)
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
