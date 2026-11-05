# Production Suite

Suite of tools for APS Filter MFG.

One Django app for the pleating floor, with five modes behind one top bar:

| Mode | Address | Who uses it |
|---|---|---|
| **Operator Kiosk** | `/operator/` | Line touchscreens on Barbara, Salvatore and any other pleater |
| **Station screen** | `/station/<machine>/` | One machine's operator view only (no other tabs, no line switcher) |
| **Scheduler** | `/scheduler/` | Planners: drag MO stages onto lines, import S2K exports, edit lines and shifts |
| **Data Kiosk** | `/data/` | Wall TV: live numbers per line plus your Grafana panels |
| **Reports** | `/reports/` | Line summaries; click to drill plant → line → shift → MO and year → month → day → hour |
| **Labor Budget** | `/labor/` | Shift leads: crews, wages and the work calendar the Scheduler plans with |

All modes share one database. When a planner places or moves a Stage 1 run, the Scheduler rebuilds that
machine's queue, and the operator sees it right away as **Up next** (with a notice in the banner).
Nothing is synced between apps any more.

All plant logic is Python:

- `hmi/engine.py` — shifts, safety checklist, minute-by-minute counting, automatic downtime, changeovers,
  QC, roll tags, maintenance tickets, notes, admin settings, Paycom / people / S2K imports
- `hmi/ticker.py` — the per-minute machine reading loop (demo replay, URL polling or pushed readings)
- `scheduler/engine.py` — run lengths from the cost sheet, shift calendars with breaks, changeovers,
  downstream stages paced so they never outrun Stage 1, and the Stage 1 queue written onto the MOs
- `hmi/integrations.py` — Grafana, Paycom, S2K, people list, webhook, read-only asset database
- `hmi/influx.py` — InfluxDB: machine data by query, and the copy of the suite's records
- `plant/updates.py`, `tools/updater.py` — updates from Admin › Updates or the update folder

The browser JavaScript (`static/…`) only draws screens from the server's JSON and sends taps back.

## Try it (demo, SQLite)

```bash
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo --yes
python manage.py runserver 0.0.0.0:8000
```

Open http://localhost:8000 on the same computer. (Other devices must be on 192.168.100.x, or run
`python manage.py network --set <your range>` first.) Demo PINs: J. Ortiz `1111`, M. Lee `2222`, K. Patel `3333` (maintenance),
S. Lead (shift lead) `4444`, Plant Admin `9999`. `runserver` also runs the machine ticker, so Barbara and Salvatore replay the PPM sheet
one reading per minute.

Run the checks with `python manage.py test tests`.

## Plant network only

The suite is locked to the plant network, **192.168.100.0/24** (192.168.100.0 – 192.168.100.255) unless changed,
in five layers:

- **The app** answers only devices on the plant network, plus the server itself. Anything else gets "403" and
  is written to the log. Forwarded-for headers are ignored, so an address can't be faked that way.
- **Windows Firewall** (set by the installer) opens the port only to the plant network, and only while Windows
  treats the network as Domain or Private, never Public.
- **The screens** load nothing from the internet. Fonts are served by the suite, and every page tells the
  browser it may only talk to the suite itself and, on the Data Kiosk, to a Grafana server on the plant network.
- **The server's own connections** (machine feeds, S2K, people list, webhook, Grafana, InfluxDB, MariaDB, asset
  database) must point at the plant network. Other addresses are refused when typed in and again when
  used; redirects off the network are refused, and these connections never go through a web proxy.
- **Startup**: the web app and ticker refuse to start if the MariaDB server is not on the plant network.

**Changing the plant network:** Operator Kiosk › Admin › Network (shift lead PIN). Add or remove ranges or single
addresses and save; the Windows Firewall rule is updated at the same time, and the page shows whether that worked.
Only private ranges can be entered (10.x, 172.16–31.x, 192.168.x, 100.64–127.x), never the internet. A save that
would lock out the screen making it is refused. The setting is kept in the database; the installer's answer
(`PLANT_NETWORKS` in `.env`) is used until it is first changed there.

**Locked out?** The server itself is always allowed: open `http://localhost:8000/operator/` on it, or run
`python manage.py network --set 192.168.100.0/24` (`python manage.py network` shows the current setting).

**Exceptions you choose to set up:** Paycom (operator import), Twilio (text alerts) and, only if you tick
"the mail server is on the internet" in Admin › Alerts, an internet mail server; GitHub updates (Admin › Updates);
and, only if you tick "This database is outside the plant network" for it in Admin › Connections, an asset, S2K or
machine data database on a cloud server or at another site (each database has its own tick box, saved as
`ASSET_DB_INTERNET`, `S2K_DB_INTERNET` or `MACHINE_DB_INTERNET` in `.env`). Each is used only when set up;
leave them out to keep the server fully offline.

## Installing on a Windows server

Use the installer package (`ProductionSuite-Setup.zip`): extract it on the server and double-click **Setup.exe**.
A setup window walks through the plant network, the MariaDB connection (with a *Test connection* button), the
optional read-only asset and S2K databases, the port and the first shift lead, then installs with a % progress bar
and a details log. Everything it needs is in the download (no pypi.org access needed); it only downloads Python
if the server has none. It registers the suite to start with Windows and opens the port to the plant network only.
Full steps are in its `README-INSTALL.txt`. Running Setup.exe again installs an update;
`Uninstall Production Suite.bat` removes it (records in MariaDB are kept).

For unattended installs: `powershell -ExecutionPolicy Bypass -File installer\install.ps1 -AnswersFile answers.json`.
Both run `python manage.py install_plant`, which works the same way on a Linux server.

## Installing on a Linux server (Rocky / AlmaLinux / CentOS Stream / RHEL 9)

Use `ProductionSuite-Linux.tar.gz` (made by `packaging/build.py`, or the Release workflow):

```bash
tar xzf ProductionSuite-Linux.tar.gz && cd ProductionSuite-Linux
sudo ./install.sh                     # add --with-mariadb to put MariaDB on this server too
```

It asks the same questions as the Windows setup window and sets up:

- `/opt/production-suite`, run by the `psuite` account (no login); settings in `.env` (mode 600).
- **systemd**: `production-suite-web` (Waitress on **127.0.0.1:8008 only**) and `production-suite-ticker`.
- **Apache (httpd)** in front, on port 80: it serves the screens' files from `/var/www/production-suite/static` and
  passes everything else to the suite. Only the plant network may open it (`Require ip`, from
  `production-suite-allow.inc`). Apache drops any `X-Forwarded-For` a client sends and adds the real address; the
  suite trusts that header only from 127.0.0.1 and only with `BEHIND_PROXY=1`.
- **firewalld** (when running): Apache's port open to the plant network only.
- **SELinux stays enforcing**: the suite uses port 8008, which Apache may connect to, and Apache's files are under
  `/var/www`.
- Two root jobs started by systemd path units, so the suite itself never needs root:
  `production-suite-network` keeps Apache and firewalld in step with Admin › Network, and `production-suite-updater`
  installs updates from Admin › Updates. Root runs only its own copies in `/usr/libexec/production-suite` (with the
  system Python and the trusted update keys), never files the suite's account can change.

HTTPS: `sudo ./install.sh --enable-https server.crt server.key [chain.crt]` (adds the 443 site; http keeps working
for machines that push readings). Running `install.sh` again updates in place and keeps settings and records.
`uninstall.sh` removes it (records in MariaDB are kept). Full steps: `packaging/linux/README-LINUX.txt`.
Production Suite Admin and the tray icon are Windows-only; on Linux use Operator Kiosk › Admin and `systemctl`.

### By hand, on any Linux

1. **MariaDB** (10.5 or newer): create a database and user, then copy `.env.example` to `.env` and fill in `DB_*`,
   `DJANGO_SECRET_KEY`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, and set `DJANGO_DEBUG=0` and
   `TICKER_IN_PROCESS=0`. With `DJANGO_DEBUG=0` the server refuses to start until a real secret key is set.
2. `pip install -r requirements.txt`, then `python manage.py install_plant` (or `migrate`, `setup_plant --lead-pin …`
   and `collectstatic`).
3. Run `python serve.py web` and `python serve.py ticker` as services (see `packaging/linux/templates`).
   Only one ticker counts at a time; a second one stands by and takes over only if the first stops.
4. In **Scheduler › Lines & Shifts**, add your machines and shifts. In **Operator Kiosk › Admin** (shift lead PIN),
   set each pleater's machine data feed, colors and training, and the connections below.

To start from the demo instead, use Admin › Backup › *Clear demo data and go live*: machines, people,
shifts and settings stay; MOs and records are removed.

### Screens

- A machine's own screen: `http://server:8000/station/Barbara/` shows only Barbara's operator view. Admin ›
  Machines lists each machine's address with a Copy button.
- A line touchscreen: `http://server:8000/operator/?line=Barbara&kiosk=1` (`kiosk=1` hides the mode bar
  on that screen; `kiosk=0` brings it back).
- The wall TV: `http://server:8000/data/?kiosk=1`.
- If `STATION_KEY` is set, open each screen once with `&key=YOUR_KEY`; it is remembered.

### Machine data

Per pleater in Admin › Connections:
- **Read from URL** — the server polls a URL (Node-RED, a PLC gateway) that returns JSON such as
  `{"ppm":190,"count":48211}`; field names can be dotted paths.
- **Pushed to server** — the gateway posts the same JSON to `POST /api/feed/<machine>` with header
  `X-Feed-Key: <FEED_KEY>`.
After 2½ minutes without data the kiosk alarms and nothing is recorded, so a dead feed never looks like downtime.
Readings that are not numbers are refused (`400`), and a problem with one machine's data never stops the others
from being counted.

### Grafana

Records land in tables Grafana can read directly: `hmi_ppm` (one row per machine per minute),
`hmi_downtime`, `hmi_qc`, `hmi_shifts`, `hmi_mo_runs`, `hmi_tickets`. Add a read-only MySQL data source,
build a dashboard with a `line` variable (`SELECT DISTINCT line FROM hmi_ppm`), allow embedding in
`grafana.ini`, then enter the URL, dashboard UID and panel IDs in Admin › Connections. The same page
picks the panels the Data Kiosk shows for every line.

Speed panel query:
```sql
SELECT t AS time, ppm AS "P/m" FROM hmi_ppm WHERE line = '$line' AND $__timeFilter(t) ORDER BY t
```

**Keeping Grafana in step with the suite's downtime rules.** The suite does not count planned time as
downtime: Scheduler breaks and lunches, and the start-up minutes after each shift's scheduled start
(Admin › Rules). Grafana can do the same from these columns:

- `hmi_ppm.planned` is 1 for a minute inside planned time, 0 otherwise.
- `hmi_planned` lists each planned stretch (`line`, `start`, `end`, `kind` = Lunch, Break, Start-up).
- `hmi_downtime` never contains planned time.

Uptime the way the suite counts it (5 = the "stopped below" P/m in Admin › Rules):
```sql
SELECT $__timeGroupAlias(t, '1h'),
  100 * SUM(ppm > 5) / NULLIF(SUM(ppm > 5 OR planned = 0), 0) AS "Uptime %"
FROM hmi_ppm WHERE line = '$line' AND $__timeFilter(t) GROUP BY 1 ORDER BY 1
```
Admin › Connections › Grafana shows these and more, filled in with your current rules, each with a Copy button.
Use the `hmi_planned` query as a dashboard annotation to shade breaks on every time series panel.

To let Reports read your Grafana panels, add a Grafana service-account token (Viewer) in Admin › Connections.

### InfluxDB

Admin › Connections › InfluxDB (2.x with Flux and a token, or 1.x / 3.x through InfluxQL). Two optional jobs:

- **Machine data from InfluxDB.** Set a machine's source to *InfluxDB query* under Machine data and write the
  query (a sample is filled in; `{machine}` is replaced with the machine's name). The newest values are used each
  minute; Flux results are read by `_field` (`ppm`, `count`), InfluxQL results by column. A reading older than
  2½ minutes counts as no data, like any other feed.
- **A copy of the suite's records**, for Grafana dashboards built on InfluxDB. Set *Copy suite records to bucket*;
  every minute the suite writes `suite_minute` (per line and minute: `ppm`, `units`, and 1/0 fields `run`,
  `down`, `planned`, `counted`), `suite_downtime` (`minutes`, `reason`, `mo`) and `suite_planned` (`kind`,
  `minutes`). `down` and `counted` follow the suite's rules, so planned breaks, lunch and start-up never count as
  downtime. If InfluxDB is down, the copy catches up when it's back. The Grafana queries panel shows Flux /
  InfluxQL examples once this is on.

Queries the suite runs can only read: Flux `to()` and imports that reach other systems (http, sql, …) are refused,
as are InfluxQL `INTO`, `DROP`, `DELETE` and the like. Like every connection, InfluxDB must be on the plant
network. Settings are kept in `.env` (`INFLUX_*`).

### Downtime reasons and alerts

- After a line has been stopped for the minutes set in Admin › Rules (planned time doesn't count), the operator
  is asked why. Operators pick from the list in Admin › Downtime reasons; maintenance and shift leads can type
  their own reason or change one someone else picked (the change is noted).
- Admin › Alerts: people to email or text when a line has been down for their chosen number of minutes, and
  optionally when it runs again. Email uses your mail server (plant network unless you tick "on the
  internet"); texts use Twilio, or a carrier's email-to-text address.

### Incoming data (machine vision QC and other systems)

Admin › Connections › Incoming data. A source either posts JSON to `POST /api/ingest/<name>` with header
`X-Source-Key: <key shown once when you create it>`, or the server collects JSON from its URL every N seconds.
Map the fields once (time, line, part, MO, length, width, pleat height, side bands, pass/fail; millimetres
are converted). Without a pass/fail field, results are judged against the part's drawing specs. QC results
show on the kiosk's QC Log and in Reports, and land in `hmi_qc_auto` for Grafana; other readings in `hmi_inbound`.

### Labor Budget and the Scheduler

The Labor Budget is kept in step with the rest of the suite. Every machine in Scheduler › Lines & Shifts is a
line in the plan: its name, stage, color and speed (filters per hour: rated PPM × 60 ÷ the pleat count of the
parts queued on it, or the line's rate) come from the suite and can't be changed in the plan. Which shifts a line
runs is one setting: change it in either place and both follow. The suite's shifts are the plan's shifts, each
with its main crew schedule (times, work days and lunch from the shift's breaks named "lunch"), and the plan's
normal work week is the days those shifts run. Crews, positions, wages, extra crew schedules, holidays and pay
rules stay the plan's own, and lines it adds itself are labor-only (Packaging, Non Production, Backup). Crew
names are offered from the Team list; a name that isn't on it is outlined in red.

The Labor Budget (shift leads only) holds crews, wage rates and the work calendar. The Scheduler plans
only on work days and staffed shifts from it (holidays and days off are shaded on the board) and shows
labor cost per run to shift leads. Turn this off in Scheduler › Planner if you need to.

### Connections after installation

Admin › Connections › Databases and accounts changes the asset and S2K databases, Paycom, InfluxDB, Grafana and the
other accounts without reinstalling. Passwords are kept in the server's `.env`, never in the database, and
take effect right away.

### Changeover instructions from the asset database

Set `ASSET_DB_*` in `.env` (read-only account). In Admin › Changeover data, build or paste a SELECT for
the steps, the machine settings and the drawing specs using `:part`, `:machine`, `:from_part`, `:mo`,
`:line_code`. During a changeover the server runs it for the new part; if the database is down, operators
get the last steps saved for that part, then the standard checklist.

### Paycom

Set `PAYCOM_SID` and `PAYCOM_TOKEN` (and register the server's IP with Paycom). Admin › Connections can
import automatically and filter by department and position. Use *Show Paycom fields* to confirm the field
names your account returns; if department, position or badge come through blank, set
`PAYCOM_FIELD_DEPT / _POSITION / _BADGE`. The import never changes roles, training or PINs.

### Machine data

Each pleater gives three values every minute: **pleats per minute**, **filters cut** (the machine's counter) and
the **recipe** loaded in the machine. Admin › Connections › Machine data sets, per machine, where they come from:

- **Database table** — the database the machines log to (Admin › Connections › Databases and accounts ›
  Machine data database, or `MACHINE_DB_*` in `.env`; the asset database works too). Pick the table, the column
  that tells which row is newest (a time stamp or an auto-number ID), and, when one table holds every machine,
  the column that names the machine and this machine's value. Then pick the column for each value. The newest
  row is read once a minute through a read-only login; table and column names only ever come from the
  database's own list. If no new row arrives for the minutes you set (default 3), it counts as no data.
- **Read from URL**, **Pushed to server** or **InfluxDB query** — the same three values as fields of the reading.

**Uptime (run signal)** is optional: a second place that says whether the machine is running — another
field or column of the same reading, or a column of a separate table. High/Low, 1/0, true/false, on/off and
run/stop are understood, and it can be set to running-when-Low. When set, uptime, downtime and stop alarms
follow the signal instead of the speed; a minute where it can't be read falls back to the speed (Admin › Rules ›
stop speed). Every minute is stored with `run` (1 = running) and `recipe` in `hmi_ppm`, so Grafana uses the same rule.

### Maintenance tickets with pictures

Call maintenance (run screen) or Maintenance › New ticket opens a ticket. **Take a picture** opens the tablet's
camera; **Choose a picture** picks one already taken. Up to 6 pictures per ticket; more can be added later from the
Maintenance page. The screen shrinks each picture to 1600 px JPEG before sending (a few hundred KB) and the
server checks it is a real picture. Pictures are kept in the records database with the ticket (`hmi_ticket_photo`),
so backups include them. Pictures on tickets closed longer than Admin › Rules › *Keep pictures on closed
maintenance tickets* (default 365 days, 0 = keep) are removed once a day. Works over plain HTTP.

### Machine errors during downtime

Admin › Connections › Machine data › *Errors and alarms*, per machine:
- **Fields of the same reading** (or columns of the same table): an error code and optional message. One entry
  is kept from the minute a code appears until it changes or clears. 0, blank, OK, none and similar mean no error;
  add your own "no error" values per machine.
- **The machine's alarm log table**: each new row becomes one entry, with its own time when the table has a time
  column. Reading starts from the newest row (old history isn't imported).

Each downtime then lists what the machine reported during it (and in the 2 minutes before), on the Downtime page
and in the "Why did the line stop?" prompt. A code still showing appears in the run screen's alerts and on Status.
Stored in `hmi_machine_errors` for Grafana (a ready query is under Admin › Connections › Grafana).

### Outside databases: MariaDB/MySQL, SQL Server or PostgreSQL

The asset, S2K and machine data databases can each be **MariaDB / MySQL, Microsoft SQL Server or PostgreSQL**.
Admin › Connections › Database type is *Auto-detect* by default: *Test connection* tells them apart from the
server's first answer on the network (no login needed for that), signs in read-only, and says which kind it
found; *Save* keeps the kind (`ASSET_DB_KIND`, `S2K_DB_KIND`, `MACHINE_DB_KIND` = `mysql`, `mssql` or `postgres`).
Every query the suite writes (S2K, changeover data, tooling, machine readings, alarm logs, the connector
builder, table previews) uses that database's own SQL (quoting, `TOP`/`LIMIT`); tables outside the default
schema (`dbo` / `public`) show as `schema.table`. The drivers ship with the suite: PyMySQL, pymssql (SQL
Server) and pg8000 (PostgreSQL). Use a login that can only read; the suite also only ever sends single SELECTs,
and MariaDB/MySQL and PostgreSQL sessions are opened read-only.

### S2K

Admin › Connections › S2K reads open MOs from one of:
- **The S2K database (read-only)** — set up with the installer (or `S2K_DB_*` in `.env`), then write a SELECT that
  returns `mo`, `item`, `qty` and optionally `due`, `process`, `mfg`, `hot` (rename with AS). *Show tables* lists
  what's there and *Build the query for me* writes the SELECT from the table and columns you pick (plus an
  optional "only rows where status = OPEN" filter). *Run test* previews the rows, *Save on server* stores it
  for timed and manual pulls.
- **An export URL** returning JSON
  (`[{"mo":309300,"item":"MG4062HP","qty":2400,"due":"2026-04-08","process":"BARB"}]`).

You can also import Excel/CSV exports on Scheduler › Orders. New MOs wait unscheduled in the Scheduler.

**The S2K and asset databases are never written to.** The installer only signs in and reads the table list (no
`CREATE DATABASE`, no test table). The suite opens both connections read-only
(`SET SESSION TRANSACTION READ ONLY`), sends only a single SELECT (row locks such as `FOR UPDATE`, `INTO OUTFILE`
and the like are refused), and its tables and migrations go only to its own records database. Give these
connections SELECT-only accounts as well; the installer warns if the account could change data.

## Production Suite Admin and the tray icon (on the server)

Setup puts **Production Suite Admin** on the server's desktop and in the Start menu (`desktop\SuiteAdmin.exe`),
and a **Production Suite tray icon** (next to the clock) that starts at every login for every user.

- **No Windows administrator login is needed.** Both run as whoever is signed in and talk to the suite's
  **control service** ("Production Suite Control" scheduled task, system account, `serve.py control`). It listens
  on 127.0.0.1 only, so only programs on the server itself can use it, and it refuses requests a web browser could
  send. Its port is 8765 (`SUITE_CONTROL_PORT` in `.env`).
- **Status, Start, Restart and the logs** are open to anyone signed in to the server. **Stop and every check or
  change of a setting** ask for a **shift lead PIN** (the same PINs as the kiosks; 5 wrong PINs pause it for a
  minute; a login lasts 20 minutes idle). Changes are in the audit log as "<shift lead> (server desktop)", and
  `logs\admin.log` notes which Windows user made each one.
- **Status**: whether the web app and the minute ticker are running and answering, the version and revision,
  Start, Stop and Restart, and the last lines of each log.
- **Connections**: the records database (MariaDB; it can create the database and user with an administrator
  login used once, builds the tables, and puts the old settings back if that fails), the read-only S2K and asset
  databases, InfluxDB, Paycom, the Grafana token, the machine feed key, the alert mail server and Twilio.
- **Network and server**: the plant network (and its firewall rule), the port (it moves the firewall rule and
  the desktop shortcuts too), the plant time zone and the station key.
- **Tray icon**: green while the suite runs, amber while it starts, red when stopped (with a balloon when that
  changes), grey if the control service can't be reached. Right-click for Production Suite Admin, the Operator
  Kiosk, Start and Restart; double-click opens Admin.

Every change goes through the same checks as Operator Kiosk › Admin, nothing is saved if a check fails, and blank
passwords keep the saved ones. The window's own log is in `%LOCALAPPDATA%\ProductionSuite`. Both are part of the
program files, so updates keep them current (an update closes open tray icons; they come back at the next login).

## Updates without reinstalling

From 1.3.0 on, updates come as a small signed file, `ProductionSuite-Update-<version>-r<revision>.zip`:

- **Admin › Updates** (shift lead): choose the file and when it installs (right away, when no line is
  running a shift, or at a set time). Or IT copies it into `C:\ProductionSuite\updates\inbox` (the folder can be
  shared); Admin › Updates sets whether those install right away, when no line is running, or only when a shift
  lead says so.
- The suite checks the file first: signed with the suite's update key, every file intact, a newer revision, and
  carrying the Python packages if it changes them. Anything else is refused before anything stops.
- The "Production Suite Updater" scheduled task (added by Setup.exe) then stops the suite, backs up the program
  files, puts the new ones in place, updates the database tables, starts the suite and waits for it to answer
  with the new revision. If any step fails, it puts the old files back, takes the tables back, and starts the old
  version again. Progress shows on Admin › Updates and in `logs\update.log`.
- **Go back** on Admin › Updates returns to the previous version (the last 3 are kept in `backups\`). Records
  made since are kept.
- Open screens load the new version by themselves within a minute (a screen where someone is typing waits).

The version and revision show in the bottom-right corner of every page, and at `/api/version`.

**Linux servers and "Check for updates".** The update file the Windows release job builds contains code-signed
Windows programs, so it needs the code-signing certificate. Linux servers get their own update file,
`ProductionSuite-Update-<version>-r<revision>-linux[-with-packages].zip`, which the release's Linux job builds and
attaches whenever the repository has the `UPDATE_SIGNING_KEY` secret (Settings › Secrets and variables › Actions;
the whole `production-suite-signing-key.txt`). It carries the Linux Python packages when `requirements-windows.txt`
or `packaging/wheels` changed since the previous release. Admin › Updates › Check for updates on a Linux server
finds it; Windows servers never take a `-linux` file. Without the secret, Linux servers update with `get.sh`.

**The Labor Calculator on its own.** The Labor Budget page is the Labor Calculator (its own repository,
`phazercil/Labor-Calculator`) with the suite's sync, A/B budgets and login added. With updates from GitHub set
up, **Admin › Updates › Labor Calculator** checks that repository for a newer calculator and installs it without
a suite release: it downloads the offline build (`desktop/app/index.html`, made by the calculator's
`tools/build_desktop.py`), strips the names and wages stored in it, adds the suite's changes and switches the page.
If a calculator change moved the parts the suite hooks into, it is refused and the page stays as it was. **Undo**
goes back one install; the page that came with the suite can always be restored. After a suite update the
installed calculator is built again with the new suite's changes (or, if it no longer fits, the shipped one is
used and the panel says so). The GitHub token needs Contents: Read-only on both repositories. Set
`LABOR_CALC_REPO` in `.env` to use another repository.

**Building an update** (whoever holds the signing key, kept off the server):
raise `REVISION` in `suite/version.py` (and `VERSION` for a release), then
`python tools/make_update.py --key production-suite-signing-key.txt --notes "what changed"`. Add
`--packages <wheels folder>` only when `requirements-windows.txt` changed. Setup.exe is still used for a new
server, a new Python, or if the signing key is ever lost (`--new-key` makes a new one; put its public key in
`plant/update_key.py` and install that build with Setup.exe).

## Layout

```
suite/       settings (reads .env), URLs
plant/       shared models: Machine, Shift, PartSpec, CostItem (cost sheet), Person, MO, PlantSettings; demo data
hmi/         Operator Kiosk engine, ticker, integrations, JSON API, records (hmi_* tables)
scheduler/   Job model, scheduling engine, imports, JSON API
datakiosk/   wall screen
reports/     Reports page: summaries with drill up/down, Grafana reader
labor/       Labor Budget (built from labor/source with tools/build_labor.py)
templates/, static/   the screens and the shared shell
packaging/   Windows installer, Linux installer (packaging/linux), code signing, build, CI checks
```
