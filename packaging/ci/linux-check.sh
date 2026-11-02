#!/usr/bin/env bash
# Run by the "Linux" check on GitHub, inside a Rocky Linux 9 container with systemd (as root).
# Installs MariaDB, runs install.sh unattended, and checks what a plant screen would see, through Apache.
set -euo pipefail
pass() { echo "PASS: $*"; }
die()  { echo "FAIL: $*"; tail -60 /var/log/production-suite-install.log 2>/dev/null || true; journalctl -u production-suite-web -u httpd --no-pager -n 60 2>/dev/null || true; exit 1; }

dnf -y -q install mariadb-server tar procps-ng iproute >/dev/null   # curl-minimal is already there
systemctl enable --now mariadb
mysql -e "CREATE DATABASE production_suite CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
          CREATE USER 'suite'@'localhost' IDENTIFIED BY 'Ci-Pass-12345'; CREATE USER 'suite'@'127.0.0.1' IDENTIFIED BY 'Ci-Pass-12345';
          GRANT ALL PRIVILEGES ON production_suite.* TO 'suite'@'localhost'; GRANT ALL PRIVILEGES ON production_suite.* TO 'suite'@'127.0.0.1';"

cd /tmp && tar xzf /src/dist/ProductionSuite-Linux.tar.gz && cd ProductionSuite-Linux
cat > /tmp/answers.json <<'JSON'
{"plant_network": "192.168.100.0/24, 172.16.0.0/12", "db_host": "127.0.0.1", "db_port": "3306", "db_name": "production_suite",
 "db_user": "suite", "db_password": "Ci-Pass-12345", "use_asset_db": "n", "use_s2k_db": "n", "web_port": "80",
 "time_zone": "America/New_York", "scheduler_lead": "y", "station_key": "", "lead_name": "CI Lead", "lead_pin": "864213", "demo": "n"}
JSON
./install.sh --answers /tmp/answers.json || die "install.sh failed"
pass "install.sh finished"

for u in production-suite-web production-suite-ticker httpd production-suite-updater.path production-suite-network.path; do
    systemctl is-active --quiet "$u" || die "$u is not running"
done
pass "services running: web, ticker, httpd, updater.path, network.path"

ss -ltn | grep -q '127.0.0.1:8008' || die "the suite isn't listening on 127.0.0.1:8008"
ss -ltn | grep -q '0.0.0.0:8008\|\*:8008' && die "the suite is reachable from the network on 8008"
pass "the suite listens on 127.0.0.1:8008 only"

code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/operator/); [ "$code" = 200 ] || die "Operator Kiosk through Apache: $code"
curl -sI http://127.0.0.1/static/suite/shell.css | grep -qi '^server: apache' || die "Apache isn't serving the static files itself"
v=$(curl -s http://127.0.0.1/api/version); echo "$v" | grep -q '"revision"' || die "/api/version: $v"
pass "Operator Kiosk and /api/version answer through Apache; Apache serves the static files ($v)"

# a client can't fake a plant-network address: Apache drops X-Forwarded-For before adding the real one
grep -q "Require ip 192.168.100.0/24" /etc/httpd/conf.d/production-suite-allow.inc || die "allow list: $(cat /etc/httpd/conf.d/production-suite-allow.inc)"
code=$(curl -s -o /dev/null -w '%{http_code}' -H 'X-Forwarded-For: 192.168.100.5' --interface 127.0.0.1 http://127.0.0.1/api/version)
[ "$code" = 200 ] || die "local request with a header: $code"
# a request from an address outside the plant network (this container's own non-loopback address) is refused by Apache
ip=$(ip -4 -o addr show scope global | awk '{sub(/\/.*/, "", $4); print $4; exit}')
if [ -n "$ip" ] && ! printf '%s' "$ip" | grep -Eq '^(192\.168\.100\.|172\.(1[6-9]|2[0-9]|3[01])\.)'; then
    code=$(curl -s -o /dev/null -w '%{http_code}' -H 'X-Forwarded-For: 192.168.100.5' "http://$ip/api/version")
    [ "$code" = 403 ] || die "a request from $ip (outside the plant network) got $code"
    pass "a request from $ip (outside the plant network) is refused, even claiming 192.168.100.5"
else
    echo "note: this container's address ($ip) is inside the test plant network; skipping the outside-address check"
fi

# Admin > Network: a change reaches Apache through the root helper within seconds
cd /opt/production-suite
runuser -u psuite -- .venv/bin/python manage.py network --set "192.168.100.0/24, 10.42.0.0/16" >/dev/null
for i in $(seq 1 30); do grep -q "10.42.0.0/16" /etc/httpd/conf.d/production-suite-allow.inc && break; sleep 1; done
grep -q "Require ip 10.42.0.0/16" /etc/httpd/conf.d/production-suite-allow.inc || die "the new plant network didn't reach Apache"
pass "a plant network change reached Apache's allow list (via production-suite-network.path, as root)"

# Admin > Updates: an update request runs the root updater (here: "go back" with nothing to go back to)
install -d -o psuite -g psuite updates/run
runuser -u psuite -- sh -c 'echo "{\"args\": [\"goback\"]}" > updates/run/request.json'
for i in $(seq 1 60); do [ -f updates/state.json ] && grep -q '"failed"' updates/state.json && break; sleep 1; done
grep -q "no earlier version" updates/state.json || die "the updater didn't run: $(cat updates/state.json 2>/dev/null)"
[ ! -e updates/run/request.json ] || die "the request file is still there"
[ "$(stat -c %U /usr/libexec/production-suite/updater.py)" = root ] || die "root's updater copy isn't root-owned"
pass "an update request ran root's own copy of the updater"

# settings file is private, records are in MariaDB
[ "$(stat -c %a .env)" = 600 ] || die ".env is $(stat -c %a .env)"
[ "$(stat -c %U .env)" = psuite ] || die ".env belongs to $(stat -c %U .env)"
n=$(mysql -N -e "SELECT COUNT(*) FROM production_suite.plant_person WHERE role='admin'"); [ "$n" -ge 1 ] || die "no shift lead in MariaDB"
pass ".env is 600 psuite; the shift lead is in MariaDB"

# running install.sh again updates in place and keeps settings
cd /tmp/ProductionSuite-Linux && ./install.sh --answers /tmp/answers.json >/dev/null || die "second install.sh (update) failed"
grep -q 'DB_NAME="production_suite"' /opt/production-suite/.env || die "settings lost on update"
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/operator/); [ "$code" = 200 ] || die "after update: $code"
pass "running install.sh again updated in place and kept the settings"

echo "ALL LINUX CHECKS PASSED"
