#!/usr/bin/env bash
# Run by the "Linux" checks on GitHub, inside a Rocky Linux 9 or Fedora container with systemd (as root).
# Installs MariaDB, runs install.sh unattended, and checks what a plant screen would see, through Apache.
set -euo pipefail
pass() { echo "PASS: $*"; }
die()  { echo "FAIL: $*"; tail -60 /var/log/production-suite-install.log 2>/dev/null || true; journalctl -u production-suite-web -u httpd --no-pager -n 60 2>/dev/null || true; exit 1; }

dnf -y -q install mariadb-server tar procps-ng iproute python3 >/dev/null   # curl-minimal is already there
systemctl enable --now mariadb
SQL="$(command -v mariadb || command -v mysql)"
"$SQL" -e "CREATE DATABASE production_suite CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
          CREATE USER 'suite'@'localhost' IDENTIFIED BY 'Ci-Pass-12345'; CREATE USER 'suite'@'127.0.0.1' IDENTIFIED BY 'Ci-Pass-12345';
          GRANT ALL PRIVILEGES ON production_suite.* TO 'suite'@'localhost'; GRANT ALL PRIVILEGES ON production_suite.* TO 'suite'@'127.0.0.1';"

cd /tmp && tar xzf /src/dist/ProductionSuite-Linux.tar.gz && cd ProductionSuite-Linux
cat > /tmp/answers.json <<'JSON'
{"plant_network": "192.168.100.0/24", "db_host": "127.0.0.1", "db_port": "3306", "db_name": "production_suite",
 "db_user": "suite", "db_password": "Ci-Pass-12345", "use_asset_db": "n", "use_s2k_db": "n", "web_port": "80",
 "time_zone": "America/New_York", "scheduler_lead": "y", "station_key": "", "lead_name": "CI Lead", "lead_pin": "864213", "demo": "n"}
JSON
# install the way a server does from GitHub: get.sh finds the release, downloads the package, checks its
# checksum and runs install.sh. A small local stand-in for api.github.com serves the release this package is in.
F=/tmp/fakegh/repos/phazercil/Production-Suite/releases; mkdir -p "$F/assets" "$F/tags"
cp /src/dist/ProductionSuite-Linux.tar.gz "$F/assets/42"
sum=$(sha256sum "$F/assets/42" | awk '{print $1}')
printf '{"tag_name":"v0.0.0-ci","assets":[{"id":42,"name":"ProductionSuite-Linux.tar.gz","digest":"sha256:%s"}]}' "$sum" > "$F/latest"
cp "$F/latest" "$F/tags/v0.0.0-ci"
(cd /tmp/fakegh && exec python3 -m http.server 8099 --bind 127.0.0.1 >/dev/null 2>&1) &
sleep 2
cp "$F/assets/42" /tmp/good.tgz; echo tampered >> "$F/assets/42"
PS_GITHUB_API=http://127.0.0.1:8099 GITHUB_TOKEN=ci-token bash /src/packaging/linux/get.sh --answers /tmp/answers.json \
    > /tmp/get-bad.log 2>&1 && die "get.sh installed a package that doesn't match its checksum"
grep -q "doesn't match its checksum" /tmp/get-bad.log || die "get.sh with a bad download: $(tail -5 /tmp/get-bad.log)"
pass "get.sh refuses a download that doesn't match its checksum"
cp /tmp/good.tgz "$F/assets/42"
PS_GITHUB_API=http://127.0.0.1:8099 GITHUB_TOKEN=ci-token bash /src/packaging/linux/get.sh --version v0.0.0-ci --answers /tmp/answers.json \
    || die "get.sh (install from the release) failed"
[ -x /var/lib/production-suite-installer/v0.0.0-ci/ProductionSuite-Linux/install.sh ] || die "get.sh didn't keep the package"
pass "get.sh downloaded the release, checked it and installed the suite"

for u in production-suite-web production-suite-ticker httpd production-suite-updater.path production-suite-network.path; do
    systemctl is-active --quiet "$u" || die "$u is not running"
done
pass "services running: web, ticker, httpd, updater.path, network.path"

ss -ltn | grep -q '127.0.0.1:8008' || die "the suite isn't listening on 127.0.0.1:8008"
ss -ltn | grep -q '0.0.0.0:8008\|\*:8008' && die "the suite is reachable from the network on 8008"
pass "the suite listens on 127.0.0.1:8008 only"

v=$(runuser -u psuite -- /opt/production-suite/.venv/bin/python -c 'import sys, PIL; print(sys.version.split()[0], "Pillow", PIL.__version__)') || die "the suite's Python can't load its packages (Pillow)"
pass "the suite runs on Python $v"

code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/operator/); [ "$code" = 200 ] || die "Operator Kiosk through Apache: $code"
curl -sI http://127.0.0.1/static/suite/shell.css | grep -qi '^server: apache' || die "Apache isn't serving the static files itself"
v=$(curl -s http://127.0.0.1/api/version); echo "$v" | grep -q '"revision"' || die "/api/version: $v"
pass "Operator Kiosk and /api/version answer through Apache; Apache serves the static files ($v)"

# a client can't fake a plant-network address: Apache drops X-Forwarded-For before adding the real one
grep -q "Require ip 192.168.100.0/24" /etc/httpd/conf.d/production-suite-allow.inc || die "allow list: $(cat /etc/httpd/conf.d/production-suite-allow.inc)"
code=$(curl -s -o /dev/null -w '%{http_code}' -H 'X-Forwarded-For: 192.168.100.5' --interface 127.0.0.1 http://127.0.0.1/api/version)
[ "$code" = 200 ] || die "local request with a header: $code"
# Admin > Network: a change reaches Apache through the root helper within seconds
cd /opt/production-suite
runuser -u psuite -- .venv/bin/python manage.py network --set 192.168.100.0/24 10.42.0.0/16 >/dev/null
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

# Admin > Updates on a Linux server: a signed Linux update file with packages (the kind the Release workflow attaches
# for "Check for updates") is taken in by the suite and installed by root's updater through systemd. Made here from
# this source with the next revision, signed with a throwaway key that only this test server trusts.
PY=/opt/production-suite/.venv/bin/python
rm -rf /tmp/next && mkdir /tmp/next && tar -C /src --exclude=./dist --exclude=./.git -cf - . | tar -C /tmp/next -xf -
rev=$(sed -n 's/^REVISION = \([0-9]*\)/\1/p' /tmp/next/suite/version.py); next=$((rev + 1))
sed -i "s/^REVISION = .*/REVISION = $next/" /tmp/next/suite/version.py
sed -i "s/^\$script:SuiteRevision = .*/\$script:SuiteRevision = $next/" /tmp/next/packaging/installer/common.ps1
pk=$("$PY" /tmp/next/tools/make_update.py --new-key /tmp/ci-key.txt | sed -n 's/.*Public key: //p')
[ -n "$pk" ] || die "couldn't make a test signing key"
trust() { "$PY" - "$1" "$pk" <<'PYEOF'
import re, sys
p, pk = sys.argv[1], sys.argv[2]
s = open(p).read()
open(p, "w").write(re.sub(r"PUBLIC_KEYS = \[", f'PUBLIC_KEYS = ["{pk}", ', s, count=1))
PYEOF
}
trust /tmp/next/plant/update_key.py
trust /opt/production-suite/plant/update_key.py && chown psuite:psuite /opt/production-suite/plant/update_key.py
"$PY" - "$pk" <<'PYEOF'
import json, sys
p = "/usr/libexec/production-suite/keys.json"
keys = json.load(open(p))
json.dump([sys.argv[1]] + keys, open(p, "w"))
PYEOF
(cd /tmp/next && "$PY" packaging/build.py --linux-update --key /tmp/ci-key.txt --packages --notes "CI update") >/dev/null || die "couldn't build the Linux update file"
upd=$(ls /tmp/next/dist/ProductionSuite-Update-*-r$next-linux-with-packages.zip) || die "no Linux update file was built"
install -o psuite -g psuite -m 644 "$upd" /tmp/ci-update.zip
root_before=$(find /opt/production-suite -user root | sort)
systemctl restart production-suite-ticker
runuser -u psuite -- "$PY" manage.py shell -c "from plant import updates; updates.receive('/tmp/ci-update.zip', 'ci-update.zip', 'CI', 'now')" \
    || die "the suite refused the Linux update file"
for i in $(seq 1 240); do grep -q '"status": "done"\|"status": "failed"' updates/state.json 2>/dev/null && break; sleep 1; done
grep -q '"status": "done"' updates/state.json || die "the update didn't install: $(cat updates/state.json 2>/dev/null)"
for i in $(seq 1 60); do curl -s http://127.0.0.1/api/version | grep -q "\"revision\": *$next" && break; sleep 1; done
curl -s http://127.0.0.1/api/version | grep -q "\"revision\": *$next" || die "after the update: $(curl -s http://127.0.0.1/api/version)"
runuser -u psuite -- "$PY" -c 'import pymssql, pg8000, PIL' || die "the packages that came with the update don't load"
new_root=$(comm -13 <(echo "$root_before") <(find /opt/production-suite -user root | sort) | head -5)
[ -z "$new_root" ] || die "the update left root-owned files: $new_root"
pass "a signed Linux update file with packages installed from Admin > Updates (rev $rev -> $next, root's updater via systemd)"

# settings file is private, records are in MariaDB
[ "$(stat -c %a .env)" = 600 ] || die ".env is $(stat -c %a .env)"
[ "$(stat -c %U .env)" = psuite ] || die ".env belongs to $(stat -c %U .env)"
n=$("$SQL" -N -e "SELECT COUNT(*) FROM production_suite.plant_person WHERE role='admin'"); [ "$n" -ge 1 ] || die "no shift lead in MariaDB"
pass ".env is 600 psuite; the shift lead is in MariaDB"

# running install.sh again updates in place and keeps settings
cd /tmp/ProductionSuite-Linux && ./install.sh --answers /tmp/answers.json >/dev/null || die "second install.sh (update) failed"
grep -q 'DB_NAME="production_suite"' /opt/production-suite/.env || die "settings lost on update"
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/operator/); [ "$code" = 200 ] || die "after update: $code"
pass "running install.sh again updated in place and kept the settings"

echo "ALL LINUX CHECKS PASSED"
