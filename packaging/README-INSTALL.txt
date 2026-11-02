PRODUCTION SUITE - INSTALLING ON A WINDOWS SERVER
=================================================

Before you start
----------------
- A Windows 10/11 PC or Windows Server 2016 or newer that stays on (the plant server).
- An administrator login on it.
- Your MariaDB server (10.5 or newer): its address, and a user name and password that
  may create tables in the suite's database. If you don't have that yet, the installer can
  sign in once with a MariaDB admin login (e.g. root) and create the database and the suite's
  user for you, with rights on that database only. The admin password is not saved.
- No internet is needed for the suite's packages: they come in the "wheels" folder of this
  download. Internet is only used to download Python if the server doesn't have Python 3.10-3.14
  (64-bit) yet. Once installed, the suite itself uses no internet.

Install
-------
1. Copy the zip to the server, right-click it > Extract All.
2. Open the extracted folder and double-click Setup.exe.
   (Setup.exe isn't code-signed yet, so Windows may say "Windows protected your PC":
   click "More info" > "Run anyway". "Install Production Suite.bat" opens the same window.)
3. Click Yes when Windows asks for administrator rights.
4. The setup window walks through these pages. Each one is checked before you move on,
   and "Test connection" tells you in plain words what to fix if something is wrong:
     - Welcome: the install folder (C:\ProductionSuite). On an update you can keep the
       current settings and skip straight to installing.
     - Getting ready: Python, the program files and the packages, with a % progress bar.
     - Plant network: 192.168.100.0/24 (192.168.100.0 to 192.168.100.255) or your own range(s).
     - Records database: MariaDB server, port, database, user and password.
     - Asset and S2K databases (optional, read-only). Setup only signs in and reads the
       table list; it never creates or changes anything in them. Use read-only accounts.
     - This server: the port screens connect to (8000), the plant time zone, station key.
     - Shift lead login: the first shift lead's name and PIN (6 to 12 digits).
     - Ready to install: a summary, then Install. The progress bar shows the % done and
       each step; "Show details" shows everything setup is doing.
5. The last page shows the addresses to open on the plant screens, for example
     http://PLANTSERVER:8000/operator/    Operator Kiosk (line touchscreens)
     http://PLANTSERVER:8000/scheduler/   Scheduler
     http://PLANTSERVER:8000/data/        Data Kiosk (wall TV)
     http://PLANTSERVER:8000/reports/     Reports
     http://PLANTSERVER:8000/labor/       Labor Budget (shift leads)
     http://PLANTSERVER:8000/station/Barbara/   one machine's screen only

The suite is installed in C:\ProductionSuite and starts by itself whenever the server
starts (scheduled tasks "Production Suite Web", "Production Suite Ticker" and
"Production Suite Control"; another,
"Production Suite Updater", only runs when an update is installed from Admin > Updates).
The version and revision show in the bottom-right corner of every page.

The "Production Suite Admin" shortcut (desktop and Start menu) opens the server's settings
window: start, stop or restart the suite, see its logs, and change any connection (records
database, S2K and asset databases, InfluxDB, Paycom, Grafana, alerts, plant network, port,
time zone). Changes are checked before they're saved. It works from any Windows login on the
server, not just administrators: stopping the suite and changing settings ask for a shift
lead PIN instead. A Production Suite icon next to the clock shows whether the suite is running
(green) or stopped (red); right-click it to open Admin, start or restart the suite. A fourth
scheduled task, "Production Suite Control", does this work for them (this server only).
Shortcuts are put on the server's desktop.

Next steps after installing
---------------------------
- Scheduler > Lines & Shifts: add your machines and shifts.
- Operator Kiosk > Admin (shift lead PIN): machine data feeds, training, downtime reasons,
  rules, alerts. Admin > Machines shows each machine's own screen address to copy.
- Labor Budget: enter crews and the work calendar; the Scheduler plans with it.
- If you connected the S2K database: Operator Kiosk > Admin > Connections > S2K, choose
  "The S2K database (read-only)", write the SELECT for open MOs, Run test, then Save.
- Databases, Paycom, InfluxDB, Grafana, incoming data (machine vision QC) and alerts can all be added
  or changed later in Operator Kiosk > Admin > Connections and Admin > Alerts. No reinstall
  is needed; passwords are stored in C:\ProductionSuite\.env, not in the database.

Updating
--------
From version 1.3.0 on, most updates don't need this installer. You get a small update file,
ProductionSuite-Update-<version>-r<revision>.zip. Either:
  - Operator Kiosk > Admin > Updates (shift lead): choose the file and when it installs
    (right away, when no line is running a shift, or at a set time), or
  - copy it into C:\ProductionSuite\updates\inbox (IT can share that folder).
The suite checks the file is genuine and complete before anything changes, backs itself up,
installs, and goes back to the old version by itself if the new one doesn't start.
Admin > Updates also has "Go back" to return to the previous version.

The full installer is still the way to install on a new server or repair an install.
Extract the newer download and run Setup.exe again. It finds the existing install and
offers a quick update: keep every setting and skip the questions. It stops the suite,
updates the program (packages are only reinstalled if they changed), keeps your settings
and records, adds any new database tables and starts it again.

Removing
--------
Run "Uninstall Production Suite.bat" (in C:\ProductionSuite or in the download).
It never touches the records in MariaDB.

Plant network only
------------------
The suite only works on the plant network, 192.168.100.0/24 (addresses 192.168.100.0 to
192.168.100.255) unless you typed something else during setup.
- Screens with any other address are refused, and the firewall rule only allows the plant
  network, on networks Windows treats as Domain or Private. The server itself is always allowed.
- To change it later: Operator Kiosk > Admin > Network (shift lead PIN). Add or remove ranges
  and press Save; the Windows Firewall rule is updated at the same time. Only private ranges
  (10.x, 172.16-31.x, 192.168.x, 100.64-127.x) can be added, never internet addresses.
- A change that would lock out the screen making it is refused. If screens are locked out anyway,
  on the server itself open http://localhost:8000/operator/ (it is always allowed), or in
  C:\ProductionSuite run:   .venv\Scripts\python manage.py network --set 192.168.100.0/24
- If the installer says the network is "Public", set it to Private in Windows network settings,
  or the plant screens can't connect.
- Machine feeds, S2K, Grafana and the databases must be on the plant network too.
- The S2K and asset databases are only ever read: the suite's own tables go only in its own
  records database.
- The only internet connections are optional ones you set up yourself: Paycom, Twilio for
  text alerts, and a mail server on the internet if you tick that box in Admin > Alerts.

If something goes wrong
-----------------------
- The installer's own log:      C:\ProgramData\ProductionSuite-install.log
- The running suite's logs:     C:\ProductionSuite\logs  (web.log, ticker.log,
                                web-console.log, ticker-console.log)
- Screens can't connect from other PCs: check Windows Firewall allows the port
  (the installer adds a rule called "Production Suite") and use the server's name or IP.
- "Installing the packages from the 'wheels' folder failed": extract the whole zip again
  (right-click > Extract All, not just opening it), and check antivirus didn't quarantine
  anything in the "wheels" folder.

Installing without internet
---------------------------
Everything the suite needs is in this download (the "wheels" folder), so only Python itself
might need downloading. If the server has no internet and no Python yet, install 64-bit
Python 3.12 for all users by hand (from python.org, copied over on a USB stick), then run
Setup.exe.

Unattended install (no window)
------------------------------
    powershell -ExecutionPolicy Bypass -File installer\install.ps1 -AnswersFile answers.json
The answers file holds the same values as the setup window (see app\README.md).
