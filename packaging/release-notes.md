## What's new in 1.8.2

- **Labor Budget is the master of lines and shifts:** Admin › Machines/Shifts and the Scheduler follow the Labor Budget's lines, shifts, times and breaks (budget A). Those fields are locked there while it's on.
- **Labor Calculator updates from GitHub:** Admin › Updates › Labor Calculator installs a newer calculator straight from its own repository (offline build), with Undo and a way back to the shipped one. The GitHub token needs read access to both repositories.
- **Databases other than MySQL/MariaDB:** the database type (MySQL/MariaDB, SQL Server, PostgreSQL) is detected when you test a connection, and queries, the column list and the connector builder use that database's own syntax. New drivers: pymssql and pg8000 (this update includes Python packages).
- **Connector builder wire view:** every column is a dot on the left, every value the suite needs is a dot on the right; drag a wire between them to link, click a wire to unlink.
- **Admin › Connections:** a typed password stays in the box after **Test**, so it can be saved without typing it again.

## What's new in 1.8.1

- **Scheduler:** every shift ticked for a line in Lines & Shifts counts. The Labor Budget no longer switches off a shift it has no crew for, which made 2nd shift show as off-shift.
- **Scheduler:** the board leaves out hours no shift covers (e.g. shows 06:00–23:00 instead of midnight to midnight).
- **Scheduler:** drag an order by its whole card, not just the stage chip.
- Screens pick up new styles and scripts right after an update. In 1.8.0 a browser could keep the old style file, which stopped the Admin page from scrolling.

## What's new in 1.8.0

**Operator Kiosk**
- **Supervisor button** (bottom left): a shift lead, Admin or maintenance person takes over the screen for 10 minutes to fix finished entries such as downtime reasons or QC checks.
- **QC checks** set per stage in Admin › QC checks. The first check is due as soon as the machine runs after a changeover. A check more than 10 minutes late needs a reason. Out-of-spec checks wait for a supervisor to Approve or Disapprove; disapproving takes the scrapped filters off the count.
- **Run screen:** the next changeover within a day with its screws, helix, media and side bands; the current MO's tooling and how the part has run before (scrap rate, pieces/hr, speed, downtime) on this machine and on any machine; the last 24 hours of shift reports.
- **Schedule:** a checkered-flag finish time on every MO. A live changeover shows "Currently Changing Over" and turns red when it runs over. Lunch shows whether each line runs through it.
- **Scheduled extra breaks** (e.g. 2:00 pm for 15 min on a hot day): the schedule pauses for them and stops during them aren't downtime.
- **Notification bar:** each machine's screen shows what changed for that machine (its queue, breaks, maintenance, QC).
- **Maintenance:** repair notes, Start work / Mark complete, and open / in progress / completed counts.
- **Downtime:** shift running time and overall shift downtime at the top.
- **Team:** an Edit dialog that saves only on Save.

**Admin**
- **Permission manager:** allow or deny each feature by role or by person.
- **Connector builder:** drag database columns onto what the suite needs (machine speed, counts, run signal, S2K MOs, changeover data, tooling).
- Databases outside the plant network can be allowed one at a time (Connections).
- Paycom: import only the position titles you pick.
- Supervisor and Maintenance sections in the top bar (Maintenance to be built out later).

**Labor Budget:** the new calculator with A/B budgets, and its own address (/budget/) for people who only have labor permission.

**Scheduler:** per-line "Runs through lunch" setting.

**Windows server:** ProductionSuite-Setup.zip (extract, run Setup.exe). An existing install can upload ProductionSuite-Update-….zip in Operator Kiosk › Admin › Updates.

**Linux server** (Fedora, Rocky / Alma / CentOS Stream / RHEL 9): install or update straight from this release with `packaging/linux/get.sh`. See README-LINUX.txt, or download ProductionSuite-Linux.tar.gz and run `sudo ./install.sh`.
