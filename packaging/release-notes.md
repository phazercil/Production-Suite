## What's new in 1.9.17

**Admin has a sidebar.**
- **Grouped pages:** Plant setup, Integrations, People & access, Monitoring, Notifications and System. Each group opens and closes, and the page you're on is marked.
- **More room:** **Pages** hides the sidebar on a desktop. On a tablet it slides in as a drawer. This screen remembers your choice.
- **Find a setting:** type in the box at the top of the sidebar (or press **Ctrl+K**, or **/**). It searches page names, what each page is for and common words like "timeout", "PIN" or "rollback". Arrow keys and Enter go straight to the page.
- **Where you are:** each page shows a "you are here" line (Admin › Plant setup › Rules), its name and a line on what it's for.
- **People & PINs:** under People & access, it goes to the Team page, where people and their PINs are set.
- **Same as before:** every address works (/admin/backup/ and the rest), and every page is still there with the same permissions.

**Work instructions.**
- **Insert variable:** a button beside Save replaces the separate dropdowns. It opens one searchable list with this instruction's parameters and the filter's data, and puts your pick into the field you were last in, written the way that field takes it:
  - `[Pleat height (mm)]` in a Target, Min or Max box or a condition;
  - `{Screw height}` or `{[Pleat height (mm)]}` in a step.
- **Lists in conditions:** Show when and Not for filters where take comma lists.
  - `[Side A thickness (mm)] = 2, 2.5, 3` means any of them.
  - `[Glue] <> "PUR", "hot melt"` means none of them.
  - Parts, Except parts and Also read already took commas.
- **Special cards between steps:** **⚑ + Add special card** places one of your Special note cards at a point in the steps.
  - **When it shows:** only for filters that meet the card's own Show when; otherwise that step is skipped.
  - **On the kiosk:** it's ticked in place and not repeated at the top of the checklist.
- **What was on the machine before:**
  - **The old value:** in a step, `{Screw #:was}` is the value for the filter the machine is changing from, so "Pull out the {Screw #:was} screws and install the {Screw #} screws" reads "Pull out the 8.1 screws and install the 9.2 screws".
  - **Only if it changes:** each step has this option. Pick what to compare (e.g. Screw # or Side A height (mm)) and what to say when it's the same ("Leave the {Side A height (mm)} side bands on the machine"). Leave that blank and the step is skipped when nothing changes.
  - **Preview:** it has a **Changing from** box to check all this.
- **Filter values by name:** `{Screw #}`, `{Helix #}` and the filter's other values can go straight into a step.
- **Animated GIFs:** they stay animated on steps, the kiosk and the preview. Their thumbnail in the editor is the first frame.
- **Preview layout:** the step comes first and bigger (larger text and pictures), with the settings below it.
- **Text settings:** a parameter can be text from the filter (Helix Size = `[Helix #]` → 24/32), which used to show an error.

## What's new in 1.9.16

**Work instructions.**
- **Nothing you type is lost:**
  - the part-number check no longer leaves the page (it used to throw away unsaved edits);
  - the Save button shows **Unsaved changes**, with **Discard changes** next to it;
  - the browser asks before leaving the page while changes aren't saved.
- **Preview:** type a part number and press **Preview** to step through the instruction as the kiosk shows it, with its settings worked out for that filter. Use Next and Previous, the arrow keys, or "Show all steps".
  - **In the editor:** it previews what's there now, saved or not, and nothing is saved.
  - **On a revision:** it previews that revision.
- **Parameters in steps:**
  - **Values:** write `{Screw height}` in a step to show that parameter's value for the filter being run, with its unit (e.g. "Set the screws to 31.5 mm").
  - **Limits:** `{Screw height:min}`, `{Screw height:max}` and `{Screw height:range}` give its limits.
  - **Formulas:** `{[Pleat height (mm)] + 1}` works a formula out right there.
  - **Picking one:** "Insert a parameter" puts one into the step you were last in.
  - **Typos:** a misspelled parameter name is caught when the draft is saved.
  - **Where it shows:** the kiosk's changeover checklist and its Instructions page show the values.

- **Special note cards:** a new kind for short call-outs about something particular to some filters.
  - **When it shows:** give it a **Show when** condition on the filter's 3D-viewer data, e.g. `[Side A thickness (mm)] = 2`.
  - **On the changeover:** every changeover to a matching filter puts the card (its text and photo) at the top of the checklist. It must be ticked "I've read this note" before Start running, and ticking counts as reading it.
  - **Missing data:** if the filter's data isn't on file, the card still shows, marked as unchecked, so it's never missed.
- **Conditions:** any instruction can have **Show when** and **Not for filters where** conditions, plus **Except parts**.
  - **Comparisons:** conditions use = <> < > <= >=, and / or / not, yes/no settings (`[Side bands] = 1`) and text (`[Media type] = "carbon"`).
  - **Changeover checklists:** a changeover instruction with a condition is used only for filters that meet it.
  - **Preview:** it says whether an instruction shows for the part you type.
- **Also read:** an instruction can name others to read with it (WI numbers).
  - **On the changeover screen:** the list shows those, plus the Safety, Quality check and Operation instructions for the new part.
  - **Opening one:** each opens over the changeover, with Read and understood and Back to the changeover.
- **Variables come only from the 3D filter library.** The drawing is just a picture, so the Parts & specs values are no longer in the variable list.
- **The list:**
  - **What it shows:** the instructions in effect; drafts not yet approved are on their own tab.
  - **Adding one:** **New +** opens the new-instruction form and goes straight to the editor.
  - **If creating fails:** the form comes back with what you typed.

- **The editor:**
  - **Details at the top:** title, kind, machines, parts, when it shows and Also read.
  - **One Save:** it saves the details and the steps together, or neither if something's wrong.
  - **Always in reach:** the Save button stays in the bottom-right corner as you scroll.
- **Information sheet fields as variables:**
  - **Text fields:** glue, screw #, helix #, notes and every row you added to a filter's information sheet are variables, e.g. `[Glue] = "hot melt"` or `[Screw #] = 8.1`.
  - **Tolerances as numbers:** each tolerance also gives its plus and minus in mm, e.g. `[Tolerance – length minus (mm)]`.
  - **Values to review:** a setting the viewer still flags for review isn't used.

**Filter catalog** (Quality › Filter catalog, where Parts & specs was):
- **What it lists:** every filter in the 3D viewer's library, with its size, its information sheet (tolerances, glue, screw #, helix #) and how many values are left to review.
- **Opening one:** **Open** shows the filter in the 3D viewer.
- **Adding one:** **Add new model** opens the viewer's New filter form.
- **QC values:** the Parts & specs table that QC checks use is under "What QC checks use".

**3D viewer:** it fills the screen, so its bottom bar is in view, and the paragraph above it is gone.

**A tidier top bar, grouped by what you're doing.**
- **The groups:** Production (Operator, Packaging, Stations), Planning (Scheduler, Schedule), Oversight (Supervisor, Reports, Data Kiosk, Labor Budget), Quality, Maintenance and Admin.
- **Menus:** a group with several pages opens a menu and shows which one you're on.
- **Same as before:**
  - every address works as it did;
  - people only see what they may open;
  - machine screens locked to one page still show no bar.
- **Plant clock:** the bar shows the plant's time (the server's clock).
- **Ticker warning:** if machine data stops being read (the minute ticker hasn't run for 3 minutes or more), Admins and supervisors get a warning under the bar with a link to Admin › Health.
- **Groundwork for the Admin redesign:**
  - shared colours, spacing and touch sizes;
  - one set of status labels (OK, WARNING, STALE, OFFLINE, FAILED, HELD, RUNNING, DISABLED), each with an icon and a word, not colour alone.

## What's new in 1.9.15

**Changeover steps and settings now live in Quality › Work instructions, inside the suite.**
- **Where they come from:**
  - A changeover now uses the approved **Changeover** work instruction for the machine and the part being changed to.
  - An inkjet-only change uses an approved **Inkjet change** instruction.
  - When several apply, the one aimed most closely wins: an exact part over a pattern, and a named machine over every machine.
  - With no approved instruction, the standard checklists in Admin › Checklists are used as before.
- **No outside database:** the asset database is no longer used for changeover steps or machine settings. Admin › Changeover data is now **Part data** and covers part specs, drawings, tooling and 3D data only.
- **Saved steps become drafts:** steps the suite last read from the asset database can be turned into draft instructions with one button on the Work instructions list, then checked and approved.
- **On the kiosk changeover:**
  - Each step shows its caution, photos and videos.
  - The process parameters show as the settings to set.
  - The screen says which instruction and revision the steps come from.
  - Finishing the changeover counts as reading that revision.

**Formulas from the filter's own data.**
- Target, Min and Max can be a formula worked out for the filter being run, e.g. `[Pleat height (mm)] + 1` for a screw height 1 mm above the pleat height.
- **Where the values come from:**
  - the 3D viewer's part library (in mm);
  - Parts & specs (the drawing, in inches).
- **Writing formulas:**
  - Pick variables from **Insert variable**.
  - Each parameter has a **Description** ("1mm above pleat height") shown with the result.
  - A typo in a variable name is caught when the draft is saved.
- **Checking them:** **Check with a part number** shows what every formula works out to for a part.
- **When data is missing:** if a part has no value for a variable, the setting shows as missing, with the variable named. No value is guessed.

**One kind for changeovers.** The Setup kind is gone: every Setup instruction is now a Changeover instruction, with nothing else about it changed.

**Filter dimensions come from the 3D filter library.**
- **What's read:** Parts & specs reads each filter's numbers from its filter in Quality › 3D viewer, worked out as the viewer works them out and converted to inches:
  - pleats;
  - overall length (pleats × pitch, plus end bands and foam);
  - width (media width, plus side bands and foam);
  - pleat height;
  - side band height.
- **Values never entered:** a value the viewer still flags for review, or one that was never entered, isn't used. What was typed in Parts & specs stays; otherwise it's left blank.
- **The switch:** Admin › Part data › Filter dimensions switches between the 3D filter library (the new default) and the asset database.
- **Still from the asset database:** the drawing file link and rev, when Drawing file is wired.
- **Formulas:** they can use the filter's size too: [Overall length (mm)], [Filter width (mm)], [Filter height (mm)], [Pack length (mm)].

**Each filter's own tolerances.**
- **Read from the sheet:** the tolerances on a filter's information sheet (Tolerance – length, width, pleat height, e.g. "+1 / -2 mm" or "±0.5 mm") are read with its dimensions and used for its QC checks.
- **Fallbacks:** filters without one use Quality › Tolerances, then the part's Tol ±.
- **Where to see them:** Parts & specs shows each filter's sheet tolerances.

**Admin › Connections is easier to get around.**
- **Section buttons:** buttons at the top (Status, Machine data, Databases & accounts, S2K schedule, Paycom…) show just that section.
- **One machine at a time:** Machine data shows one machine's card, picked by name.
- **Status table:** it can be searched, filtered by type (machines, databases, other) and sorted with problems first.
- **Jump to a machine:** clicking a machine's row opens its machine data settings.
- **Remembered:** the choice is kept in the address (e.g. /admin/connections/?sec=machines&mach=Salvatore).

**Editing instructions.**
- Steps can be added, removed, inserted below another step and moved up or down. Parameters can be added and removed.
- **📷 Add photo / video** on each step: pick photos or videos (or take them on a tablet). They're shown on the step right away and saved with the draft.

**The most specific instruction wins.** When several Changeover instructions match, the kiosk uses the one aimed most closely; they aren't combined. Order: an exact part number (MG4062HP), then the longest pattern (MG4062* before MG*), then one for the machine before one for every machine.

## What's new in 1.9.14

**Maintenance is now a full work-order system (CMMS).**
- **Assets** (Maintenance › Assets):
  - Each machine's assemblies and components as a tree, plus gauges and instruments.
  - Each asset has its history: the requests on it, parts used, replacements, rebuilds and calibrations.
- **Work orders:**
  - **Planning:** each request can be assigned to a technician, with a due date and estimated hours.
  - **Workload** shows each person's open work, what's late and the hours they logged.
  - **Recording the work:**
    - Hours worked are logged on the request.
    - Failure code, root cause and remedy are picked from lists (Admin › Maintenance lists).
    - Outside (contractor) costs can be added.
  - **Cost:** each request shows parts at their cost when used, labor at one plant maintenance rate (Admin › Rules; no one's wage is used) and outside costs.
  - **Downtime:** each request shows how long the machine was down while it was open.
- **Checklists:** a PM's steps become the request's checklist, ticked off as they're done. The request can't be completed until every step is ticked.
- **Meter PMs:** a PM can fall due on a meter (knife cycles, glue-gun hours…), read by hand or from a machine data field. This is besides days, run hours and filters.
- **Calibration** (Maintenance › Calibration):
  - Each instrument's interval, as found and as left, result, the standard used and the certificate number.
  - A failed instrument is taken out of use until it passes again.
- **Spare-part reservations:**
  - Parts can be set aside for a request, and a PM's parts list reserves itself when its request opens.
  - Inventory shows what's reserved and what's free.
  - Using the parts on the request uses the reservation; completing the request releases what's left.
- **Maintenance windows** (Maintenance › Windows): planned maintenance time on a machine. The Scheduler plans no production in it, and the kiosk counts it as planned time, not downtime.
- **Repeat failures:**
  - When a machine fails the same way 3 times in 90 days (Admin › Rules), Maintenance suggests a **corrective action**: one request to fix the cause.
  - Once it's open, new requests for that failure are attached to it instead of standing alone.
- **Reliability** adds downtime, hours and cost by machine and asset, with each one's top failures.

**Digital work instructions** (Quality › Work instructions).
- **Documents:**
  - Numbered (WI-001…), for chosen machines and parts (`MG40*`, `AQ12*`…).
  - Kinds: setup, changeover, operation, quality check, safety or maintenance.
- **Contents:** steps with cautions, photos and videos, and the approved process parameters (target, min, max, unit).
- **Revision control:**
  - Every change is a draft with a note on what changed and why.
  - Someone other than its author approves it (an Admin may approve their own). The revision before then becomes obsolete, kept read-only.
  - Every revision stays viewable.
- **On the kiosk:**
  - A new **Instructions** page shows the approved instructions for the machine and the part it runs (or is changing over to).
  - The banner says when there's one to read, and operators tap **Read and understood** for each revision.
  - Who read which revision can be downloaded.
- **Storage:** photos are kept in the database. Videos are saved as files in `media/instructions` in the suite's folder, so include that folder in your backups.

## What's new in 1.9.13

- **S2K posting posts each entry to the S2K MO of its stage.** S2K keeps one MO per stage of an order.
  - A pleater's MO runs go to the order's Stage 1 MO (1MG…).
  - End Band and foam Glue Bead lines (John Sr, William, the glue beads) go to its Stage 2 MO (2MG…/2AQ…). Packaging lines go to the finished MG…/AQ… MO.
  - Packed pallets go to the finished-part (packaging) MO, even when the tag carries another of the order's numbers.
  - Each stage MO is checked against S2K's own quantity and "still to make" for that MO.
  - Packaging is checked against what Stage 1 made good, allowing for products that use two stage units per finished filter.
  - The CSV has a Stage column and the S2K MO number with its current revision.
  - **Postings are kept as a record that's never edited.** If a station job, pallet or MO run changes after it was marked as posted (a new quantity, a completion undone, a pallet voided, a run deleted), the difference shows as a **correction** or **reversal** to post, even outside the dates shown.
  - When an order has stage MOs but not the one a line posts to (no 2MG… MO yet, say), its entries are flagged and held back, never posted to another stage's MO.
- **Counting without a counter is a per-machine switch** (Admin › Connections › Machine data): "Count filters from the speed when no counter is hooked up".
  - On (as before): filters = pleats ÷ pleats per filter.
  - Off: nothing is counted until a counter is connected, and the operator enters the count at End order.
  - A machine with a counter always uses its counter.
- **Counters that give filters per minute.** Machine data has a new setting, "The counter value is":
  - **A running total**, as before: each minute counts what the counter went up by.
  - **Filters made per minute**: each minute's reading is that minute's filters.
  - A per-minute value read as a total only counted when the rate went up (Salvatore showed 50 instead of ~860).
- **The machine's recipe times the MOs.** With the recipe coming from the machine's telemetry, an MO's run is tied to the recipe the machine was on when it began.
  - If the machine runs another recipe for 2 minutes in a row, the old filter can't still be running. The MO's time ends at the change, and what's made from then on is held for the next MO.
  - The kiosk shows "Recipe changed" until End order. The next MO gets the held filters and starts at the change.
  - At Start of shift on a line nobody was logged in to, the recipe change answers "when did it change over?" by itself.
  - An inkjet-only change keeps the same recipe, so it's timed as before (by the operator). A glance at another recipe (one minute) doesn't count.
- **A changeover done with nobody logged in.** When Start of shift finds the line ran another MO with nobody on it, it asks when it changed over:
  - **One of this shift's stops:** that stop becomes the changeover. What was made before it stays on the old MO, and what was made after goes to the new one.
  - **Now:** the line is still stopped for the changeover.
  - **Before this shift:** a supervisor is told to sort out the earlier logs.
- **Stations:** two screens starting work on one station at the same moment can't both start; the database allows one MO under way per station. Quantity updates no longer overwrite each other.
- **Maintenance inventory:**
  - A take-back pressed twice at once only credits the stock once.
  - A PM's parts list is only used once per request.
  - Requests now keep when they were completed. MTTR, PM on-time and "completed this week" use it, so a note added later no longer moves them.
- **Reports, OEE:**
  - Availability is now running time against **planned production time** (the Schedule's shifts less breaks) for the plant and each line, not against the minutes that happened to have readings.
  - When readings cover less than 90% of the planned time, the report says so.
  - An MO run spanning two days puts each day's share of its scrap and output on that day.
- **Quality, SPC:**
  - Cp/Cpk now use the within-process spread (average moving range ÷ 1.128). Pp/Ppk (overall spread) are shown beside them.
  - Capability from under 25 measurements is marked preliminary.
- **Admin › Health:**
  - "All good" now also checks data sources, sensors in fault or alarm, live video and the off-server backup copy.
  - The page lists what needs a look.
  - MariaDB off-server copies are labelled as a dump check, not a test restore.
- **Supervisor › Timeline:** when a busy period has more events than it can load, it says from when the list is complete instead of leaving some out silently.
- **Switch person:** the list of names is only given to a screen where someone is signed in.
- **Fixes:**
  - **Machine counts:** a push-feed machine with no counter now counts from its speed instead of recording 0.
  - **Ticker:**
    - A reading isn't stamped onto minutes being caught up.
    - A ticker that lost its lease no longer runs pulls, backups or video.
  - **Downtime:**
    - No more downtime entries that end before they start.
    - A stop, the QC due after a changeover and a pleats override carry into the next log at a shift or operator change.
  - **Changeovers:**
    - End order needs the count, as a whole number, and locks the MO while it updates it.
    - Picking up a passed-over changeover with an MO scanned keeps the line in the changeover.
    - Passing over a changeover in the first seconds of a shift names the new shift.
  - **Shifts:** shift names follow each machine's own schedule.
  - **Sign-in:**
    - Wrong-PIN limits count per address even without cookies.
    - The kiosk's automatic ping no longer keeps an idle shift lead signed in.
  - **Scheduler:**
    - Pacing weighs each upstream line by what it makes, not by time.
    - A time split before part 1 can start keeps part 1 on the board, flagged.
  - **Updates:** state writes and the waiting file are now safe when the web server and the ticker act at once.
  - **Settings:** a `.env` value with a comment after it is read correctly.

## What's new in 1.9.12

- **Every page and tab has its own address**, so it can be bookmarked or sent: /admin/backup/, /supervisor/down/, /quality/trace/, /maintenance/inventory/, /scheduler/orders/ and so on. Older links still work and switch to the new address.
- **S2K posting moved to the Scheduler** (Scheduler › S2K posting). Old Supervisor links forward there. Planners (Change the schedule) and supervisors can open it.
- **Switching person needs your name and your PIN.** This covers the top bar's Switch person, logging in on a kiosk while someone else is on it, and Change operator. Logging in on an empty kiosk still takes just a PIN or badge.
- **Labor Budget:**
  - Plans over 2.5 MB failed to save (a server error), so a large imported plan was lost on leaving the page. Plans up to 12 MB now save.
  - Import JSON saves right away and says whether the server has it.
- **Scheduler › Lines & Shifts** no longer shows the Labor Budget plan panel. Crews and costs still apply.
- **The suite's icon** shows on the browser tab of every page.

## What's new in 1.9.11

- **A changeover ends when the machine runs.**
  - Once the line has stopped for the changeover and then runs for 3 minutes in a row, the changeover's time ends at the minute it started running. This is set in Admin › Rules › "A changeover is done once the machine runs for". Running out the old MO's last parts before that first stop doesn't end it.
  - Stops after that are ordinary downtime needing a reason; they're no longer tagged Changeover.
  - Shorter test runs don't end it.
  - The operator still finishes the changeover (steps, next MO) on the kiosk. Finishing later keeps the time as it was, and the filters made since it started running go to the new MO.
- **Roll pictures at the start and end of each shift.**
  - Start of shift asks for a picture of the roll on the machine and one of its tag; End shift asks for the same of the roll left for the next shift.
  - Start of shift also shows the pictures the last shift left, to compare.
  - The pictures are kept with the shift: End of Shift report and Supervisor › Shift logs.
  - Admin › Rules › Roll pictures: 2 requires them (the default), 1 asks but allows skipping, 0 turns it off.
- **Schedule** (More › Schedule): the plant's shifts and calendar on one page.
  - Shifts: times, the weekdays each one works, breaks and lunch.
  - Calendar: each day as it will be worked. Any day can be closed (holiday), off, no production, an extra working day, or have one shift off, added or at other times that day.
  - The kiosks' shift windows and planned time, the Scheduler and Reports all follow it. A change for today is picked up at once.
  - The Labor Budget's holidays can be copied in with one button. The Scheduler no longer takes its working days from the Labor Budget, only crews and costs.
- **Nights and weekends:**
  - A changeover left open between shifts with nobody on it no longer counts that time as changeover or downtime (the 420-minute midnight-to-7 am entries).
  - Supervisor › Shift logs always lists every scheduled shift's log, with or without an operator.
- **Deleting a changeover** (Supervisor › Changeovers) clears the Changeover reason from the stops in its time, so the right reason can be given. The stops themselves stay, since the line really was stopped.
- **Supervisor › Downtime: change many reasons at once.**
  - Tick the stops (or tick the box in the header for every stop on the page; Shift-click ticks a range), pick the reason, say why, and press Change them. Delete them removes stops that shouldn't be there (with why).
  - Each stop keeps its change in its history, the same as editing it alone.- End of Shift › Downtime by reason no longer runs past its box.

## What's new in 1.9.10

- **Reliability:**
  - Fixed: parts were lost at every log boundary (shift change, operator change, pass-over): a new log now carries on the machine counter from the log that just ended.
  - The database itself now allows only one active log per machine and one open request per PM task. The update settles any duplicates already there: older logs are ended where the next one started, and extra PM requests are completed with a note.
  - The minute ticker is fenced: a ticker that stalls past its lease and is replaced can't write a minute.
  - Webhook events carry a stable id (the same on every retry), and each event's retries and last error are kept.
  - Maintenance › Inventory: a setting to stop uses from taking an item below zero. "Take back" now keeps the use in the stock history instead of deleting it.
  - Incoming data keeps the time the source sent, and why it was replaced when it couldn't be used. A record whose id was already received is skipped.
  - Machine feeds and data sources: size limits, rate limits and constant-time key checks.
  - The PIN limiter keeps working when the shared cache is down.
  - Database queries are checked the way each database reads them (quotes, comments, `#`), and more side-effect functions are refused.
  - Live video only plays on the suite's own pages: go2rtc no longer accepts any website's request.
  - Camera logins never show in error messages.
  - The station key now guards the suite's Admin page; Django's table editor (/db-admin/) keeps its own login.
  - The kiosk's Today board always shows now, overnight included.
- **Visibility:**
  - **Admin › Health:** ticker, each machine (stopped vs. *not reporting*), connections, data sources, webhook queue, cameras, sensors, backups, disk space, sign-in failures and recent server warnings.
  - **Supervisor › Timeline:** one searchable history of logs, downtime, errors, QC fails, changeovers, alarms, maintenance, parts, corrections (before → after, why) and settings changes. It can be downloaded as CSV.
  - **OEE and speed loss** in Reports, the CSV and the Data Kiosk.
  - **Downtime Pareto, repeat stops** and the filters and crew wages stops cost.
  - **Daily digest email** (Admin › Alerts).
  - **Must-read handoff notes:** the next operator ticks each one before starting.
  - **Scheduled database backups with a copy on another machine**, each copy checked and test-restored (Admin › Backup).
  - **Live updates** (Admin › Network, off by default): screens hear about changes as they happen instead of asking every few seconds.
  - Supervisor line cards show target so far, with buttons for the kiosk, downtime, maintenance and cameras, plus a strip of what needs someone now.
- **Quality and planning:**
  - **Quality › SPC:** control charts against the drawing, Cp / Cpk by machine and part, and drift warnings sent to the line before scrap.
  - **Recall:** a supplier lot to every MO, shift, QC check and pallet tag to quarantine. A pallet tag back to its rolls and lots.
  - **Reconciliation:** made vs. counted on MOs and rolls; filters counted twice are shown, never hidden.
  - **Changeover report:** actual vs. target, handoff wait, slowest steps, part-to-part times. The Scheduler can plan with the real part-to-part times.
  - **Scheduler › Suggest order** (⇅ on a Stage 1 line): groups parts and base parts to save changeovers without making more runs late. Nothing changes until Apply.
  - **Stations** (/stations/): End Band, Arts & Crafts and Glue Bead check in work (Start, Log qty, Complete), and the Scheduler plans what's really left.
  - **Supervisor › S2K posting:** finished runs and pallets against S2K's numbers, with what doesn't agree flagged. A CSV of what isn't posted yet and a Posted mark (nothing is ever sent to S2K).
- **Maintenance:**
  - PM parts lists (with "parts short" warnings and one-tap use).
  - Low-stock notices and alerts, plus a reorder list CSV.
  - Lockout and return to service from a request.
  - **Reliability:** MTBF, MTTR, repeat failures, PM done on time, PM coming up, and parts usage.
- **Screens:**
  - One-row top bar, with Reports, Labor Budget and Admin under More, and only the sections the person signed in can open.
  - One sign-in pill top right on every screen, with the version in it.
  - On-screen PIN keypad (sends by itself when all PINs are one length).
  - The kiosk's action buttons sit in one row.
  - The operator badge switches operator.
  - "Machine data 12 s ago" instead of a bare number.
  - Schedule notices have Show on schedule.
  - Scheduler:
    - ＋ Schedule on each order;
    - details on hover or tap (Inspect, Split, Zoom to day);
    - the KPI chips filter the board;
    - dragging while view-only asks for a PIN;
    - narrow runs don't show cut-off text.
  - Packaging:
    - "Same as last" makes the next pallet in one tap;
    - a two-row form;
    - Find filters as you type, with quick filters.
  - Data Kiosk: sharing under TV / share; the Grafana note is smaller and only for people who can set it up.
  - Undo for scheduler moves, downtime reasons and voided pallet tags.
  - "/" jumps to the page's search box.

## What's new in 1.9.9

- **Linux updates now refresh the server's root helpers:**
  - Since the Linux install was added, every update tried to refresh the root-owned updater and network helper (/usr/libexec/production-suite) only after it had deleted the update file they come from, so they stayed at their first version. That's why live camera video wasn't passed on: the old network helper never wrote Apache's live-video rules.
  - Updates now refresh them before deleting the file.
  - **A server updated before 1.9.9 needs this once, as root**, because its old updater can't refresh itself:
    `install -m 644 /opt/production-suite/tools/updater.py /opt/production-suite/tools/linux_apply.py /opt/production-suite/plant/updatefile.py /opt/production-suite/plant/ed25519.py /usr/libexec/production-suite/`
    then `/usr/bin/python3 /usr/libexec/production-suite/linux_apply.py --dir /opt/production-suite network`.
- **Cameras:**
  - Each tile says **LIVE** when it plays live video, or **PICTURES** and why it isn't live: the server isn't passing live video on yet, the screen can't play the camera's video, or no video came from the stream.
  - The **Live video** box on Admin › Cameras stays current while live video runs (it used to fall back to "STARTING" after a minute).
  - It now tells apart "Apache's rules aren't in place yet", "Apache refused them" and "the ticker hasn't checked in".
- **Fixes from a code review:**
  - Maintenance › Inventory: editing an item no longer changes its stock (stock only changes through Add stock, Count and Use, so every change is logged). After saving, the page keeps its tab and filters.
  - Quality: approving a check that was first disapproved puts its scrapped pieces back on the count.
  - Shift times, breaks, extra breaks and pallet numbering follow the wall clock on the days the clocks change.
  - Planned time for Grafana is looked up again as soon as shifts, breaks or rules change, not up to 10 minutes later.
  - Renaming a machine now renames it everywhere: PM tasks, roll tag photos, data sources, auto QC, inbound records, alerts, planned time, changeovers, supervisor history edits, inventory use, pallet tags and sensors.
  - The minute ticker reads the machines outside the database transaction, so a slow PLC no longer holds the database.
  - Sensors: each alarm rule is checked under its own lock; "not equal" rules use a tiny tolerance; `round(x, n)` and Modbus answers are checked, and "NaN"/"inf" text is no reading. Suite values for uptime and downtime are live during the shift.
  - Data Viewer and saved queries: `:name` inside quotes, `::` casts and `--` comments are left alone, and filters only take real column names. SQL Server "DISTINCT" previews work. Database backups write binary columns correctly.
  - Scheduler: dragging part 2 of a split job previews part 2; runs started before the MO on the same shift are shown; split markers line up.
  - "Machine back up" alerts wait while the shift is paused and say so when the shift ends without the line running again.
  - Inbound data: a part's specs come from its own key or the longest matching prefix; a dimension with no target isn't judged.
  - PM schedule: a due date more than 10 years away isn't shown (it used to error).
  - Cameras: a camera's redirect is only followed to another plant-network address; typing only a new password keeps the saved user name; a stream that hangs without closing is dropped after 20 seconds and reconnected.
  - Supervisor › History: a log from the paper log can't end in the future, and changing a log's start or end recomputes its planned time.
  - Updates: two uploads at once no longer share a file; a failed update keeps its "what was put back" details; a waiting "go back" isn't reported as a new update.
  - The labor page's current-tab marker and the weekly report's day labels work on every server.

## What's new in 1.9.8

- **Maintenance requests are only completed on the Maintenance page:**
  - The Operator Kiosk's Maintenance page still shows every request with its status, pictures and repair notes, and operators can still make a new request, add pictures and (with permission) add repair notes.
  - Its status picker, **Start work** and **Mark complete** are gone, and the kiosk can't change a request's status at all.
  - Starting, waiting on parts and completing are done on the Maintenance page (top bar › Maintenance), which the kiosk links to (except on a locked station).
  - Adding a repair note no longer switches a request to In progress.
- **Planned maintenance completed on the Maintenance page restarts its count:** completing a PM request with the status form now starts the task's count again, as completing it from the kiosk used to.

## What's new in 1.9.7

**Back up before installing:** this update changes the database (Admin › Backup › Back up the database now).

- **Live camera video at the camera's own frame rate (25 fps and up):**
  - RTSP cameras now play as real video on Operator Kiosk › Cameras, about half a second behind live, instead of a picture at a time. It's on by default for RTSP cameras (**Live video** in Admin › Cameras).
  - **How it works:** the suite runs go2rtc (open source, MIT licence, included in the update) on the server. It connects to each camera once and passes its video to the tablets untouched, and the tablet plays it. The server decodes nothing, so its CPU stays low, and the camera's H.264 profile doesn't matter (no RPM Fusion ffmpeg needed for live video).
  - **Network:** go2rtc listens on the server itself only (127.0.0.1:8009) and its own RTSP and WebRTC servers are off. Apache passes on only a camera the suite set up (/video/ws?src=cam<number>), and only to the plant network. Camera logins stay in .env, and go2rtc's settings file is readable by the suite only.
  - **After the update:** Apache's live-video rules are put in place by the server's network helper, which can take up to 10 minutes. Admin › Cameras shows whether live video is running and passed on. Until then, and on any tablet that can't play the camera's video (H.265 on some tablets), the tile shows pictures.
  - Live video needs the Linux server (x86-64). Windows installs keep pictures.
- **Cameras up to 10 pictures a second:** RTSP and MJPEG cameras now stay connected while a screen watches. The server keeps one connection per camera, however many screens watch, holds only the newest picture, and lets go 30 seconds after the last screen stops. Before, each RTSP picture started ffmpeg again, so it was held to 1 a second. Picture (JPEG) addresses go up to 5 a second (each one is a request to the camera). **Test** still takes a single picture.
- **Cameras (RTSP):** when the server's ffmpeg can't decode a camera's H.264 (Fedora's own ffmpeg-free decodes with OpenH264, which only reads the simplest H.264), the camera says so in plain words with the fix: install the full ffmpeg from RPM Fusion, set the camera's stream to H.264 Baseline, or use its picture (JPEG) address. Before, it showed ffmpeg's raw error.
- Admin › Cameras: on a phone, each camera's Test and Change buttons sit side by side under its machines.

## What's new in 1.9.6

**Back up before installing:** this update changes the database (Admin › Backup › Back up the database now).

- **Cameras:**
  - **Operator Kiosk › Cameras** shows the cameras set up for that machine, updating live. Tap one to fill the screen; tap again (or Esc) to go back.
  - **Admin › Cameras › Set up cameras** (/cameras/) adds each camera and picks the machines that show it. A camera can be:
    - a picture (JPEG snapshot) address, which most IP cameras have;
    - an MJPEG stream;
    - or RTSP, which needs ffmpeg on the server (`sudo dnf install ffmpeg`, with RPM Fusion for H.264).
  - Each camera has its own pictures a second, a turn (90° / 180° / 270°) and **Test**.
  - **How the pictures get to the screens:** the server gets each picture from the camera and every screen shares it, so the tablets never connect to a camera. A camera answers at most its pictures a second, however many screens watch, and isn't asked at all when nobody does.
  - **Logins and the plant network:** a camera's user name and password are kept in the server's .env, never in the database. Only cameras on the plant network can be added.
  - A camera that isn't answering says why on its tile (wrong password, wrong address…) and is asked again every 5 seconds.
  - The new **Cameras** permission (shift leads and admins) sets them up. Watching needs nothing more than being on the plant network.
- **Supervisor › Line status: No machine data.** A machine with no readings for 3 minutes was still shown as Running (or Stopped), which was only the last state known. It now shows **No machine data** with the time of the last reading, and isn't counted as running.
- **Pages no longer get stuck sideways on a phone:** a phone browser could shift a page (Admin, Supervisor, Maintenance…) left, with Log out half off the screen and no way to swipe it back. Wide tables now scroll in their own box, and a shifted page goes back the next time the screen updates.
- **Packaging Kiosk › New pallet tag:** the MO list only has the MOs running today, never every open MO: what the Scheduler has on a line today (any stage), what is running on a line now and what ran earlier today. It lists each order and its finished-part MO, not the Stage 1 or end band MOs. With nothing on today's schedule it says so, and a tag can still be made without an MO.

## What's new in 1.9.5

**Back up before installing:** this update changes the database (Admin › Backup › Back up the database now).

- **Sensors & alarms (Admin › Sensors & alarms):** add your own sensors to a machine or the whole plant and alarm on them.
  - **Connections:** Modbus TCP (PLC, remote I/O block or IO-Link master: registers, coils, 16/32-bit, float, one bit, 40001-style addresses), a web address that answers JSON, a database table, an InfluxDB query, an extra field of the machine's own reading, pushed to the server (`POST /api/sensors` with the machine feed key), an Incoming data source, values the suite already has (speed, filters made, MO left, uptime, minutes stopped, minutes since QC), a formula over other sensors, or typed in. The **Connection types** tab lists them all, and how MQTT, OPC UA, EtherNet/IP and S7 come in through Node-RED.
  - **Scaling:** turn the raw reading into real units with two points (0–5 V = 0–500 mm), with ready-made ranges (0–10 V, 4–20 mA, Siemens 0–27648, 16/12/10-bit), an offset, averaging and fault limits (below 3.6 mA = broken wire). On / off sensors get their own words.
  - **Alarm rules:** below, above, between, outside, equals, turns on / off, changes faster than X per minute, doesn't change, no new reading, faulty; with a delay, a deadband, only while the machine runs or is stopped, a severity and your own text.
  - **What an alarm does:** shows in the machine's kiosk Alarms (or every kiosk), can stay until acknowledged, emails or texts people from Admin › Alerts (after N minutes, repeating, and when it clears), opens a maintenance request and goes to the webhook. Live values can show on the kiosk's Status page. Grafana tables: `sensor_values` and `sensor_alarms`.
  - New permission **Sensors & custom alarms** (shift leads and Admin).
- **Packaging Kiosk: pallet tags.** The Packaging tab is open: make and print a pallet tag with the part number (or pick the MO), manufacturing line, pallet number (the next one is filled in), quantity and description. The person logged in is the packer; **Verify** (another person's PIN) and **Sign off** (a shift lead's PIN) add names, otherwise the printed tag has lines to sign by hand. Prints full page, landscape letter. A pallet number that already has a tag is refused until the old tag is voided. New permissions: **Verify pallet tags** and **Supervisor sign-off on pallet tags**.
- **Alarms: Acknowledge clears it right away.** Acknowledging in the Alarms dialog used to leave the alarm showing until the page was reloaded.

## What's new in 1.9.4

**Back up before installing:** this update changes the database (Admin › Backup › Back up the database now).

- **Maintenance › PM schedule and PM history:** two new tabs beside Pending requests, Completed, Inventory and Parts by machine. A request opened by the PM schedule shows a **PM** tag under Pending requests.
- **Planned maintenance (PM schedule):** add jobs for each machine or piece of equipment, done every so many **days**, **run hours** or **filters made** (run hours and filters come from the machine's own readings). The schedule shows how much of each interval is used, what is due, due soon or open, and an estimate of when each one falls due. When a job falls due, the server opens a maintenance request for it (**PM · job**), so it shows on every line's Maintenance page; completing that request starts the count again. **Log done** records a job done early; **Open request** opens it before it's due. Changing the schedule needs the new **PM schedule** permission (maintenance, shift leads and Admin by default).
- **Alerts for QC and planned maintenance (Admin › Alerts):** besides line-down messages, each person can be told when a **QC check is overdue** by their number of minutes (re-checks too), when a **QC check fails** and waits for a supervisor, and when **planned maintenance falls due**. Each is one message per check or request. A person can now get only these (line down set to 0).
- **Traceability (Quality › Traceability):** search a roll, lot, material, MO or part and see which media rolls went into which MOs, with the filters each roll made for each MO, the QC checks while the roll was on, and the picture of its tag. A roll counts from when its tag is scanned until the next roll's tag on that line. Download it as CSV.
- **Roll off:** Roll tags shows the roll on the line now and has **Roll off**, for a roll taken off (used up or put away part-used) before the next one goes on.
- **Read a roll tag from a picture:** Roll tags › **Take a picture of the tag**. The tablet reads the tag itself (nothing leaves the plant network) and fills in the roll ID, lot, material and footage for the operator to check, and the picture is kept with the roll. It finds each field by the label printed next to it; Quality › Traceability › **Reading roll tags** sets the labels your suppliers' tags use, a pattern for a value with no label, and which field a plain barcode fills (GS1 barcodes fill the lot and roll themselves). A material that isn't the MO's media is pointed out.

## What's new in 1.9.3

**Back up before installing:** this update changes the database (Admin › Backup › Back up the database now).

- **Maintenance section:** the Maintenance tab in the top bar is now open (Admin › Permissions › Open Maintenance; maintenance, shift leads and admins have it).
  - **Pending requests:** every request from the lines that isn't completed, on every machine, high priority first. Filter by machine or status.
  - **Status form:** each request has a status form (Open, Reviewed, In progress, Waiting on parts, Completed) with a note on why. Every change goes in the request's history with who made it, and the lines get a notice.
  - **Completed** lists the last 30 days of completed requests; a request can be set back to Open from there.
  - The Operator Kiosk's Maintenance page shows the new statuses and has the same status picker.
- **Inventory (Maintenance › Inventory):** the parts and supplies maintenance keeps: name, part #, location, unit, what's on hand and the reorder level (low or out shows in colour).
  - **Add stock** when parts come in, and **Count** to set what's on the shelf; every change is logged.
  - **Use on a machine** records parts used, with or without a request. On a request, **Parts used** does the same from the request's card. The parts come off the stock, are linked to the machine (and the request), and the request's history says what was used. **Take back** undoes one recorded by mistake.
  - **Parts by machine** shows everything used on each machine: totals per item and every use.
  - The new **Maintenance inventory** permission adds and changes items and stock. Recording parts used needs **Work maintenance requests**.
- **Connector builder in pages:** Admin › Connector builder is split into **Machines**, **MOs & schedule**, **Filter dimensions** and **Changeover & tooling**, each with how many of its boxes are connected. Each page shows the source cards its boxes use and the cards nothing uses yet. A **Still needed** chip opens the page its box is on.
- **3D filter data has every viewer setting:** the box now has an input for each of the 3D viewer's batch-sheet settings: pleats, media, side and end bands (height, thickness, offset below and above the pleat, score flaps), fold-overs, pull tab, glue beads, inkjet, foam strips 1–4 and the info sheet, plus a unit of measure for the (mm) ones. B bands copy A when the database has no B. A setting the database doesn't have keeps what's saved in the viewer.
- **The 3D viewer's part library is a source:** in the Connector builder, add a source card from the **3D viewer part library** (table filter_parts: the part number and every setting of each filter in Quality › 3D viewer). Wire it to Tooling for the part (its screw, helix and media material numbers), Part specs, Changeover steps or Machine settings: no outside database needed, and an edit in the viewer shows up straight away. Filters saved before this update are added when someone with Quality settings opens Quality › 3D viewer.

## What's new in 1.9.2

**Back up before installing:** this update changes the database (Admin › Backup › Back up the database now).

- **QC re-check after a failed check:** as soon as a failed QC check is disapproved or processed out (its scrap taken off the count), a QC check is due on that line right away, even with QC reminders off. Once the issue is fixed, the operator does the re-check: the Run screen alert and the QC Log say **QC re-check due**, and the QC check screen says it's the re-check after the failed check. The next check saved on the line is the re-check: if it passes, the failed check shows **Re-check passed** with the time; if it fails, it goes to a supervisor again, and disapproving it makes another re-check due. A failed check that's approved needs no re-check.
- The re-check (due, passed or failed) shows on the failed check in the kiosk QC Log, Supervisor › QC checks and the shift report, Process Out and the Quality report. Supervisor › Line status shows **Re-check due** on a line still waiting for one, and a re-check that fails again sends a notice.
- **End order:** the run screen's Start changeover button is now **End order**: the run's total made, its total scrap (what QC checks already logged is filled in) and an **end-of-order checklist** to tick off, then the changeover opens to pick the next MO. Closed by mistake? The run screen shows **Start changeover**.
- **Each machine has its own checklists** (Admin › Checklists): the end-of-order checklist, the changeover steps and the inkjet checklist. Pick a machine to give it its own copy; the standard lists are for every machine without one.
- **Inkjet-only changeovers:** going between filters with the same base part (AQ1278 → AQ1278C) only changes the inkjet branding. The kiosk shows the short inkjet checklist, the line keeps running and its filters count to the new MO; any stop still counts as changeover downtime. The Scheduler plans no changeover time for it (Lines & Shifts sets the part prefixes, AQ by default, and the minutes, 0 by default).
- **Emergency switch:** in the changeover, **Not on the schedule? Type the MO - Supervisor** changes over to an MO that isn't lined up for the line: one on another line moves here, one the suite doesn't have yet is added with its filter number and quantity. Once it runs, the Scheduler shows it on this line.
- **Full screen:** a button at the right end of the top bar fills the display with the suite (for a kiosk or a TV); Esc or the button again leaves it. Screens without the top bar (the view-only Data Kiosk) have it in the bottom-right corner, and the Operator Kiosk has it in its left rail under the alarms.
- **Quality › 3D viewer:** the Pleated Filter 3D Viewer is part of the suite, with everything it had (the model, the filter info sheet, the part library, batch sheets, STL and Excel export). Its part library is kept in the suite's own database, so every screen sees the same filters; anyone can look, saving needs **Quality settings**. It runs on the plant network alone (three.js and the Excel reader are served by the suite). To bring in filters you already have, use its **Batch sheet › Import filled sheet** with your exported parts sheet.
- **Filter dimensions from another database later:** Admin › Connector builder has a new **3D filter data** entry (pleat height, pleat count, pitch, length, media width and thickness, fold radius, with a unit-of-measure column). Once it's set up, a filter's stored dimensions are updated from it whenever the filter is opened (at most every "Pulls every" minutes), and Quality › 3D viewer has **Refresh from the asset database**. Anything that database doesn't have stays as saved.
- **Operator Kiosk › 3D Model:** a view-only 3D model of the filter the line is running (or setting up for), with its info sheet. Operators can turn, zoom, change the view and show dimensions; nothing can be changed or saved. It finds the filter by its part number or a customer / OE number listed on it, and follows the line through changeovers.
- **Operator Kiosk › Schedule shows only its own line:** the Today timeline has just this screen's machine, the new **Week** view lays out its runs and changeovers day by day (Monday to Sunday), and the MO list is the MOs on this line this week (or **Next week**), in run order with when each one starts. A machine's locked screen (/station/<machine>/) no longer has the Plan in the Scheduler link, so operators can't leave it from there.
- **Force end a changeover:** a changeover nobody finished can be ended by a supervisor, from Supervisor › Line status (**Force end** on the machine's card) or at the kiosk (**Force end - Supervisor** in the changeover, with a supervisor's PIN). Its time so far stays on the record as changeover with who ended it and why, unchecked steps are skipped, and the line runs the MO that was picked (or is left without one, for Pick MO).
- **Pick MO:** a line running without an MO (or by filter number) has a **Pick MO** button by the changeover button: pick the MO it's making, no changeover needed; the filters already made, the scrap and the QC checks move to it.
- **Run by filter number:** Set up has a **Back** button to fix the filter number or pick another MO, and a filter number that isn't in the cost sheet, Parts & specs or the asset database is refused.
- **Scheduler scrolls on and on:** scroll the board forward and back through the dates without stopping. Day, Week and Month set how much fits on the screen (keeping the middle of the screen where it was); ‹ › scroll a screenful; Today and the date box scroll to that day. The heading and the date box follow what's on screen, and the pieces totals beside each line are for what's on screen. In Day view each day's first hour shows its date.
- **Dragging an MO onto the Scheduler** shows it at its real length on the line under the pointer (its changeover and run over that line's working hours, with the start and end), and it stays there at that size when dropped until the board redraws.
- **Scheduler, what really happened:** each line shows what ran on it (from the kiosks), with gross, net and scrap and the time saved or lost against the planned speed. The MO running now is re-planned at the speed it's really running (after 20 minutes of run time), and the board shows how far ahead or behind it is. Order cards show **gross, net and scrap**, and what's left to make is good filters.
- **Gross, net and scrap:** a disapproved or processed-out QC check now takes the filters off the MO's net once (before, it was taken off twice). The shift's count still drops.
- **Parts & specs:** a value the asset database doesn't have is left blank, never guessed (a blank isn't judged in QC checks until it's filled in); values typed in are kept. Press **Refresh from the asset database** to connect again and re-read every part. Parts already on file lose values that weren't typed in and the database doesn't have, the next time they're read.
- **Add a measurement (Quality › Tolerances):** a dimension every filter has (frame thickness, gasket width…) gets a column in Parts & specs, a tolerance, a role in Admin › Connector builder › Part specs and a place in a stage's QC check. Until the asset database connection reads it, Parts & specs and Tolerances say so (and a notice goes out).
- **Connections save even when they don't connect yet:** the database, InfluxDB and GitHub logins are kept with the reason they aren't connecting, so a fix on the server side doesn't mean typing them again.
- **Data Kiosk:** `/data/<machine>/` shows one machine (e.g. /data/Salvatore/), with a line picker on the page. **Copy link** gives a view-only link (no menu, nothing to press) for a TV or someone on the plant network. Speed charts have a line at every hour and a dotted line at every half hour.

## What's new in 1.9.1

**Back up before installing:** this update changes the database (Admin › Backup › Back up the database now).

- **S2K orders with one MO per stage are one order on the Scheduler.** S2K's MOs for one order (for example 312151 1MG4107HP for pleating, 312152 2MG4107HP for end bands and 312153 MG4107HP for the finished part) are merged into one order card, the finished part's MO. Each stage shows its own S2K MO number and makes what that MO says, so a filter where two stage units make one finished part (400 1MG3012 and 2MG3012 for 200 MG3012) is planned at 400 on the pleater and end bands and 200 at packaging. Stage MOs are matched by part number and consecutive MO numbers, in whatever order S2K lists them.
- **Packaging Kiosk** is in the top bar, ready for the packaging lines' own kiosk in a later update (permission **Open Packaging Kiosk**).
- **Packaging stage:** the Scheduler has a Packaging stage after the end bands. Add your packaging line or lines in Admin › Machines (stage Packaging), and set a filters-per-hour rate in Lines & Shifts. An order whose only open MO is the finished part is a packaging-only order.
- **S2K revisions:** when S2K moves an MO to a new revision (306556 → 306556-001) with the quantity still to make, the MO on file is updated: the screens show the new revision and S2K's "still to make", while the suite's quantity, made count, records and schedule stay. An older revision changes nothing.
- **The pleater shows the MO it posts against:** the run screen, queue, changeover and schedule show the Stage 1 MO's number with its latest revision (312151-031), and scanning that label (any revision) starts the order.
- **Cost sheet:** stage part numbers (1AQ1102, 2MG3041…) now match their finished part's cost sheet row.
- **A changeover no longer stops the line's record:** while a changeover is under way the Run screen's line rate, run state and part count keep showing what the machine really does. Only the time the line is actually stopped during the changeover is Changeover downtime, including a stop that was already going when Change MO was pressed, instead of the whole changeover from start to finish.
- **"Why did the line stop?" is about this shift:** on the operator screens (Run screen, Downtime and the stop prompt) everyone, shift leads and admins included, is only asked about stops in the shift running now. Earlier shifts' stops still without a reason are listed on Admin › Downtime reasons to fill in there.
- **Pull intervals (Admin › Connections › Connection status):** a new **Pulls every** column sets, in minutes, how often the server reads each connection on a timer: S2K, the people list and Paycom; how old a part's drawing may get before the asset database is read again (0 = every time); and how often records are copied to InfluxDB. Machine data is read every minute, because each minute is what's counted.

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
