PRODUCTION SUITE - INSTALLING ON ROCKY / ALMALINUX / CENTOS STREAM / RHEL 9 OR FEDORA
====================================================================================

Before you start
----------------
- A server running Rocky Linux 9, AlmaLinux 9, CentOS Stream 9, RHEL 9 or Fedora (checked on Fedora 44),
  with its normal package repositories. dnf installs Python (3.12 on EL9; Fedora's own Python), Apache and
  the SELinux tools; nothing else comes from the internet.
- A root (sudo) login.
- Your MariaDB server: its address and a login that may create tables in the suite's database. Setup can
  use a MariaDB administrator login (e.g. root) once to create the database and user. To put MariaDB on this
  same server instead, add --with-mariadb.

Install (or update)
-------------------
  tar xzf ProductionSuite-Linux.tar.gz
  cd ProductionSuite-Linux
  sudo ./install.sh

It asks the same questions as the Windows setup window: plant network, records database, the optional
read-only asset and S2K databases, the port screens use (80), time zone, station key, first shift lead.
Running it again updates the suite and keeps every setting and record.

What it sets up
---------------
  /opt/production-suite            the suite, run by the "psuite" account (no login)
  production-suite-web             the web app, on 127.0.0.1:8008 only (never reachable from the network)
  production-suite-ticker          machine readings every minute, timed pulls, alerts, InfluxDB copy
  Apache (httpd)                   serves the screens on port 80; only the plant network may open them
                                   (/etc/httpd/conf.d/production-suite.conf; allow list in production-suite-allow.inc)
  firewalld                        port 80 (and 443) open to the plant network only, when firewalld is running
  production-suite-updater         installs updates from Operator Kiosk > Admin > Updates (root, on request)
  production-suite-network         keeps Apache and firewalld in step with Admin > Network (root, on request)
SELinux stays enforcing.

Screens open http://SERVERNAME/operator/ (no :8000 on Linux). Each machine's own screen is
http://SERVERNAME/station/Barbara/ (Admin > Machines has a Copy button for each).

HTTPS
-----
With a certificate for the server's name from your company's certificate authority:
  sudo ./install.sh --enable-https /path/server.crt /path/server.key [/path/chain.crt]
http:// keeps working too, for machines and gateways that push readings.

Day to day
----------
  Status:    systemctl status production-suite-web production-suite-ticker httpd
  Restart:   sudo systemctl restart production-suite-web production-suite-ticker
  Logs:      /opt/production-suite/logs (web.log, ticker.log, update.log), /var/log/httpd/production-suite-*.log
  Settings:  Operator Kiosk > Admin (Connections, Network, Alerts, Updates) from any plant screen
  Updates:   Operator Kiosk > Admin > Updates (upload the update file), or run the newer ./install.sh

Production Suite Admin and the tray icon are Windows programs; on Linux everything they do is in
Operator Kiosk > Admin, plus the systemctl commands above.

Remove
------
  sudo ./uninstall.sh       (records in MariaDB are kept; the settings file is saved in /root)

If something goes wrong
-----------------------
  Setup's log:   /var/log/production-suite-install.log
  The suite:     journalctl -u production-suite-web -u production-suite-ticker, and /opt/production-suite/logs
  Apache:        /var/log/httpd/production-suite-error.log
  A "403" page:  the screen is outside the plant network (Admin > Network, or /etc/httpd/conf.d/production-suite-allow.inc)
  SELinux:       ausearch -m avc -ts recent
