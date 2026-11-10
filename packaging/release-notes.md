## What's new in 1.8.5

- **Scheduler board**
  - A dragged MO now starts where its left edge (the blue line) is, not where the mouse pointer is.
  - Days with no shift on any line (weekends, holidays) shrink to a thin strip, so the board is mostly production time. Week and month view stretch to fill the screen.
  - Easier to read: one line of detail per machine (hover the name for speed, scrap, downtime and crew), part counts only on the stage rows, flat shading for off-shift time, and short breaks left out of week and month view (the day view still shows them). Lines with nothing scheduled are dimmed.
- **Supervisor › History** (the Supervisor tab, for people with Open Supervisor): every shift log, QC check, downtime entry and changeover, filtered by line, dates, operator, MO and result (failed QC, waiting for review, downtime with no reason, ...). Each list downloads as a CSV. A shift log opens as its full End of Shift report (MOs, QC checks with every value, downtime, changeovers with who did what, rolls, tickets and notes), with Print and Copy report.
- **Changeover pass over:** at the end of a shift, **Pass over** on the changeover screen hands a changeover to the next operator or shift with a note ("where you left off"). Each person's and shift's part is kept: their time, the steps they checked off (every step shows who checked it and when), and any wait with nobody logged in. The next operator sees the note and picks up where it was left; the End of Shift report lists every piece. Ending a shift in the middle of a changeover passes it over, so it's never lost. The changeover timer shows the whole changeover's time.
- **Shift logs:** an operator who clocks in a few minutes early for the next shift is no longer split off at the shift change and asked "working into 2nd shift?".

## What's new in 1.8.4

**Back up before installing:** this update changes the database (Admin › Backup › Back up the database now).

- **Connector builder**
  - **Source cards:** each card has a title, the columns you pick and its own row filter. A table can have several cards, e.g. one per machine.
  - **Test data** on every box shows what its wires read now, without saving.
  - **Tag tables** (one value per row with a tag, e.g. Salvatore_Speed / Salvatore_Count): the ⚑ on a machine input reads only its tag's rows.
  - Filter values offer a dropdown of the values in the column.
  - Taking every wire off a box and saving disconnects it.
  - A **second run signal**, named on the box, for two machines that run together: the line runs only while both do.
- **Scheduler**
  - Every shift shows on the board, shaded (1st lighter than 2nd).
  - The Orders list has a section per stage.
  - A dropped MO lands with its start where you drop it.
  - **Split** an MO run at a time of day, when the operator ends it, or by quantity.
- **Machines**
  - Speed in pleats per minute, linear mm per second or parts per minute.
  - With a counter, parts count only when the counter goes up.
  - **The log always runs**, with or without a shift or an operator. It splits at each shift's start and end, so every stretch of time belongs to a shift (or "No shift", which is logged but never counted as downtime). The operator who starts their shift takes over what was logged in it.
- **Kiosk**
  - A pop-up when one of the operator's machines stops.
  - When a shift ends with the operator still running, the kiosk asks "[Name], working into 2nd shift?" — Yes keeps them on the new shift's log.
  - **Change operator** hands the machine to the next person who logs in; the previous operator keeps everything up to that moment.
- **Admin › Machine schedule** (was Shifts & breaks) owns the shifts: times, days, lunch and authorized downtimes (Add start-up, Add shut-down). The Labor Budget keeps lines and crews but no longer changes shift times.
- **Downtime fix:** another shift's lunch, authorized downtime and hours with no shift scheduled are no longer logged as downtime when a run stays open across shifts or overnight.
- **Admin › Backup › Back up the database now**, with downloads.

## What's new in 1.8.3

- **Updates on Linux servers:** Admin › Updates › Check for updates now finds new releases on a Linux server. Releases carry a signed Linux update file (with the Python packages when they changed) when the repository has the UPDATE_SIGNING_KEY secret.
- **Connector builder:** every table in use has its own card on the left, so saved wires always show, also after switching tabs (fixes a table showing as picked with no columns). Add a source shows another table's columns. Saving a box keeps the other boxes' unsaved wires.
- **One table for every machine:** a machine box can copy another machine's wires; machines sharing a table each need "Which machine" and their own value.

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
