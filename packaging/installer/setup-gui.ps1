<#
  Production Suite setup window. Started by Setup.exe (or "Install Production Suite.bat").
  A step-by-step installer: asks the questions on pages with a "Test connection" button,
  and shows what it is doing with an overall % progress bar and a details log.
  The work itself is done by common.ps1 and the suite's own `manage.py install_plant`.
  Windows PowerShell 5.1.
#>
param([string]$InstallDir = "")

$ErrorActionPreference = "Continue"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Here

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# ---------------------------------------------------------------- administrator rights
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    try {
        $a = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSCommandPath`""
        if ($InstallDir) { $a += " -InstallDir `"$InstallDir`"" }
        Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList $a | Out-Null
    } catch {
        [Windows.Forms.MessageBox]::Show("Setup needs administrator rights, and Windows did not give them. Nothing was installed.",
            "Production Suite setup", "OK", "Warning") | Out-Null
    }
    exit
}

try {
    Add-Type -Namespace PS -Name Dpi -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();' -ErrorAction SilentlyContinue
    [void][PS.Dpi]::SetProcessDPIAware()
} catch { }
[Windows.Forms.Application]::EnableVisualStyles()

. (Join-Path $Here "common.ps1")
Write-SuiteLog "---- Production Suite $script:SuiteVersion setup window started by $([Environment]::UserName)"

# ---------------------------------------------------------------- state
$Setup = @{
    busy = $false; cancel = $false; page = ""; pct = 0
    installDir = ""; hadEnv = $false; keep = $false; python = $null; venvPy = $null
    env = @{}; hasPw = @{}; createDb = $false; warnings = New-Object Collections.ArrayList
    port = "8000"; stationKey = ""; errorBackTo = ""
}

# ---------------------------------------------------------------- look
$C = @{
    side = [Drawing.Color]::FromArgb(27, 31, 36); sideInk = [Drawing.Color]::FromArgb(230, 232, 235)
    sideDim = [Drawing.Color]::FromArgb(130, 138, 148); accent = [Drawing.Color]::FromArgb(78, 161, 255)
    ok = [Drawing.Color]::FromArgb(22, 128, 61); bad = [Drawing.Color]::FromArgb(190, 40, 40); warn = [Drawing.Color]::FromArgb(150, 100, 0)
    ink = [Drawing.Color]::FromArgb(30, 34, 40); dim = [Drawing.Color]::FromArgb(95, 102, 112)
}
$F = @{
    base = New-Object Drawing.Font("Segoe UI", 9.75)
    title = New-Object Drawing.Font("Segoe UI Semibold", 15)
    brand = New-Object Drawing.Font("Segoe UI Semibold", 13)
    step = New-Object Drawing.Font("Segoe UI", 10)
    stepOn = New-Object Drawing.Font("Segoe UI Semibold", 10)
    big = New-Object Drawing.Font("Segoe UI Semibold", 22)
    mono = New-Object Drawing.Font("Consolas", 9)
}

$form = New-Object Windows.Forms.Form
$form.AutoScaleDimensions = New-Object Drawing.SizeF(96, 96)   # layout below is in 96-DPI pixels; scaled up on high-DPI screens
$form.AutoScaleMode = "Dpi"
$form.Text = "Production Suite setup"
$form.ClientSize = New-Object Drawing.Size(860, 600)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false
$form.Font = $F.base
$form.BackColor = [Drawing.Color]::White
try { $form.Icon = New-Object Drawing.Icon((Join-Path $Here "setup.ico")) } catch { }

# left side: product name and the list of steps
$side = New-Object Windows.Forms.Panel
$side.SetBounds(0, 0, 220, 600); $side.BackColor = $C.side
$form.Controls.Add($side)
$brand = New-Object Windows.Forms.Label
$brand.Text = "Production Suite"; $brand.Font = $F.brand; $brand.ForeColor = $C.sideInk
$brand.SetBounds(22, 24, 190, 30); $side.Controls.Add($brand)
$ver = New-Object Windows.Forms.Label
$ver.Text = "Setup $script:SuiteVersion"; $ver.ForeColor = $C.sideDim
$ver.SetBounds(23, 54, 190, 20); $side.Controls.Add($ver)

$STEPS = @(
    @("welcome", "Welcome"), @("prepare", "Getting ready"), @("network", "Plant network"), @("db", "Records database"),
    @("optional", "Asset and S2K databases"), @("server", "This server"), @("lead", "Shift lead login"),
    @("ready", "Ready to install"), @("install", "Installing"), @("done", "Finished"))
$stepLabels = @{}
$y = 100
foreach ($st in $STEPS) {
    $l = New-Object Windows.Forms.Label
    $l.Text = "   " + $st[1]; $l.ForeColor = $C.sideDim; $l.Font = $F.step
    $l.SetBounds(14, $y, 200, 26); $side.Controls.Add($l)
    $stepLabels[$st[0]] = $l
    $y += 30
}

# right side: title, page area, buttons
$title = New-Object Windows.Forms.Label
$title.Font = $F.title; $title.ForeColor = $C.ink; $title.SetBounds(246, 22, 590, 34)
$form.Controls.Add($title)
$subtitle = New-Object Windows.Forms.Label
$subtitle.ForeColor = $C.dim; $subtitle.SetBounds(248, 58, 590, 40)
$form.Controls.Add($subtitle)

$area = New-Object Windows.Forms.Panel
$area.SetBounds(248, 104, 590, 424)
$form.Controls.Add($area)

$rule = New-Object Windows.Forms.Label
$rule.BorderStyle = "Fixed3D"; $rule.SetBounds(220, 540, 640, 2)
$form.Controls.Add($rule)

function New-Button([string]$text, [int]$x) {
    $b = New-Object Windows.Forms.Button
    $b.Text = $text; $b.SetBounds($x, 552, 100, 32); $b.FlatStyle = "System"
    $form.Controls.Add($b); return $b
}
$btnBack = New-Button "< Back" 520
$btnNext = New-Button "Next >" 626
$btnCancel = New-Button "Cancel" 744
$form.AcceptButton = $btnNext

# ---------------------------------------------------------------- page building helpers
$pages = @{}
function New-Page([string]$key) {
    $p = New-Object Windows.Forms.Panel
    $p.SetBounds(0, 0, 590, 424); $p.Visible = $false; $p.AutoScroll = $true
    $area.Controls.Add($p); $pages[$key] = $p; return $p
}
function Add-Text($page, [string]$text, [int]$y, [int]$h = 40, $color = $null, $font = $null) {
    $l = New-Object Windows.Forms.Label
    $l.Text = $text; $l.SetBounds(0, $y, 570, $h)
    if ($color) { $l.ForeColor = $color }
    if ($font) { $l.Font = $font }
    $page.Controls.Add($l); return $l
}
function Add-Field($page, [string]$label, [int]$y, [string]$value = "", [switch]$Password, [int]$width = 330) {
    $l = New-Object Windows.Forms.Label
    $l.Text = $label; $l.SetBounds(0, $y + 4, 190, 22); $page.Controls.Add($l)
    $t = New-Object Windows.Forms.TextBox
    $t.SetBounds(200, $y, $width, 24); $t.Text = $value
    if ($Password) { $t.UseSystemPasswordChar = $true }
    $page.Controls.Add($t); return $t
}
function Add-Check($page, [string]$text, [int]$y, [bool]$checked) {
    $box = New-Object Windows.Forms.CheckBox
    $box.Text = $text; $box.Checked = $checked; $box.SetBounds(0, $y, 570, 24); $page.Controls.Add($box); return $box
}
function Add-Status($page, [int]$y, [int]$h = 44) {
    $l = New-Object Windows.Forms.Label
    $l.SetBounds(0, $y, 570, $h); $page.Controls.Add($l); return $l
}
function Set-Status($label, [bool]$ok, [string]$text) {
    if ($ok) { $label.ForeColor = $C.ok; $label.Text = [char]0x2713 + " " + $text } else { $label.ForeColor = $C.bad; $label.Text = $text }
}

# ---------------------------------------------------------------- pages
# Welcome
$pw = New-Page "welcome"
Add-Text $pw ("This installs Production Suite on this server: the Operator Kiosk, the Scheduler and the Data Kiosk, " +
    "running in the background and starting with Windows.`n`nYou'll need the MariaDB server details for the suite's records. " +
    "The asset and S2K databases are optional and only ever read.") 0 96 | Out-Null
$wDir = Add-Field $pw "Install folder" 110 $script:DefaultDir -width 260
$wBrowse = New-Object Windows.Forms.Button
$wBrowse.Text = "Browse..."; $wBrowse.SetBounds(468, 108, 90, 27); $wBrowse.FlatStyle = "System"; $pw.Controls.Add($wBrowse)
$wUpgrade = Add-Text $pw "" 150 44 $C.warn
$wKeep = New-Object Windows.Forms.RadioButton
$wKeep.Text = "Quick update: keep all settings, ask no questions (recommended)"; $wKeep.SetBounds(0, 198, 570, 24); $wKeep.Checked = $true; $wKeep.Visible = $false
$pw.Controls.Add($wKeep)
$wChange = New-Object Windows.Forms.RadioButton
$wChange.Text = "Update and review or change the settings"; $wChange.SetBounds(0, 224, 570, 24); $wChange.Visible = $false
$pw.Controls.Add($wChange)
Add-Text $pw "Your records stay in MariaDB. Nothing in the asset or S2K databases is ever created or changed." 380 40 $C.dim | Out-Null

# Progress (used for both "Getting ready" and "Installing")
$pp = New-Page "progress"
$pPct = Add-Text $pp "0%" 6 44 $C.ink $F.big
$pStatus = Add-Text $pp "" 56 24 $C.ink
$pBar = New-Object Windows.Forms.ProgressBar
$pBar.SetBounds(0, 86, 570, 22); $pBar.Minimum = 0; $pBar.Maximum = 100; $pBar.Style = "Continuous"
$pp.Controls.Add($pBar)
$pDetail = Add-Text $pp "" 114 22 $C.dim
$pDetail.AutoEllipsis = $true
$pToggle = New-Object Windows.Forms.LinkLabel
$pToggle.Text = "Show details"; $pToggle.SetBounds(0, 142, 200, 22); $pp.Controls.Add($pToggle)
$pLog = New-Object Windows.Forms.TextBox
$pLog.Multiline = $true; $pLog.ReadOnly = $true; $pLog.ScrollBars = "Vertical"; $pLog.Font = $F.mono
$pLog.BackColor = [Drawing.Color]::FromArgb(246, 247, 249); $pLog.SetBounds(0, 168, 570, 250); $pLog.Visible = $false
$pp.Controls.Add($pLog)
$pToggle.add_LinkClicked({ $pLog.Visible = -not $pLog.Visible; if ($pLog.Visible) { $pToggle.Text = "Hide details" } else { $pToggle.Text = "Show details" } })

# Plant network
$pn = New-Page "network"
Add-Text $pn ("Screens, tablets and PCs can open the suite only from these addresses, and the suite only connects to servers inside them. " +
    "Use a range such as 192.168.100.0/24 (192.168.100.0 to 192.168.100.255); separate several with commas. " +
    "Only private ranges are accepted, never the internet. It can be changed later in Operator Kiosk > Admin > Network.") 0 76 | Out-Null
$nNet = Add-Field $pn "Plant network" 86 "192.168.100.0/24" -width 360
$nStatus = Add-Status $pn 122 60

# Records database
$pd = New-Page "db"
Add-Text $pd "The suite keeps its records (shifts, downtime, QC, schedule) in its own database on your MariaDB server. Setup creates its tables there." 0 40 | Out-Null
$dHost = Add-Field $pd "Server name or address" 48 "localhost"
$dPort = Add-Field $pd "Port" 80 "3306" -width 90
$dName = Add-Field $pd "Database name" 112 "production_suite"
$dUser = Add-Field $pd "User name" 144 "suite"
$dPass = Add-Field $pd "Password" 176 "" -Password
$dPassNote = Add-Text $pd "" 204 20 $C.dim
$dPassNote.Left = 200
$dTest = New-Object Windows.Forms.Button
$dTest.Text = "Test connection"; $dTest.SetBounds(200, 230, 140, 30); $dTest.FlatStyle = "System"; $pd.Controls.Add($dTest)
$dStatus = Add-Status $pd 270 90

# Asset and S2K databases (read-only)
$po = New-Page "optional"
Add-Text $po "Both are optional and only ever read: setup signs in and reads the table list, and creates nothing in them. Use read-only accounts." 0 36 $C.dim | Out-Null
$oAsset = Add-Check $po "Connect the asset database (changeover instructions)" 40 $false
$aHost = Add-Field $po "  Server" 68 ""; $aPort = Add-Field $po "  Port" 96 "3306" -width 90
$aName = Add-Field $po "  Database" 124 ""; $aUser = Add-Field $po "  Read-only user" 152 ""; $aPass = Add-Field $po "  Password" 180 "" -Password
$oS2k = Add-Check $po "Connect the S2K database (open MOs)" 216 $false
$sHost = Add-Field $po "  Server" 244 ""; $sPort = Add-Field $po "  Port" 272 "3306" -width 90
$sName = Add-Field $po "  Database" 300 ""; $sUser = Add-Field $po "  Read-only user" 328 ""; $sPass = Add-Field $po "  Password" 356 "" -Password
$oStatus = Add-Status $po 386 38
function Update-Optional {
    foreach ($t in @($aHost, $aPort, $aName, $aUser, $aPass)) { $t.Enabled = $oAsset.Checked }
    foreach ($t in @($sHost, $sPort, $sName, $sUser, $sPass)) { $t.Enabled = $oS2k.Checked }
}
$oAsset.add_CheckedChanged({ Update-Optional }); $oS2k.add_CheckedChanged({ Update-Optional })

# This server
$ps = New-Page "server"
$sWebPort = Add-Field $ps "Port screens connect to" 0 "8000" -width 90
$sTz = New-Object Windows.Forms.ComboBox
$sTz.SetBounds(200, 34, 330, 24); $sTz.DropDownStyle = "DropDown"
foreach ($z in @("America/New_York", "America/Chicago", "America/Denver", "America/Phoenix", "America/Los_Angeles", "America/Anchorage",
                 "Pacific/Honolulu", "America/Toronto", "America/Mexico_City", "Europe/London", "UTC")) { [void]$sTz.Items.Add($z) }
$sTz.Text = "America/New_York"; $ps.Controls.Add($sTz)
$tzl = New-Object Windows.Forms.Label; $tzl.Text = "Plant time zone"; $tzl.SetBounds(0, 38, 190, 22); $ps.Controls.Add($tzl)
$sLead = Add-Check $ps "Require a shift lead PIN to change the schedule" 72 $true
$sKey = Add-Field $ps "Station key (optional)" 104 ""
Add-Text $ps "If set, every screen must be opened once with ?key=... in the address; it is then remembered. Leave blank for none." 132 40 $C.dim | Out-Null
$sStatus = Add-Status $ps 180 60

# Shift lead
$pl = New-Page "lead"
Add-Text $pl "The first shift lead can log in to Admin, approve schedule changes and add everyone else." 0 24 | Out-Null
$lName = Add-Field $pl "Name" 34 "Shift Lead"
$lPin = Add-Field $pl "PIN (6 to 12 digits)" 66 "" -Password -width 160
$lPin2 = Add-Field $pl "Type the PIN again" 98 "" -Password -width 160
$lDemo = Add-Check $pl "Load the demo plant for training (two pleaters, sample orders, demo PINs)" 140 $false
Add-Text $pl "If the database already has a shift lead login, it is kept and this page is skipped." 172 24 $C.dim | Out-Null
$lStatus = Add-Status $pl 204 50

# Ready
$pr = New-Page "ready"
$rText = New-Object Windows.Forms.TextBox
$rText.Multiline = $true; $rText.ReadOnly = $true; $rText.ScrollBars = "Vertical"; $rText.BorderStyle = "None"
$rText.BackColor = [Drawing.Color]::White; $rText.SetBounds(0, 0, 570, 410); $pr.Controls.Add($rText)

# Done
$pf = New-Page "done"
$fHead = Add-Text $pf "" 0 30 $C.ok $F.title
$fText = Add-Text $pf "" 36 40
$fLinks = New-Object Windows.Forms.FlowLayoutPanel
$fLinks.SetBounds(0, 78, 570, 150); $fLinks.FlowDirection = "TopDown"; $fLinks.WrapContents = $false
$pf.Controls.Add($fLinks)
$fWarn = Add-Text $pf "" 232 118 $C.warn
$fOpen = Add-Check $pf "Open the Operator Kiosk when I click Finish" 356 $true
$fLog = New-Object Windows.Forms.LinkLabel
$fLog.Text = "Open the setup log"; $fLog.SetBounds(0, 386, 200, 22); $pf.Controls.Add($fLog)
$fLog.add_LinkClicked({ Start-Process notepad.exe $script:LogFile })

# Error
$pe = New-Page "error"
$eText = New-Object Windows.Forms.TextBox
$eText.Multiline = $true; $eText.ReadOnly = $true; $eText.ScrollBars = "Vertical"; $eText.BorderStyle = "None"
$eText.ForeColor = $C.bad; $eText.BackColor = [Drawing.Color]::White; $eText.SetBounds(0, 0, 570, 200); $pe.Controls.Add($eText)
Add-Text $pe ("Nothing that was already working has been removed. Fix the problem above and click Back to try again, or Cancel to stop. " +
    "The full log is in $script:LogFile.") 210 60 $C.dim | Out-Null
$eLog = New-Object Windows.Forms.LinkLabel
$eLog.Text = "Open the setup log"; $eLog.SetBounds(0, 276, 200, 22); $pe.Controls.Add($eLog)
$eLog.add_LinkClicked({ Start-Process notepad.exe $script:LogFile })

# ---------------------------------------------------------------- navigation
$TITLES = @{
    welcome = @("Welcome", "Set up Production Suite on this server.")
    network = @("Plant network", "Where the screens are.")
    db = @("Records database (MariaDB)", "The suite's own database for its records.")
    optional = @("Asset and S2K databases", "Optional, read-only connections.")
    server = @("This server", "How screens reach the suite.")
    lead = @("Shift lead login", "The first person who can manage the suite.")
    ready = @("Ready to install", "Check the choices below, then click Install.")
    done = @("Finished", "")
    error = @("Setup didn't finish", "")
}
function Show-Page([string]$key, [string]$stepKey = "") {
    foreach ($p in $pages.Values) { $p.Visible = $false }
    $pages[$key].Visible = $true
    $Setup.page = $key
    if ($TITLES.ContainsKey($key)) { $title.Text = $TITLES[$key][0]; $subtitle.Text = $TITLES[$key][1] }
    if (-not $stepKey) { $stepKey = $key }
    $passed = $true
    foreach ($st in $STEPS) {
        $l = $stepLabels[$st[0]]
        if ($st[0] -eq $stepKey) { $l.ForeColor = $C.sideInk; $l.Font = $F.stepOn; $l.Text = [char]0x25B8 + "  " + $st[1]; $passed = $false }
        elseif ($passed) { $l.ForeColor = $C.sideDim; $l.Font = $F.step; $l.Text = [char]0x2713 + "  " + $st[1] }
        else { $l.ForeColor = $C.sideDim; $l.Font = $F.step; $l.Text = "    " + $st[1] }
    }
    # No Back from the plant network page: the program files are already in place by then.
    $btnBack.Visible = $key -in @("db", "optional", "server", "lead", "ready", "error")
    $btnNext.Text = "Next >"
    if ($key -eq "ready") { $btnNext.Text = "Install" }
    if ($key -eq "done") { $btnNext.Text = "Finish"; $btnCancel.Visible = $false }
    $btnNext.Visible = $key -notin @("error", "progress")
}

function Set-Busy([bool]$busy) {
    $Setup.busy = $busy
    $btnNext.Enabled = -not $busy
    $btnBack.Enabled = -not $busy
    $form.UseWaitCursor = $busy
    # While a page is being checked its fields are locked; the progress page stays usable (Show details).
    foreach ($k in @($pages.Keys)) { if ($k -ne "progress") { $pages[$k].Enabled = -not $busy } }
    if (-not $busy) { Update-Optional }
}

$script:Pump = {
    [Windows.Forms.Application]::DoEvents()
    if ($Setup.cancel) { $Setup.cancel = $false; throw "Setup was stopped." }
}
$script:OnLog = {
    param([string]$line)
    if ($line.StartsWith("@@")) { return }
    $clean = $line -replace ([string][char]27 + '\[[0-9;]*m'), ""
    if ($pLog.TextLength -gt 400000) { $pLog.Text = $pLog.Text.Substring(200000) }
    $pLog.AppendText($clean + "`r`n")
    if ($clean.Trim()) { $pDetail.Text = $clean.Trim() }
}
function Set-Progress([double]$pct, [string]$status = "") {
    $v = [int][Math]::Max($Setup.pct, [Math]::Min(100, $pct))
    $Setup.pct = $v
    $pBar.Value = $v
    $pPct.Text = "$v%"
    if ($status -and $status -ne $pStatus.Text) { $pStatus.Text = $status; Write-SuiteLog "== $status" }
    [Windows.Forms.Application]::DoEvents()
}
function Show-Error([string]$msg, [string]$backTo) {
    Write-SuiteLog "ERROR: $msg"
    $eText.Text = $msg
    $Setup.errorBackTo = $backTo
    Show-Page "error"
}

# ---------------------------------------------------------------- phase 1: Python, files, packages (0-35%)
function Start-Prepare {
    $Setup.pct = 0
    Show-Page "progress" "prepare"
    $title.Text = "Getting ready"; $subtitle.Text = "Python, the program files and the packages the suite needs."
    Set-Busy $true
    try {
        Set-Progress 1 "Looking for Python"
        $Setup.python = Find-Python
        if (-not $Setup.python) {
            $r = [Windows.Forms.MessageBox]::Show("Python 3.10 to 3.14 (64-bit) isn't installed on this server.`n`nDownload Python $script:PyVersion from python.org and install it for all users now?",
                "Production Suite setup", "YesNo", "Question")
            if ($r -ne "Yes") { throw "Install 64-bit Python 3.12 for all users from python.org, then run setup again." }
            $Setup.python = Install-Python { param($p) Set-Progress (1 + 11 * $p / 100) "Installing Python $script:PyVersion" }
        }
        Write-SuiteLog "Using $($Setup.python)"
        Set-Progress 12 "Stopping the running copy"
        $existing = Get-ExistingInstall
        if ($existing) { Stop-Suite $existing }
        Set-Progress 14 "Copying the program files to $($Setup.installDir)"
        Copy-Suite $Root $Setup.installDir
        Set-Progress 22 "Installing the packages the suite needs (no internet needed)"
        $Setup.venvPy = Install-Packages $Root $Setup.installDir $Setup.python { param($p) Set-Progress (22 + 13 * $p / 100) }
        Set-Progress 35 "Ready for your settings"
        $r = Test-SetupPage $Setup.installDir $Setup.venvPy "env" @{}
        if ($r.env) { foreach ($p in $r.env.PSObject.Properties) { $Setup.env[$p.Name] = $p.Value } }
        if ($r.hasPassword) { foreach ($p in $r.hasPassword.PSObject.Properties) { $Setup.hasPw[$p.Name] = [bool]$p.Value } }
        Set-Busy $false
        Fill-Pages
        # A quick update goes straight on to installing: no questions, nothing reset.
        if ($Setup.keep -and $Setup.env["PLANT_NETWORKS"]) { Start-Install } else { Show-Page "network" }
    } catch {
        Set-Busy $false
        Show-Error $_.Exception.Message "welcome"
    }
}

function Fill-Pages {
    $e = $Setup.env
    if ($e["PLANT_NETWORKS"]) { $nNet.Text = ($e["PLANT_NETWORKS"] -split ",") -join ", " }
    if ($e["DB_HOST"]) { $dHost.Text = $e["DB_HOST"] }
    if ($e["DB_PORT"]) { $dPort.Text = $e["DB_PORT"] }
    if ($e["DB_NAME"]) { $dName.Text = $e["DB_NAME"] }
    if ($e["DB_USER"]) { $dUser.Text = $e["DB_USER"] }
    if ($Setup.hasPw["DB_PASSWORD"]) { $dPassNote.Text = "Leave blank to keep the saved password." }
    if ($e["ASSET_DB_HOST"]) {
        $oAsset.Checked = $true; $aHost.Text = $e["ASSET_DB_HOST"]; $aPort.Text = "" + $e["ASSET_DB_PORT"]; $aName.Text = "" + $e["ASSET_DB_NAME"]; $aUser.Text = "" + $e["ASSET_DB_USER"]
    }
    if ($e["S2K_DB_HOST"]) {
        $oS2k.Checked = $true; $sHost.Text = $e["S2K_DB_HOST"]; $sPort.Text = "" + $e["S2K_DB_PORT"]; $sName.Text = "" + $e["S2K_DB_NAME"]; $sUser.Text = "" + $e["S2K_DB_USER"]
    }
    if ($e["WEB_PORT"]) { $sWebPort.Text = $e["WEB_PORT"] }
    if ($e["PLANT_TZ"]) { $sTz.Text = $e["PLANT_TZ"] }
    if ($e.ContainsKey("SCHEDULER_REQUIRE_LEAD")) { $sLead.Checked = ($e["SCHEDULER_REQUIRE_LEAD"] -ne "0") }
    if ($e["STATION_KEY"]) { $sKey.Text = $e["STATION_KEY"] }
    Update-Optional
}

# ---------------------------------------------------------------- answers for manage.py install_plant
function Get-Answers {
    $a = @{
        keep_settings = "n"
        plant_network = $nNet.Text.Trim()
        db_host = $dHost.Text.Trim(); db_port = $dPort.Text.Trim(); db_name = $dName.Text.Trim(); db_user = $dUser.Text.Trim()
        create_database = $(if ($Setup.createDb) { "y" } else { "n" })
        use_asset_db = $(if ($oAsset.Checked) { "y" } else { "n" })
        use_s2k_db = $(if ($oS2k.Checked) { "y" } else { "n" })
        web_port = $sWebPort.Text.Trim(); time_zone = $sTz.Text.Trim()
        scheduler_lead = $(if ($sLead.Checked) { "y" } else { "n" })
        station_key = $sKey.Text.Trim()
        demo = $(if ($lDemo.Checked) { "y" } else { "n" })
        lead_name = $lName.Text.Trim(); lead_pin = $lPin.Text
        network_confirm = "y"; retry_db = "n"; retry_asset = "n"; retry_s2k = "n"
    }
    # A blank password means "keep the saved one": leave it out so setup keeps it.
    if ($dPass.Text -or -not $Setup.hasPw["DB_PASSWORD"]) { $a.db_password = $dPass.Text }
    if ($oAsset.Checked) {
        $a.asset_host = $aHost.Text.Trim(); $a.asset_port = $aPort.Text.Trim(); $a.asset_name = $aName.Text.Trim(); $a.asset_user = $aUser.Text.Trim()
        if ($aPass.Text -or -not $Setup.hasPw["ASSET_DB_PASSWORD"]) { $a.asset_password = $aPass.Text }
    }
    if ($oS2k.Checked) {
        $a.s2k_host = $sHost.Text.Trim(); $a.s2k_port = $sPort.Text.Trim(); $a.s2k_name = $sName.Text.Trim(); $a.s2k_user = $sUser.Text.Trim()
        if ($sPass.Text -or -not $Setup.hasPw["S2K_DB_PASSWORD"]) { $a.s2k_password = $sPass.Text }
    }
    if ($Setup.keep) {
        $a = @{ keep_settings = "y"; plant_network = $nNet.Text.Trim(); use_s2k_db = "n"; network_confirm = "y" }
    }
    return $a
}

function Test-Page([string]$kind, $statusLabel) {
    Set-Busy $true
    $statusLabel.ForeColor = $C.dim; $statusLabel.Text = "Checking..."
    [Windows.Forms.Application]::DoEvents()
    try {
        $r = Test-SetupPage $Setup.installDir $Setup.venvPy $kind (Get-Answers)
    } catch {
        $r = [pscustomobject]@{ ok = $false; msg = $_.Exception.Message }
    }
    Set-Busy $false
    if ($r.ok) { Set-Status $statusLabel $true ("" + $r.msg) } else { Set-Status $statusLabel $false ("" + $r.msg) }
    return $r
}

function Test-Db {
    $r = Test-Page "db" $dStatus
    if (-not $r.ok -and $r.missing -and -not $Setup.createDb) {
        $q = [Windows.Forms.MessageBox]::Show("There is no database named `"$($dName.Text.Trim())`" on that MariaDB server yet.`n`nCreate it now?",
            "Production Suite setup", "YesNo", "Question")
        if ($q -eq "Yes") { $Setup.createDb = $true; $r = Test-Page "db" $dStatus }
    }
    if (-not $r.ok -and $r.needsAdmin) {
        $q = [Windows.Forms.MessageBox]::Show(("The MariaDB user `"$($dUser.Text.Trim())`" can't set up the suite's database yet:`n$($r.msg)`n`n" +
            "Use a MariaDB administrator login (for example root) once, to create the database and the user if needed and give that user " +
            "full rights on the suite's own database only? The administrator login is not saved."), "Production Suite setup", "YesNo", "Question")
        if ($q -eq "Yes") {
            $cred = Show-AdminLogin
            if ($cred) {
                Set-Busy $true
                $dStatus.ForeColor = $C.dim; $dStatus.Text = "Setting up the database with the administrator login..."
                [Windows.Forms.Application]::DoEvents()
                $a = Get-Answers
                $a.admin_user = $cred.user; $a.admin_password = $cred.password
                try { $x = Test-SetupPage $Setup.installDir $Setup.venvPy "dbadmin" $a } catch { $x = [pscustomobject]@{ ok = $false; msg = $_.Exception.Message } }
                $cred = $null; $a = $null
                Set-Busy $false
                if ($x.ok) {
                    [Windows.Forms.MessageBox]::Show("" + $x.msg, "Production Suite setup", "OK", "Information") | Out-Null
                    $r = Test-Page "db" $dStatus
                } else { Set-Status $dStatus $false ("" + $x.msg); $r = $x }
            }
        }
    }
    return $r
}

# A small window asking for a MariaDB administrator login (used once, never saved).
function Show-AdminLogin {
    $dlg = New-Object Windows.Forms.Form
    $dlg.Text = "MariaDB administrator login"; $dlg.FormBorderStyle = "FixedDialog"; $dlg.StartPosition = "CenterParent"
    $dlg.MaximizeBox = $false; $dlg.MinimizeBox = $false; $dlg.ClientSize = New-Object Drawing.Size(420, 200); $dlg.Font = $F.base
    $dlg.AutoScaleDimensions = New-Object Drawing.SizeF(96, 96); $dlg.AutoScaleMode = "Dpi"
    $l0 = New-Object Windows.Forms.Label; $l0.Text = "Used once to set up the suite's database and user. It is not saved."; $l0.SetBounds(16, 14, 390, 36); $dlg.Controls.Add($l0)
    $l1 = New-Object Windows.Forms.Label; $l1.Text = "Administrator user"; $l1.SetBounds(16, 60, 140, 22); $dlg.Controls.Add($l1)
    $u = New-Object Windows.Forms.TextBox; $u.Text = "root"; $u.SetBounds(160, 56, 240, 24); $dlg.Controls.Add($u)
    $l2 = New-Object Windows.Forms.Label; $l2.Text = "Password"; $l2.SetBounds(16, 96, 140, 22); $dlg.Controls.Add($l2)
    $p = New-Object Windows.Forms.TextBox; $p.UseSystemPasswordChar = $true; $p.SetBounds(160, 92, 240, 24); $dlg.Controls.Add($p)
    $ok = New-Object Windows.Forms.Button; $ok.Text = "Set it up"; $ok.SetBounds(196, 150, 100, 30); $ok.DialogResult = "OK"; $dlg.Controls.Add($ok)
    $no = New-Object Windows.Forms.Button; $no.Text = "Cancel"; $no.SetBounds(304, 150, 100, 30); $no.DialogResult = "Cancel"; $dlg.Controls.Add($no)
    $dlg.AcceptButton = $ok; $dlg.CancelButton = $no
    $res = $dlg.ShowDialog($form)
    $out = $null
    if ($res -eq "OK" -and $u.Text.Trim()) { $out = @{ user = $u.Text.Trim(); password = $p.Text } }
    $p.Text = ""; $dlg.Dispose()
    return $out
}

function Show-Ready {
    $a = Get-Answers
    $lines = New-Object Collections.ArrayList
    [void]$lines.Add("Install folder:      $($Setup.installDir)")
    if ($Setup.keep) {
        [void]$lines.Add("Settings:            kept from the current install")
        if ($a.plant_network) { [void]$lines.Add("Plant network:       $($a.plant_network)") }
    } else {
        [void]$lines.Add("Plant network:       $($a.plant_network)")
        [void]$lines.Add("Records database:    $($a.db_name) on $($a.db_host):$($a.db_port) as $($a.db_user)")
        if ($Setup.createDb) { [void]$lines.Add("                     (setup creates this database)") }
        if ($oAsset.Checked) { [void]$lines.Add("Asset database:      $($a.asset_name) on $($a.asset_host), read-only") } else { [void]$lines.Add("Asset database:      not connected") }
        if ($oS2k.Checked) { [void]$lines.Add("S2K database:        $($a.s2k_name) on $($a.s2k_host), read-only") } else { [void]$lines.Add("S2K database:        not connected") }
        [void]$lines.Add("Port:                $($a.web_port)")
        [void]$lines.Add("Time zone:           $($a.time_zone)")
        [void]$lines.Add("Schedule changes:    " + $(if ($sLead.Checked) { "need a shift lead PIN" } else { "anyone" }))
        [void]$lines.Add("Station key:         " + $(if ($a.station_key) { $a.station_key } else { "none" }))
        if (-not $Setup.hadEnv) { [void]$lines.Add("First shift lead:    $($a.lead_name)" + $(if ($lDemo.Checked) { "  (demo plant loaded)" } else { "" })) }
    }
    [void]$lines.Add("")
    [void]$lines.Add("Setup will then:")
    [void]$lines.Add("  - build the suite's tables in its records database (existing records are kept)")
    [void]$lines.Add("  - register two tasks that start the suite with Windows")
    [void]$lines.Add("  - open the port in Windows Firewall to the plant network only")
    [void]$lines.Add("  - put shortcuts on the desktop")
    $rText.Text = $lines -join "`r`n"
    Show-Page "ready"
}

# ---------------------------------------------------------------- phase 2: settings, tables, tasks, firewall (35-100%)
function Start-Install {
    $Setup.pct = 35
    Show-Page "progress" "install"
    $title.Text = "Installing"; $subtitle.Text = "This takes a minute or two. You can watch each step under Show details."
    Set-Busy $true
    $answers = Get-Answers
    try {
        Set-Progress 36 "Saving the settings"
        Invoke-SuiteSetup $Setup.installDir $Setup.venvPy $answers { param($p) Set-Progress (36 + 46 * $p / 100) } { param($t) Set-Progress $Setup.pct $t }
        $envNow = Read-EnvFile (Join-Path $Setup.installDir ".env")
        if ($envNow["WEB_PORT"]) { $Setup.port = $envNow["WEB_PORT"] }
        $Setup.stationKey = "" + $envNow["STATION_KEY"]
        Set-Progress 83 "Registering the suite to start with Windows"
        Register-SuiteTasks $Setup.installDir $Setup.port
        Set-Progress 86 "Opening port $($Setup.port) to the plant network in Windows Firewall"
        $w = Set-SuiteFirewall $Setup.installDir $Setup.venvPy $Setup.port
        foreach ($x in $w) { [void]$Setup.warnings.Add($x) }
        Set-Progress 88 "Starting the suite and waiting for it to answer"
        $ok = Wait-SuiteAnswers $Setup.port $Setup.stationKey { param($p) Set-Progress (88 + 10 * $p / 100) }
        if (-not $ok) {
            $log = Join-Path $Setup.installDir "logs\web-console.log"
            $tail = ""
            if (Test-Path $log) { $tail = (Get-Content $log -Tail 12) -join "`r`n" }
            throw "The suite did not answer on port $($Setup.port) within 90 seconds. The last lines of its log:`r`n$tail"
        }
        Set-Progress 99 "Adding desktop shortcuts"
        $w = Add-SuiteShortcuts $Setup.installDir $Setup.port $Setup.stationKey $Setup.python
        foreach ($x in @($w)) { if ($x) { [void]$Setup.warnings.Add($x) } }
        Set-Progress 100 "Done"
        Write-SuiteLog "Production Suite is installed and running."
        Set-Busy $false
        Show-Done
    } catch {
        Set-Busy $false
        $back = "ready"
        if ($_.Exception.Message -match "MariaDB|database|records") { $back = "db" }
        if ($_.Exception.Message -match "S2K|asset") { $back = "optional" }
        Show-Error $_.Exception.Message $back
    }
}

function Show-Done {
    $fHead.Text = "Production Suite is installed and running."
    $fText.Text = "It starts by itself whenever this server starts. Open these from any screen on the plant network:"
    $fLinks.Controls.Clear()
    $q = ""
    if ($Setup.stationKey) { $q = "?key=$($Setup.stationKey)" }
    $hostName = $env:COMPUTERNAME
    foreach ($item in @(@("Operator Kiosk", "operator"), @("Scheduler", "scheduler"), @("Data Kiosk (wall screen)", "data"), @("Reports", "reports"), @("Labor Budget", "labor"))) {
        $u = "http://${hostName}:$($Setup.port)/$($item[1])/$q"
        $ll = New-Object Windows.Forms.LinkLabel
        $ll.Text = "$($item[0]):  $u"; $ll.AutoSize = $true; $ll.Tag = $u
        $ll.LinkArea = New-Object Windows.Forms.LinkArea(($item[0].Length + 3), $u.Length)
        $ll.add_LinkClicked({ param($src) Start-Process $src.Tag })
        $fLinks.Controls.Add($ll)
    }
    $adminExe = Join-Path $Setup.installDir "desktop\SuiteAdmin.exe"
    if (Test-Path $adminExe) {
        $t = "Server settings, start / stop / restart:  Production Suite Admin (on this server's desktop and Start menu)"
        $ll = New-Object Windows.Forms.LinkLabel
        $ll.Text = $t; $ll.AutoSize = $true; $ll.Tag = $adminExe
        $ll.LinkArea = New-Object Windows.Forms.LinkArea($t.IndexOf("Production Suite Admin"), "Production Suite Admin".Length)
        $ll.add_LinkClicked({ param($src) Start-Process $src.Tag })
        $fLinks.Controls.Add($ll)
    }
    $warn = @($Setup.warnings)
    if ($Setup.stationKey) { $warn += "Each screen needs the station key once: open the address with ?key=$($Setup.stationKey) the first time." }
    if ($warn.Count) { $fWarn.Text = "Please note:`n- " + ($warn -join "`n- ") } else { $fWarn.Text = "" }
    Show-Page "done"
}

# ---------------------------------------------------------------- buttons
$wBrowse.add_Click({
    $d = New-Object Windows.Forms.FolderBrowserDialog
    $d.Description = "Pick the folder to install Production Suite in"
    if ($d.ShowDialog() -eq "OK") { $wDir.Text = Join-Path $d.SelectedPath "ProductionSuite" }
})
$dTest.add_Click({ [void](Test-Db) })

$btnNext.add_Click({
    if ($Setup.busy) { return }
    switch ($Setup.page) {
        "welcome" {
            $dir = $wDir.Text.Trim().TrimEnd("\")
            if ($dir -notmatch '^[A-Za-z]:\\.+') { [Windows.Forms.MessageBox]::Show("Use a full folder path such as C:\ProductionSuite.", "Production Suite setup") | Out-Null; return }
            if ($dir -match '\\Program Files') { [Windows.Forms.MessageBox]::Show("Pick a folder outside Program Files (the suite writes its logs and settings in its own folder), for example C:\ProductionSuite.", "Production Suite setup") | Out-Null; return }
            $Setup.installDir = $dir
            $Setup.hadEnv = Test-Path (Join-Path $dir ".env")
            $Setup.keep = $Setup.hadEnv -and $wKeep.Checked
            Start-Prepare
        }
        "network" {
            $r = Test-Page "network" $nStatus
            if (-not $r.ok) { return }
            $nNet.Text = ($r.networks -split ",") -join ", "
            if ($r.warn) {
                $q = [Windows.Forms.MessageBox]::Show("$($r.warn)`n`nUse this plant network anyway?", "Production Suite setup", "YesNo", "Warning")
                if ($q -ne "Yes") { return }
            }
            if ($Setup.keep) { Show-Ready } else { Show-Page "db" }
        }
        "db" { $r = Test-Db; if ($r.ok) { Show-Page "optional" } }
        "optional" {
            $oStatus.Text = ""
            if ($oAsset.Checked) { $r = Test-Page "asset" $oStatus; if (-not $r.ok) { $oStatus.Text = "Asset database: " + $oStatus.Text; return } }
            if ($oS2k.Checked) { $r = Test-Page "s2k" $oStatus; if (-not $r.ok) { $oStatus.Text = "S2K database: " + $oStatus.Text; return } }
            if ($oAsset.Checked -or $oS2k.Checked) { Set-Status $oStatus $true "Connected read-only. Nothing was created or changed." }
            Show-Page "server"
        }
        "server" {
            $r = Test-Page "server" $sStatus
            if (-not $r.ok) { return }
            if ($Setup.hadEnv) { Show-Ready } else { Show-Page "lead" }
        }
        "lead" {
            if ($lPin.Text -ne $lPin2.Text) { Set-Status $lStatus $false "The two PINs don't match."; return }
            $r = Test-Page "pin" $lStatus
            if ($r.ok) { Show-Ready }
        }
        "ready" { Start-Install }
        "done" {
            if ($fOpen.Checked) { try { Start-Process "http://localhost:$($Setup.port)/operator/" } catch { } }
            $form.Close()
        }
    }
})

$btnBack.add_Click({
    if ($Setup.busy) { return }
    switch ($Setup.page) {
        "db" { Show-Page "network" }
        "optional" { Show-Page "db" }
        "server" { Show-Page "optional" }
        "lead" { Show-Page "server" }
        "ready" { if ($Setup.keep) { Show-Page "network" } elseif ($Setup.hadEnv) { Show-Page "server" } else { Show-Page "lead" } }
        "error" {
            $to = $Setup.errorBackTo
            if (-not $to -or ($to -ne "welcome" -and -not $Setup.venvPy)) { $to = "welcome" }
            if ($Setup.keep -and $to -ne "welcome") { $to = "network" }
            Show-Page $to
        }
    }
})

$btnCancel.add_Click({
    if ($Setup.busy) {
        $q = [Windows.Forms.MessageBox]::Show("Stop setup now? Run it again later to finish.", "Production Suite setup", "YesNo", "Question")
        if ($q -eq "Yes") { $Setup.cancel = $true }
        return
    }
    $form.Close()
})
$form.add_FormClosing({
    param($src, $e)
    if ($Setup.busy) {
        $e.Cancel = $true
        $q = [Windows.Forms.MessageBox]::Show("Stop setup now? Run it again later to finish.", "Production Suite setup", "YesNo", "Question")
        if ($q -eq "Yes") { $Setup.cancel = $true }
    }
})

# ---------------------------------------------------------------- start
if (-not (Test-Path (Join-Path $Root "app\manage.py"))) {
    [Windows.Forms.MessageBox]::Show("The 'app' folder is missing next to Setup. Extract the whole download first (right-click the zip > Extract All), then run Setup.exe from the extracted folder.",
        "Production Suite setup", "OK", "Error") | Out-Null
    exit 1
}
# Files from a downloaded zip are marked as coming from the internet; clear that so they can run.
Get-ChildItem -Path $Root -Recurse -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue

$existing = Get-ExistingInstall
if ($InstallDir) { $wDir.Text = $InstallDir } elseif ($existing) { $wDir.Text = $existing }
if ($existing -and (Test-Path (Join-Path $existing ".env"))) {
    $wDir.Enabled = $false; $wBrowse.Enabled = $false
    $info = Get-InstalledInfo $existing
    $was = if ($info -and $info.version) { "" + $info.version } else { "" }
    $wasText = if ($was) { "Production Suite $was is" } else { "An earlier version of Production Suite is" }
    $cmp = if ($info -and $info.version) { Compare-SuiteVersion $script:SuiteVersion $info.version } else { 1 }
    if ($cmp -eq 0 -and $info.revision) {   # same version: an update from Admin > Updates may have moved it to a newer revision
        $cmp = [Math]::Sign($script:SuiteRevision - [int]$info.revision)
        if ($cmp -lt 0) { $was = "$was rev $($info.revision)" }
    }
    if ($cmp -gt 0) {
        $wUpgrade.Text = "$wasText installed in $existing. Setup will update it to $script:SuiteVersion. Your settings, records and logins are kept; only the program files that changed are replaced."
    } elseif ($cmp -eq 0) {
        $wUpgrade.ForeColor = $C.dim
        $wUpgrade.Text = "Production Suite $script:SuiteVersion is already installed in $existing. Running setup again repairs it; settings and records are kept."
    } else {
        $wUpgrade.ForeColor = $C.bad
        $wUpgrade.Text = "A newer version ($was) is installed in $existing. This download is $script:SuiteVersion; installing it would go back to an older version. Use the newer download instead."
        $btnNext.Enabled = $false
    }
    $wKeep.Visible = $true; $wChange.Visible = $true
}
Show-Page "welcome"
[void]$form.ShowDialog()
Write-SuiteLog "---- setup window closed"
