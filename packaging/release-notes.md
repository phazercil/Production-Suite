## What's new in 1.9.0

**Back up before installing:** this update changes the database (Admin › Backup › Back up the database now).

- **Quality (new section in the top bar)**
  - **Quality report:** every quality problem from the floor in one list: QC checks out of spec (what was out and by how much against the drawing and its tolerance, the scrap, the review or Process Out), machine vision fails and quality holds. It shows totals (fail rate, waiting for review, processed out), the measurements most often out, the parts with the most problems and processed-out filters by quality issue. Filter by line, dates, kind and part, or download a CSV.
  - **QC checks** (moved from Admin): what each stage's QC check asks, plus the list of quality issues Process Out offers.
  - **Parts & specs** (moved from Admin): the whole table is edited and saved at once. **Paste a list of part numbers to import them from the asset database**; each part's dimensions, rev and drawing link are read through the Connector builder, and parts it doesn't have are named.
  - **Tolerances:** how far under and over the drawing each measurement (length, width, pleat height, side band height) may be, the same for every filter. Under and over can differ (e.g. −0.06 / +0.03). A blank row keeps each part's own tolerance. Operator QC checks, the in/out marks as values are typed, machine vision results, the drawing and Part Info all use them; past checks keep their result.
  - New permissions **Open Quality** and **Quality settings** (shift leads and Admins by default; Admin › Permissions).
- **Always the latest dimensions:** a part's drawing is read from the asset database again whenever a QC check is saved or corrected, and when the QC check, drawing, changeover or Part Info screens open (at most once a minute per part). If the database can't be reached, the dimensions on file are used.
- **Units of measure:** Part specs (Connector builder) has a **Unit of measure** input. Wire the asset database's unit column (e.g. `uom`) and each dimension is converted from its own row's unit (mm, cm or in) to inches. Without one, Quality › Parts & specs says which dimensions the asset database keeps in millimetres.
- **No more QC checks against made-up dimensions:** a QC check needs a part. With no MO running, the screen says so instead of judging against estimates. Supervisor › Edit can now set the part of a past check that was saved without one, and it's judged again against that part's drawing.
- **Process Out (Supervisor and Quality):** a failed QC check has a **Process out** button that asks how many failed the QC and what the quality issue was (from a list, with details). Those filters come off the shift's and MO's count onto the MO's scrap, the line is told, and it shows in the History, the shift report, the kiosk QC log and the quality report.
- **Supervisor › Line status** (the first tab): a card for each line, showing running, stopped or in a changeover, who's logged in, the MO with its quantity made of the order, the speed, and this shift's count, uptime and downtime. It refreshes every 30 seconds.
- **Part Info (Operator Kiosk, under Schedule):** the drawing and the filter's dimensions, each with its tolerance and the smallest and largest it may measure, for the part running now. You can switch to the next parts on the line's schedule or type any filter number. It has **Open drawing** and **Get updated drawing**.
- **No MO? Run by filter number:** when a line has no MO to run, Change over › Next MO has **No MO? Run by filter number - Supervisor**. A supervisor (using the Supervisor button's PIN if the operator can't) enters the filter number, how many if known, and why. It runs as an MO of its own, numbered NOMO-<line>-<date>-<time>, so counts, QC and reports work as usual. The planners are told and it's kept in the audit log. It needs the new **Run without an MO** permission (maintenance, shift leads and Admins by default).
- **Downtime alerts:** each person has an **On / Off** switch in Admin › Alerts.
- **S2K open MOs from the S2K API:** Admin › Connections › S2K schedule › Read open MOs from › **The S2K API**. Enter the address, Company and Location (sent as `company=` and `location=`), and the token, which is kept in the server's `.env` and sent as `X-Api-Token`. **Test** shows what it sends. It's one opt-in internet connection, used only for that address. Each open MO and sequence comes in as an MO (e.g. 318555-002) with the quantity still due and the current due date.
- **S2K process, MFG and hot flag removed:** which line runs an MO is set in the Scheduler, so the S2K query builder and the Schedule page no longer show them.

## What's new in 1.8.9

**Back up before installing:** this update changes the database (Admin › Backup › Back up the database now).

- **Drawing file (Admin › Connector builder)**
  - A new **Drawing file** box reads the part's drawing rev and a link to the drawing (PDF or image, e.g. on AWS) from its own table, so it can live somewhere other than Part specs. When a part has a row per rev, wire **Newest first by** to a date (or rev) column and the newest one is used. Its rev wins over Part specs' rev.
  - **Get updated drawing** keeps the link with the part, and operators get an **Open drawing** button on the drawing, changeover and QC screens and next to the drawing rev on the run screen. It opens in a new tab. Only web addresses (http/https) are kept; signed AWS links are refreshed every time the drawing is fetched again.
- **Connector builder**
  - **Drawing rev** is no longer required to save Part specs (it can come from Drawing file instead).
  - Fixed: when a Save was refused (something still missing or wrong), the box came back as if saved, and the next automatic refresh wiped the wires. It now stays unsaved with its wires and the reason, until it's fixed and saved or undone.

## What's new in 1.8.8

- **Part specs from an attribute table (Admin › Connector builder)**
  - Part specs (drawing) can now read a table that keeps one value per row with a code saying which spec it is, like the asset database's `part_attribute` (`value`, with `PAID` telling length, width and so on apart). Wire `value` to each spec, then click ⚑ on it and pick the rows it reads, e.g. `PAID = 30` for Length. Once one spec has its column, the next ⚑ offers the same one, so you only type the codes. The suite folds those rows into one row for the part.
  - **Get updated drawing** now reads numbers stored as text with units (e.g. "9.25 in"), and skips values with no number in them instead of failing.
  - Fixed: clicking **Test data** on a box with unsaved wires could let the next automatic refresh wipe them.

## What's new in 1.8.7

- **Database connection tests (Admin › Connections)**
  - Fixed: repeated **Test connection** clicks could make a MariaDB server block the suite's server ("Packet sequence number wrong - got 1 expected 0"). Each test opened extra connections to see what kind of database it was and closed them without signing in, which MariaDB counts toward blocking a host. On the standard ports (3306, 1433, 5432) the test now just signs in.
  - A blocked server now says so plainly, with how to unblock it (`mariadb-admin flush-hosts` on the database server).
  - Fixed: the page no longer jumps down and loses its place after **Test connection** (or any other part of a page that's sent again); it stays where it was, with the button still focused.
  - Fixed: an asset, S2K or machine-data database on an older MariaDB (before 10.5, e.g. 10.3) failed with "MariaDB 10.5 or later is required". That minimum only matters for the suite's own records database, which the suite writes to and upgrades; the outside databases are only read, so any MariaDB version works for them now.

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
