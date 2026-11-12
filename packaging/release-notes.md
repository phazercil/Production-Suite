## What's new in 1.8.7

- **Database connection tests (Admin › Connections)**
  - Fixed: repeated **Test connection** clicks could make a MariaDB server block the suite's server ("Packet sequence number wrong - got 1 expected 0"). Each test opened extra connections to see what kind of database it was and closed them without signing in, which MariaDB counts toward blocking a host. On the standard ports (3306, 1433, 5432) the test now just signs in.
  - A blocked server now says so plainly, with how to unblock it (`mariadb-admin flush-hosts` on the database server).
  - Fixed: the page no longer jumps down and loses its place after **Test connection** (or any other part of a page that's sent again); it stays where it was, with the button still focused.

## What's new in 1.8.6

**Back up before installing:** this update changes the database (Admin › Backup › Back up the database now).

- **Run screen speed chart**
  - Operators see their whole shift, start to end, plus the last half of the shift before it (shaded), with the shift's start marked and a line at the current time.
  - Shift leads and admins (Shift lead / Admin mode) see the whole day, midnight to midnight, with every shift's start marked.
  - The chart shows the machine's readings whatever log they were in, and leaves gaps empty instead of drawing a line across them.
- **Scheduler board:** hours with no shift on any line are left out of each day (e.g. midnight to 7 am), even when a shift ends at midnight or runs past it. A day can show two stretches (00:00–02:00 · 07:00–24:00 around an overnight shift); a dotted line marks where hours were left out. Crewed overtime and scheduled runs outside the shifts still show.
- **Machine schedule (Admin)**
  - Each shift has **Mon–Sun** buttons for the days it runs.
  - **Add seasonal variant** puts a copy of the shift for a range of dates (e.g. 2nd shift an hour longer in July): its own times, days, lunch, breaks and authorized downtime, without changing the main schedule. The kiosk, downtime and planned time, and the Scheduler use it on those dates.
  - **Past days keep their schedule:** a change counts from today on. The first change to a shift on a day keeps how it was, so the days before are still judged by the schedule they ran. A removed shift is kept for the days before it was removed; a new shift doesn't count for the days before it was added.
- **Machine tags in other systems:** Admin › Machines › **Add system** (e.g. PIM, asset or maintenance) adds a column to fill in each machine's tag in that system, next to its S2K process code. Saved queries (Changeover data, tooling) use `:tag_pim`, the way they use `:line_code` for the S2K code.
- **Supervisor › History: corrections.** People with the new **Correct history** permission (shift leads and admins by default; Admin › Permissions) get an **Edit** link on every shift log, downtime entry, QC check, changeover and MO run, in the lists and in a shift's report.
  - Change the fields: times, reason, notes, operator, units, QC values (pass or fail follows the specs), review, and so on.
  - **Split** a downtime entry in two at a time (e.g. a stop left open overnight), or **delete** a record.
  - Every change is kept with who, when, the old and new value and why. It shows on the record's page and the shift's report, and in Admin › Recent changes.
  - A closed shift's downtime and uptime follow its corrected downtime.
  - New **MO runs** tab.
- **Kiosk banner:** next to the clock, a running count of milliseconds since the last machine data pull (it starts again from 0 each time the server reads the machine).
- **Scheduled breaks on record:** every shift report (kiosk End of Shift and Supervisor) lists the breaks and planned time in the shift, including breaks added with **Schedule a break** and who added them. Cancelling a break that already started keeps its past times (stops then stay planned, not downtime); only the ones still to come are cancelled.
- Fixed: the End of Shift and Downtime pages failed when a shift's only stops were 0 minutes long.
- **Lines are the suite's own, not the Labor Budget's**
  - Saving the Labor Budget no longer changes the suite's lines. A line added in the budget starts in the suite with the budget's shifts and lunch; after that its name, stage, shifts, lunch and being in service are set in the suite (Admin › Machines, the Scheduler's Lines & Shifts), which are no longer locked.
  - Lines & Shifts marks where the budget plans a line differently (e.g. "⚠ Labor Budget: 1st shift only"). A line deleted from the budget stays in service and shows as not in the Labor Budget.
  - On the Labor Budget page, a machine's name and stage are locked like its color and speed.
- **Running through lunch is set per shift:** each shift a line runs has its own **through lunch** box on Lines & Shifts (e.g. relief cover on 1st shift only). Lines that ran through lunch keep doing so on every shift they run.
- **Downtime list:** each stop shows its date as well as its time.
- **Downtime for operators** covers only their own shift: the Downtime page, the "stops need a reason" alert, the badge and the pop-up leave out earlier shifts' stops. Shift leads and admins still see every stop on the line without a reason, under **Earlier shifts**.

## What's new in 1.8.5

**Back up before installing:** this update changes the database (Admin › Backup › Back up the database now).

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
