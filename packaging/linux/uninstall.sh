#!/usr/bin/env bash
# Removes Production Suite from this server: its services, its Apache site and firewalld rules, and the
# program files in /opt/production-suite. The records in MariaDB are NOT touched. A copy of the settings
# file (.env) is kept in /root/production-suite.env.saved in case you reinstall.
#   sudo ./uninstall.sh
set -euo pipefail
DIR=/opt/production-suite
[ "$(id -u)" -eq 0 ] || { echo "Run as root: sudo $0"; exit 1; }
echo "This removes Production Suite from this server (records in MariaDB are kept)."
read -r -p "Continue? (y/N) " v; case "$v" in y|Y|yes) ;; *) echo "Nothing was changed."; exit 0 ;; esac
for u in production-suite-web production-suite-ticker production-suite-updater.path production-suite-network.path; do
    systemctl disable --now "$u" >/dev/null 2>&1 || true
done
rm -f /etc/systemd/system/production-suite-{web,ticker,updater,network}.service /etc/systemd/system/production-suite-{updater,network}.path
systemctl daemon-reload
if command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1 && [ -f /etc/production-suite/firewalld-rules.json ]; then
    python3 -c "import json;[print(r) for r in json.load(open('/etc/production-suite/firewalld-rules.json'))]" | while read -r r; do
        firewall-cmd --permanent --remove-rich-rule="$r" >/dev/null 2>&1 || true
    done
    firewall-cmd --reload >/dev/null 2>&1 || true
fi
rm -f /etc/httpd/conf.d/production-suite.conf /etc/httpd/conf.d/production-suite-ssl.conf /etc/httpd/conf.d/production-suite-allow.inc
systemctl reload httpd >/dev/null 2>&1 || true
[ -f "$DIR/.env" ] && install -m 600 "$DIR/.env" /root/production-suite.env.saved && echo "Settings saved to /root/production-suite.env.saved"
rm -rf "$DIR" /var/www/production-suite /usr/libexec/production-suite /etc/production-suite /etc/pki/production-suite
userdel psuite >/dev/null 2>&1 || true
echo "Production Suite was removed. Apache and MariaDB themselves are left installed."
