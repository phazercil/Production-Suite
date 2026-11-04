#!/usr/bin/env bash
# Production Suite installer for Rocky Linux, AlmaLinux, CentOS Stream and RHEL 9, and Fedora.
#
#   sudo ./install.sh                          install, or update an existing install (settings and records are kept)
#   sudo ./install.sh --answers answers.json   unattended (same answer names as the Windows installer)
#   sudo ./install.sh --with-mariadb           also install MariaDB on this server for the suite's records
#   sudo ./install.sh --enable-https CERT KEY [CHAIN]   turn on HTTPS with your company's certificate
#
# What it sets up:
#   /opt/production-suite        the suite, run by the "psuite" account (no login)
#   production-suite-web         the web app on 127.0.0.1:8008 only
#   production-suite-ticker      machine readings, timed pulls, alerts, InfluxDB copy, update checks
#   Apache (httpd)               serves the screens on port 80 (443 with HTTPS); only the plant network may connect
#   firewalld                    Apache's port open to the plant network only (when firewalld is running)
#   production-suite-updater     installs updates from Operator Kiosk > Admin > Updates (root, on request)
#   production-suite-network     keeps Apache and firewalld in step with Admin > Network (root, on request)
# SELinux stays enforcing: the suite uses port 8008, which Apache may already connect to, and Apache's files
# live under /var/www. The suite's records stay in MariaDB; nothing here deletes them.
set -euo pipefail

DIR=/opt/production-suite
STATIC=/var/www/production-suite/static
LIBEXEC=/usr/libexec/production-suite
USER_NAME=psuite
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG=/var/log/production-suite-install.log
ANSWERS=""
WITH_MARIADB=0
ASSUME_YES=0

hostn() { hostname -f 2>/dev/null || hostname 2>/dev/null || cat /proc/sys/kernel/hostname; }
addrs() { hostname -I 2>/dev/null || ip -4 -o addr show scope global 2>/dev/null | awk '{sub(/\/.*/, "", $4); print $4}'; }
say()  { printf '%s\n' "$*" | tee -a "$LOG"; }
step() { printf '\n== %s\n' "$*" | tee -a "$LOG"; }
fail() { printf '\nSetup stopped: %s\nThe full log is in %s\n' "$*" "$LOG" | tee -a "$LOG" >&2; exit 1; }
run()  { "$@" >>"$LOG" 2>&1; }

[ "$(id -u)" -eq 0 ] || { echo "Run setup as root: sudo $0 $*"; exit 1; }
mkdir -p "$(dirname "$LOG")"; : >>"$LOG"; chmod 600 "$LOG"
echo "---- $(date '+%F %T') Production Suite install.sh $* (by ${SUDO_USER:-root})" >>"$LOG"

# ---------------------------------------------------------------- HTTPS only
enable_https() {
    local cert="$1" key="$2" chain="${3:-}"
    [ -r "$cert" ] && [ -r "$key" ] || fail "Can't read the certificate or key file."
    [ -f "$DIR/.env" ] || fail "Install the suite first, then turn on HTTPS."
    step "Turning on HTTPS"
    run dnf -y install mod_ssl || fail "Couldn't install mod_ssl (Apache's HTTPS module)."
    install -d -m 755 /etc/pki/production-suite
    install -m 644 "$cert" /etc/pki/production-suite/server.crt
    install -m 600 "$key" /etc/pki/production-suite/server.key
    local chainline=""
    if [ -n "$chain" ]; then install -m 644 "$chain" /etc/pki/production-suite/chain.crt; chainline="    SSLCertificateChainFile /etc/pki/production-suite/chain.crt"; fi
    command -v restorecon >/dev/null && restorecon -R /etc/pki/production-suite || true
    local ssl="    SSLEngine on
    SSLCertificateFile /etc/pki/production-suite/server.crt
    SSLCertificateKeyFile /etc/pki/production-suite/server.key
$chainline"
    write_apache_conf 443 https "$ssl" /etc/httpd/conf.d/production-suite-ssl.conf
    set_env PUBLIC_PORT 443
    "$LIBEXEC_PY" "$LIBEXEC/linux_apply.py" --dir "$DIR" network >>"$LOG" 2>&1 || true
    apachectl -t >>"$LOG" 2>&1 || fail "Apache doesn't accept the HTTPS settings (see $LOG). Check the certificate and key match."
    systemctl reload httpd
    say "HTTPS is on: https://$(hostn)/operator/  (http:// keeps working for machines that push readings)"
    exit 0
}

# ---------------------------------------------------------------- helpers
set_env() {   # set_env KEY VALUE: change one line of the suite's settings file
    runuser -u "$USER_NAME" -- "$DIR/.venv/bin/python" - "$DIR/.env" "$1" "$2" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).parent))
from plant.envfile import read_env, write_env
p = Path(sys.argv[1]); v = read_env(p); v[sys.argv[2]] = sys.argv[3]; write_env(p, v)
PY
}
env_get() { sed -n "s/^$1=\"\{0,1\}\([^\"]*\)\"\{0,1\}$/\1/p" "$DIR/.env" 2>/dev/null | tail -1; }

write_apache_conf() {   # port proto ssl-block file
    local port="$1" proto="$2" ssl="$3" file="$4" listen=""
    if [ "$port" != 80 ] && [ "$port" != 443 ]; then listen="Listen $port"; fi
    local host; host="$(hostn)"
    PORT="$port" PROTO="$proto" SSLBLOCK="$ssl" LISTEN="$listen" HOSTN="$host" python3 - "$HERE/templates/production-suite.conf" "$file" <<'PY'
import os, sys
t = open(sys.argv[1]).read()
for k, v in (("@PORT@", os.environ["PORT"]), ("@PROTO@", os.environ["PROTO"]), ("@SSL@", os.environ["SSLBLOCK"]),
             ("@LISTEN@", os.environ["LISTEN"]), ("@HOSTNAME@", os.environ["HOSTN"])):
    t = t.replace(k, v)
open(sys.argv[2], "w").write(t)
PY
    chmod 644 "$file"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --answers) ANSWERS="$(readlink -f "$2")"; ASSUME_YES=1; shift 2 ;;
        --with-mariadb) WITH_MARIADB=1; shift ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        --enable-https) LIBEXEC_PY=/usr/bin/python3; enable_https "${2:-}" "${3:-}" "${4:-}" ;;
        -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
        *) fail "Unknown option $1 (see --help)." ;;
    esac
done
LIBEXEC_PY=/usr/bin/python3

echo
echo "Production Suite setup ($(sed -n 's/^VERSION = "\(.*\)"/v\1/p' "$HERE/app/suite/version.py") rev $(sed -n 's/^REVISION = //p' "$HERE/app/suite/version.py"))"
echo "Log: $LOG"

# ---------------------------------------------------------------- 1. this server
step "1. Checking this server"
. /etc/os-release
case " ${ID:-} ${ID_LIKE:-} " in *" rhel "*|*" centos "*|*" rocky "*|*" almalinux "*|*" fedora "*) ;; *) fail "This installer is for Rocky, AlmaLinux, CentOS Stream, RHEL 9 or Fedora (this is ${PRETTY_NAME:-unknown})." ;; esac
FEDORA=0; [ "${ID:-}" = fedora ] && FEDORA=1
if [ "$FEDORA" = 0 ]; then
    case "${VERSION_ID:-}" in 9*) ;; *) say "  Note: built and tested for version 9; this is ${PRETTY_NAME:-unknown}. Continuing." ;; esac
fi
[ -d "$HERE/app" ] && [ -d "$HERE/wheels" ] || fail "The app and wheels folders are missing. Extract the whole download, then run ./install.sh from it."
say "  ${PRETTY_NAME}"
[ -e "$DIR/.env" ] && say "  Updating the existing install in $DIR (settings and records are kept)."

# ---------------------------------------------------------------- 2. packages
step "2. Installing Python, Apache and the SELinux tools (dnf)"
extra=(httpd policycoreutils-python-utils)
command -v curl >/dev/null || extra+=(curl)   # curl-minimal counts; asking for curl next to it makes dnf refuse
[ "$WITH_MARIADB" = 1 ] && extra+=(mariadb-server)
# Pythons the suite's packages are included for (packaging/wheels): 3.11 to 3.14
py_ok() { "$1" -c 'import sys, venv, ensurepip; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 14) else 1)' >/dev/null 2>&1; }
if [ "$FEDORA" = 1 ]; then
    # Fedora's own Python (3.13 or 3.14) is used as it is; nothing extra is needed
    run dnf -y install python3 "${extra[@]}" || fail "dnf couldn't install the packages. This server needs its normal Fedora package repositories (or a local mirror)."
    if ! py_ok /usr/bin/python3; then   # a newer Fedora: use an older Python that Fedora still offers
        for v in 3.14 3.13 3.12; do run dnf -y install "python$v" && break; done
    fi
else
    if ! run dnf -y install python3.12 python3.12-pip "${extra[@]}"; then
        # older 9.x releases have 3.11 but not 3.12
        run dnf -y install python3.11 python3.11-pip "${extra[@]}" || fail "dnf couldn't install the packages. This server needs its normal Rocky/RHEL package repositories (or a local mirror)."
    fi
fi
PY=""
for p in python3.12 python3.11 python3.13 python3.14 python3; do
    c="$(command -v "$p" 2>/dev/null)" || continue
    if py_ok "$c"; then PY="$c"; break; fi
done
[ -n "$PY" ] || fail "No Python from 3.11 to 3.14 (with venv) is available. Install one with dnf (for example python3.13) and run ./install.sh again."
command -v /usr/bin/python3 >/dev/null || fail "/usr/bin/python3 is missing (it runs the updater as root)."
say "  Using $PY ($("$PY" --version 2>&1))"
if [ "$WITH_MARIADB" = 1 ]; then
    run systemctl enable --now mariadb || fail "MariaDB didn't start."
    say "  MariaDB is running on this server. Setup will ask for an administrator login (root) once to create the suite's database."
fi

# ---------------------------------------------------------------- 3. account and files
step "3. Putting the program files in $DIR"
id "$USER_NAME" >/dev/null 2>&1 || useradd --system --home-dir "$DIR" --no-create-home --shell /sbin/nologin "$USER_NAME"
systemctl stop production-suite-web production-suite-ticker >/dev/null 2>&1 || true
install -d -m 750 -o "$USER_NAME" -g "$USER_NAME" "$DIR"
# copy without touching settings, records, logs, updates or the Python environment
cp -a "$HERE/app/." "$DIR/"
rm -rf "$DIR/wheels"; cp -a "$HERE/wheels" "$DIR/wheels"
mkdir -p "$DIR/updates" "$DIR/logs" "$DIR/backups"
chown -R "$USER_NAME:$USER_NAME" "$DIR"
chmod 750 "$DIR"
install -d -m 755 -o "$USER_NAME" -g "$USER_NAME" /var/www/production-suite "$STATIC"
command -v restorecon >/dev/null && restorecon -R /var/www/production-suite >/dev/null 2>&1 || true
# root's own copy of the updater and network helper (root never runs files the suite's account can change)
install -d -m 755 "$LIBEXEC"
install -m 644 "$DIR/tools/updater.py" "$DIR/tools/linux_apply.py" "$DIR/plant/updatefile.py" "$DIR/plant/ed25519.py" "$LIBEXEC/"
python3 - "$DIR/plant/update_key.py" "$LIBEXEC/keys.json" <<'PY'
import json, re, sys
keys = re.findall(r'"([A-Za-z0-9+/=]{40,60})"', open(sys.argv[1]).read())
open(sys.argv[2], "w").write(json.dumps(keys))
PY
chmod 644 "$LIBEXEC/keys.json"
say "  Done."

# ---------------------------------------------------------------- 4. Python packages
step "4. Installing the packages the suite needs (no internet needed)"
if ! runuser -u "$USER_NAME" -- "$DIR/.venv/bin/python" -c "import sys" >/dev/null 2>&1; then
    rm -rf "$DIR/.venv"
    runuser -u "$USER_NAME" -- "$PY" -m venv "$DIR/.venv" >>"$LOG" 2>&1 || fail "Couldn't create the Python environment."
fi
runuser -u "$USER_NAME" -- "$DIR/.venv/bin/python" -m pip install --no-index --no-cache-dir --find-links "$DIR/wheels" --disable-pip-version-check \
    -r "$DIR/requirements-windows.txt" >>"$LOG" 2>&1 || fail "Installing the packages from the wheels folder failed (see $LOG)."
say "  Done."

# ---------------------------------------------------------------- 5. settings, records database, first login
# (unbuffered: its output goes through tee, and a message like "That PIN is too easy to guess" must show before the next question)
step "5. Settings, records database and first shift lead"
args=(manage.py install_plant --behind-apache)
tmpans=""
if [ -n "$ANSWERS" ]; then
    tmpans="$(mktemp -p "$DIR" .answers-XXXXXX.json)"; cp "$ANSWERS" "$tmpans"; chown "$USER_NAME" "$tmpans"; chmod 600 "$tmpans"
    args+=(--answers "$tmpans")
fi
set +e
( cd "$DIR" && runuser -u "$USER_NAME" -- env HOME="$DIR" PYTHONUTF8=1 PYTHONUNBUFFERED=1 "$DIR/.venv/bin/python" "${args[@]}" ) 2>&1 | tee -a "$LOG"
code=${PIPESTATUS[0]}
set -e
[ -n "$tmpans" ] && rm -f "$tmpans"
[ "$code" -eq 0 ] || fail "The suite's own setup stopped (see the message above). Fix it and run ./install.sh again; answers already saved are kept."
chmod 600 "$DIR/.env"; chown "$USER_NAME:$USER_NAME" "$DIR/.env"
PUBLIC_PORT="$(env_get PUBLIC_PORT)"; PUBLIC_PORT="${PUBLIC_PORT:-80}"

# ---------------------------------------------------------------- 6. services
step "6. Starting the suite (systemd)"
for u in production-suite-web.service production-suite-ticker.service production-suite-updater.service production-suite-updater.path \
         production-suite-network.service production-suite-network.path; do
    sed "s#@DIR@#$DIR#g" "$HERE/templates/$u" > "/etc/systemd/system/$u"
    chmod 644 "/etc/systemd/system/$u"
done
systemctl daemon-reload
run systemctl enable production-suite-web production-suite-ticker production-suite-updater.path production-suite-network.path
run systemctl restart production-suite-network.path production-suite-updater.path
run systemctl restart production-suite-web production-suite-ticker || fail "The suite's services didn't start: journalctl -u production-suite-web"
say "  production-suite-web and production-suite-ticker are running."

# ---------------------------------------------------------------- 7. Apache, SELinux, firewall
step "7. Apache on port $PUBLIC_PORT, plant network only"
HTTP_PORT="$PUBLIC_PORT"; [ "$HTTP_PORT" = 443 ] && HTTP_PORT=80
write_apache_conf "$HTTP_PORT" http "" /etc/httpd/conf.d/production-suite.conf
if [ -f /etc/pki/production-suite/server.crt ]; then   # HTTPS was turned on before: keep it, with this version's settings
    chain=""; [ -f /etc/pki/production-suite/chain.crt ] && chain="    SSLCertificateChainFile /etc/pki/production-suite/chain.crt"
    write_apache_conf 443 https "    SSLEngine on
    SSLCertificateFile /etc/pki/production-suite/server.crt
    SSLCertificateKeyFile /etc/pki/production-suite/server.key
$chain" /etc/httpd/conf.d/production-suite-ssl.conf
elif [ "$PUBLIC_PORT" = 443 ]; then
    say "  Port 443 needs a certificate: run  sudo ./install.sh --enable-https CERT KEY  next. Serving on port 80 until then."
    PUBLIC_PORT=80
fi
if command -v getenforce >/dev/null && [ "$(getenforce)" != Disabled ]; then
    if [ "$PUBLIC_PORT" != 80 ] && [ "$PUBLIC_PORT" != 443 ]; then
        run semanage port -a -t http_port_t -p tcp "$PUBLIC_PORT" || run semanage port -m -t http_port_t -p tcp "$PUBLIC_PORT" || true
    fi
    # Apache may connect to 8008 (an http_port_t port); if this server's policy says otherwise, allow it.
    if ! semanage port -l 2>/dev/null | grep -E '^http_port_t\s' | grep -qw 8008; then run setsebool -P httpd_can_network_connect 1 || true; fi
    say "  SELinux: $(getenforce), Apache allowed to reach the suite."
fi
"$LIBEXEC_PY" "$LIBEXEC/linux_apply.py" --dir "$DIR" network | tee -a "$LOG" || true   # writes the allow list, opens firewalld
apachectl -t >>"$LOG" 2>&1 || fail "Apache doesn't accept its configuration (see $LOG)."
run systemctl enable httpd
run systemctl restart httpd || fail "Apache didn't start: journalctl -u httpd"

# ---------------------------------------------------------------- 8. check
step "8. Checking it answers"
ok=0
proto=http; [ "$PUBLIC_PORT" = 443 ] && proto=https
for i in $(seq 1 45); do
    if curl -fsk -o /dev/null "$proto://127.0.0.1:$PUBLIC_PORT/api/version"; then ok=1; break; fi
    sleep 2
done
[ "$ok" = 1 ] || fail "The suite isn't answering through Apache yet. Check: systemctl status production-suite-web httpd, and $DIR/logs/web.log"
v="$(curl -fsk "$proto://127.0.0.1:$PUBLIC_PORT/api/version")"
host="$(hostn)"
portpart=""; { [ "$PUBLIC_PORT" = 80 ] || [ "$PUBLIC_PORT" = 443 ]; } || portpart=":$PUBLIC_PORT"
echo
say "Production Suite is installed and running ($v)."
say "Open these from any screen on the plant network:"
say "  $proto://$host$portpart/operator/    Operator Kiosk"
for ip in $(addrs); do case "$ip" in *:*) ;; *) say "  $proto://$ip$portpart/operator/";; esac; done
say "  $proto://$host$portpart/scheduler/   Scheduler"
say "  $proto://$host$portpart/data/        Data Kiosk (wall screen)"
say "  $proto://$host$portpart/reports/     Reports"
say ""
say "Start / stop / restart:  sudo systemctl restart production-suite-web production-suite-ticker"
say "Logs: $DIR/logs   ·   Apache: /var/log/httpd/production-suite-*.log   ·   Updates: Operator Kiosk > Admin > Updates"
[ -f /etc/httpd/conf.d/production-suite-ssl.conf ] || say "HTTPS: sudo ./install.sh --enable-https /path/server.crt /path/server.key [/path/chain.crt]"
