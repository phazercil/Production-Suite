<#
  Production Suite Admin: the server-side settings window (desktop shortcut "Production Suite Admin").

  - Status: is the suite running and answering; Start, Stop, Restart; open the kiosk, the logs.
  - Connections: records database (MariaDB), S2K and asset databases (read-only), InfluxDB, Paycom,
    Grafana, machine feed key, downtime alerts (mail server, Twilio), plant network, port, time zone.

  Every change is checked first (the same checks as Operator Kiosk > Admin) and nothing is saved if the
  check fails. Passwords left blank keep the saved ones. Runs as administrator (SuiteAdmin.exe asks).
  It lives with the program files, so updates from Admin > Updates update it too.
  Windows PowerShell 5.1 compatible.
#>
param([string]$InstallDir = "")
$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $InstallDir) { $InstallDir = Split-Path -Parent $Here }
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    try {
        Start-Process powershell.exe -Verb RunAs -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", "`"$($MyInvocation.MyCommand.Path)`"")
    } catch {
        [Windows.Forms.MessageBox]::Show("Production Suite Admin needs administrator rights, and Windows did not give them.", "Production Suite Admin") | Out-Null
    }
    exit
}
[Windows.Forms.Application]::EnableVisualStyles()

$TaskWeb = "Production Suite Web"; $TaskTicker = "Production Suite Ticker"; $TaskUpdater = "Production Suite Updater"
$FirewallRule = "Production Suite"
$Py = Join-Path $InstallDir ".venv\Scripts\python.exe"
$LogDir = Join-Path $InstallDir "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$AdminLog = Join-Path $LogDir "admin.log"
$State = @{ busy = $false; info = $null; page = "" }

function Write-AdminLog([string]$msg) {
    try { Add-Content -Path $AdminLog -Value ("{0:yyyy-MM-dd HH:mm:ss} [{1}] {2}" -f (Get-Date), [Environment]::UserName, $msg) -Encoding UTF8 } catch { }
}

# ---------------------------------------------------------------- running the suite's own commands
function ConvertTo-ArgString([string[]]$list) {
    $parts = foreach ($a in $list) {
        if ($a -eq "") { '""'; continue }
        if ($a -notmatch '[\s"]') { $a; continue }
        $sb = New-Object Text.StringBuilder
        [void]$sb.Append('"')
        $slashes = 0
        foreach ($ch in $a.ToCharArray()) {
            if ($ch -eq '\') { $slashes++; continue }
            if ($ch -eq '"') { [void]$sb.Append('\' * ($slashes * 2 + 1)); [void]$sb.Append('"'); $slashes = 0; continue }
            if ($slashes) { [void]$sb.Append('\' * $slashes); $slashes = 0 }
            [void]$sb.Append($ch)
        }
        if ($slashes) { [void]$sb.Append('\' * ($slashes * 2)) }
        [void]$sb.Append('"')
        $sb.ToString()
    }
    return ($parts -join " ")
}

# Runs python manage.py <arguments> without a console window, keeping this window responsive.
# Returns @{ code; lines } with every output line.
function Invoke-Manage([string[]]$arguments, [int]$timeoutSec = 600) {
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName = $Py
    $psi.Arguments = ConvertTo-ArgString (@("manage.py") + $arguments)
    $psi.WorkingDirectory = $InstallDir
    $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [Text.Encoding]::UTF8; $psi.StandardErrorEncoding = [Text.Encoding]::UTF8
    $psi.EnvironmentVariables["PYTHONUTF8"] = "1"; $psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"
    $psi.EnvironmentVariables["TICKER_IN_PROCESS"] = "0"
    $p = New-Object Diagnostics.Process
    $p.StartInfo = $psi
    try { [void]$p.Start() } catch { throw "Couldn't start the suite's Python ($Py): $($_.Exception.Message). Run Setup.exe to repair the install." }
    $lines = New-Object Collections.ArrayList
    $out = $p.StandardOutput.ReadLineAsync(); $err = $p.StandardError.ReadLineAsync()
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($out -or $err) {
        $idle = $true
        if ($out -and $out.IsCompleted) { $l = $out.Result; if ($null -eq $l) { $out = $null } else { [void]$lines.Add($l); $out = $p.StandardOutput.ReadLineAsync(); $idle = $false } }
        if ($err -and $err.IsCompleted) { $l = $err.Result; if ($null -eq $l) { $err = $null } else { [void]$lines.Add($l); $err = $p.StandardError.ReadLineAsync(); $idle = $false } }
        [Windows.Forms.Application]::DoEvents()
        if ($idle) { Start-Sleep -Milliseconds 30 }
        if ($sw.Elapsed.TotalSeconds -gt $timeoutSec) { try { $p.Kill() } catch { }; throw "That took longer than $timeoutSec seconds and was stopped." }
    }
    $p.WaitForExit()
    return @{ code = $p.ExitCode; lines = $lines }
}

# Writes params to a file only administrators can read (the logs folder), runs the command, and returns
# the @@RESULT object it printed. The file is removed afterwards: it can hold passwords.
function Invoke-Suite([string[]]$arguments, $params = $null, [string]$paramFlag = "--params") {
    $file = $null
    try {
        if ($null -ne $params) {
            $file = Join-Path $LogDir ("admin-" + [guid]::NewGuid().ToString("N") + ".json")
            [IO.File]::WriteAllText($file, ($params | ConvertTo-Json -Depth 6 -Compress), (New-Object Text.UTF8Encoding($false)))
            $arguments = $arguments + @($paramFlag, $file)
        }
        $r = Invoke-Manage $arguments
        $line = @($r.lines | Where-Object { "$_".StartsWith("@@RESULT ") }) | Select-Object -Last 1
        if (-not $line) {
            $tail = (@($r.lines) | Select-Object -Last 4) -join " | "
            return [pscustomobject]@{ ok = $false; error = "The suite didn't answer ($tail)"; msg = "The suite didn't answer ($tail)" }
        }
        return ($line.Substring(9) | ConvertFrom-Json)
    } catch {
        return [pscustomobject]@{ ok = $false; error = $_.Exception.Message; msg = $_.Exception.Message }
    } finally {
        if ($file -and (Test-Path $file)) { Remove-Item $file -Force -ErrorAction SilentlyContinue }
    }
}
function Invoke-SuiteCmd([string]$name, $params) { return Invoke-Suite @("suiteadmin", "cmd", $name) $params }
function Get-Msg($r) { if ($r.ok) { return "" + $(if ($r.msg) { $r.msg } else { "Done." }) } else { return "" + $(if ($r.error) { $r.error } else { $r.msg }) } }

# ---------------------------------------------------------------- the suite's processes
function Get-Port {
    $port = "8000"
    $envFile = Join-Path $InstallDir ".env"
    if (Test-Path $envFile) {
        foreach ($l in (Get-Content $envFile)) { if ($l -match '^\s*WEB_PORT\s*=\s*"?(\d+)') { $port = $Matches[1] } }
    }
    return $port
}
function Get-TaskState([string]$name) {
    $t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if (-not $t) { return "Missing" }
    return "" + $t.State
}
function Get-Answering {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$(Get-Port)/api/version" -UseBasicParsing -TimeoutSec 3
        return ($r.Content | ConvertFrom-Json)
    } catch { return $null }
}
function Stop-SuiteNow {
    foreach ($t in @($TaskWeb, $TaskTicker)) { if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) { Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue } }
    # the task stops cmd.exe; also stop the Python processes it started (their command line names the folder)
    $pattern = "*" + $InstallDir.TrimEnd("\") + "*"
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'cmd.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like $pattern -and ($_.CommandLine -like "*serve.py*" -or $_.CommandLine -like "*run-web.cmd*" -or $_.CommandLine -like "*run-ticker.cmd*") } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    for ($i = 0; $i -lt 20; $i++) { if (-not (Get-Answering)) { break }; Wait-Pumped 1000 }
    Write-AdminLog "Stopped the suite"
}
function Start-SuiteNow {
    foreach ($t in @($TaskWeb, $TaskTicker)) {
        if (-not (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue)) { throw "The scheduled task '$t' is missing. Run Setup.exe to repair the install." }
        Start-ScheduledTask -TaskName $t
    }
    Write-AdminLog "Started the suite"
    for ($i = 0; $i -lt 90; $i++) { $v = Get-Answering; if ($v) { return $v }; Wait-Pumped 1000 }
    return $null
}
function Wait-Pumped([int]$ms) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.ElapsedMilliseconds -lt $ms) { [Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 50 }
}

# ---------------------------------------------------------------- look
$C = @{
    side = [Drawing.Color]::FromArgb(27, 31, 36); sideInk = [Drawing.Color]::FromArgb(230, 232, 235)
    sideDim = [Drawing.Color]::FromArgb(150, 158, 168); sideOn = [Drawing.Color]::FromArgb(45, 60, 80)
    ok = [Drawing.Color]::FromArgb(22, 128, 61); bad = [Drawing.Color]::FromArgb(190, 40, 40); warn = [Drawing.Color]::FromArgb(150, 100, 0)
    ink = [Drawing.Color]::FromArgb(30, 34, 40); dim = [Drawing.Color]::FromArgb(95, 102, 112)
}
$F = @{
    base = New-Object Drawing.Font("Segoe UI", 9.75)
    title = New-Object Drawing.Font("Segoe UI Semibold", 15)
    head = New-Object Drawing.Font("Segoe UI Semibold", 11)
    brand = New-Object Drawing.Font("Segoe UI Semibold", 13)
    nav = New-Object Drawing.Font("Segoe UI", 10)
    navOn = New-Object Drawing.Font("Segoe UI Semibold", 10)
    big = New-Object Drawing.Font("Segoe UI Semibold", 12)
    mono = New-Object Drawing.Font("Consolas", 9)
}

$form = New-Object Windows.Forms.Form
$form.AutoScaleDimensions = New-Object Drawing.SizeF(96, 96)
$form.AutoScaleMode = "Dpi"
$form.Text = "Production Suite Admin"
$form.ClientSize = New-Object Drawing.Size(940, 640)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedSingle"
$form.MaximizeBox = $false
$form.Font = $F.base
$form.BackColor = [Drawing.Color]::White
foreach ($ico in @((Join-Path $Here "suite.ico"), (Join-Path $InstallDir "installer\setup.ico"))) {
    if (Test-Path $ico) { try { $form.Icon = New-Object Drawing.Icon($ico); break } catch { } }
}

$side = New-Object Windows.Forms.Panel
$side.SetBounds(0, 0, 210, 640); $side.BackColor = $C.side
$form.Controls.Add($side)
$brand = New-Object Windows.Forms.Label
$brand.Text = "Production Suite"; $brand.Font = $F.brand; $brand.ForeColor = $C.sideInk
$brand.SetBounds(20, 22, 185, 28); $side.Controls.Add($brand)
$verLabel = New-Object Windows.Forms.Label
$verLabel.Text = "Server admin"; $verLabel.ForeColor = $C.sideDim
$verLabel.SetBounds(21, 50, 185, 20); $side.Controls.Add($verLabel)

$NAV = @(@("status", "Status"), @("records", "Records database"), @("readonly", "S2K and asset databases"), @("influx", "InfluxDB"),
         @("accounts", "Paycom, Grafana, feeds"), @("alerts", "Downtime alerts"), @("network", "Network and server"))
$navLabels = @{}
$y = 92
foreach ($n in $NAV) {
    $l = New-Object Windows.Forms.Label
    $l.Text = "   " + $n[1]; $l.ForeColor = $C.sideDim; $l.Font = $F.nav; $l.Tag = $n[0]; $l.Cursor = [Windows.Forms.Cursors]::Hand
    $l.SetBounds(10, $y, 192, 30); $l.TextAlign = "MiddleLeft"
    $l.add_Click({ param($src) Show-Page $src.Tag })
    $side.Controls.Add($l); $navLabels[$n[0]] = $l
    $y += 34
}
$folderLabel = New-Object Windows.Forms.Label
$folderLabel.Text = $InstallDir; $folderLabel.ForeColor = $C.sideDim
$folderLabel.SetBounds(20, 590, 185, 40); $side.Controls.Add($folderLabel)

$title = New-Object Windows.Forms.Label
$title.Font = $F.title; $title.ForeColor = $C.ink; $title.SetBounds(232, 18, 690, 32)
$form.Controls.Add($title)
$subtitle = New-Object Windows.Forms.Label
$subtitle.ForeColor = $C.dim; $subtitle.SetBounds(234, 52, 690, 40)
$form.Controls.Add($subtitle)
$area = New-Object Windows.Forms.Panel
$area.SetBounds(234, 96, 694, 534)
$form.Controls.Add($area)

# ---------------------------------------------------------------- building blocks
$pages = @{}; $pageText = @{}
function New-Page([string]$key, [string]$titleText, [string]$sub) {
    $p = New-Object Windows.Forms.Panel
    $p.SetBounds(0, 0, 694, 534); $p.Visible = $false; $p.AutoScroll = $true
    $area.Controls.Add($p); $pages[$key] = $p; $pageText[$key] = @($titleText, $sub); return $p
}
function Add-Head($page, [string]$text, [int]$y) {
    $l = New-Object Windows.Forms.Label
    $l.Text = $text; $l.Font = $F.head; $l.ForeColor = $C.ink; $l.SetBounds(0, $y, 660, 24); $page.Controls.Add($l); return $l
}
function Add-Text($page, [string]$text, [int]$y, [int]$h = 36, $color = $null) {
    $l = New-Object Windows.Forms.Label
    $l.Text = $text; $l.SetBounds(0, $y, 660, $h); if ($color) { $l.ForeColor = $color } else { $l.ForeColor = $C.dim }
    $page.Controls.Add($l); return $l
}
function Add-Field($page, [string]$label, [int]$y, [switch]$Password, [int]$width = 300, [int]$x = 0) {
    $l = New-Object Windows.Forms.Label
    $l.Text = $label; $l.SetBounds($x, $y + 4, 180, 22); $page.Controls.Add($l)
    $t = New-Object Windows.Forms.TextBox
    $t.SetBounds($x + 186, $y, $width, 24)
    if ($Password) { $t.UseSystemPasswordChar = $true }
    $page.Controls.Add($t); return $t
}
function Add-Combo($page, [string]$label, [int]$y, [string[]]$items, [int]$width = 300) {
    $l = New-Object Windows.Forms.Label
    $l.Text = $label; $l.SetBounds(0, $y + 4, 180, 22); $page.Controls.Add($l)
    $c = New-Object Windows.Forms.ComboBox
    $c.DropDownStyle = "DropDownList"; $c.SetBounds(186, $y, $width, 24)
    foreach ($i in $items) { [void]$c.Items.Add($i) }
    $page.Controls.Add($c); return $c
}
function Add-Check($page, [string]$text, [int]$y) {
    $b = New-Object Windows.Forms.CheckBox
    $b.Text = $text; $b.SetBounds(186, $y, 470, 24); $page.Controls.Add($b); return $b
}
function Add-Button($page, [string]$text, [int]$x, [int]$y, [int]$w = 110) {
    $b = New-Object Windows.Forms.Button
    $b.Text = $text; $b.SetBounds($x, $y, $w, 30); $b.FlatStyle = "System"
    $page.Controls.Add($b); return $b
}
function Add-Status($page, [int]$y, [int]$h = 40) {
    $l = New-Object Windows.Forms.Label
    $l.SetBounds(0, $y, 660, $h); $page.Controls.Add($l); return $l
}
function Set-Status($label, $ok, [string]$text) {
    if ($ok -eq $true) { $label.ForeColor = $C.ok; $label.Text = [char]0x2713 + " " + $text }
    elseif ($ok -eq $false) { $label.ForeColor = $C.bad; $label.Text = $text }
    else { $label.ForeColor = $C.dim; $label.Text = $text }
}
function Set-Busy([bool]$on) {
    $State.busy = $on
    if ($on) { $form.Cursor = [Windows.Forms.Cursors]::WaitCursor } else { $form.Cursor = [Windows.Forms.Cursors]::Default }
    foreach ($p in $pages.Values) { foreach ($c in $p.Controls) { if ($c -is [Windows.Forms.Button]) { $c.Enabled = -not $on } } }
    [Windows.Forms.Application]::DoEvents()
}
# Runs a check/save with the status line showing progress, then shows the result. Returns the result.
function Run-Step($statusLabel, [string]$doing, [scriptblock]$work) {
    if ($State.busy) { return $null }
    Set-Busy $true
    Set-Status $statusLabel $null $doing
    try { $r = & $work } catch { $r = [pscustomobject]@{ ok = $false; error = $_.Exception.Message } }
    Set-Busy $false
    Set-Status $statusLabel ([bool]$r.ok) (Get-Msg $r)
    return $r
}
function Confirm-Box([string]$text, [string]$icon = "Question") {
    return ([Windows.Forms.MessageBox]::Show($text, "Production Suite Admin", "YesNo", $icon) -eq "Yes")
}

# ================================================================ Status
$pS = New-Page "status" "Status" "Whether the suite is running on this server, and starting, stopping or restarting it. Screens show `"offline`" while it is stopped and reconnect by themselves."
$sVersion = Add-Text $pS "" 0 26 $C.ink; $sVersion.Font = $F.big
$sWeb = Add-Text $pS "" 34 22 $C.ink
$sTicker = Add-Text $pS "" 58 22 $C.ink
$sAnswer = Add-Text $pS "" 82 40 $C.ink
$sStart = Add-Button $pS "Start" 0 128 100
$sStop = Add-Button $pS "Stop" 108 128 100
$sRestart = Add-Button $pS "Restart" 216 128 100
$sRefresh = Add-Button $pS "Refresh" 324 128 100
$sStatus = Add-Status $pS 166 40
$sOpen = Add-Button $pS "Open Operator Kiosk" 0 212 170
$sLogs = Add-Button $pS "Open logs folder" 178 212 150
$sBackups = Add-Button $pS "Open updates folder" 336 212 160
Add-Head $pS "Logs" 258 | Out-Null
$sLogPick = New-Object Windows.Forms.ComboBox
$sLogPick.DropDownStyle = "DropDownList"; $sLogPick.SetBounds(0, 286, 240, 24)
foreach ($n in @("web.log", "ticker.log", "update.log", "admin.log", "web-console.log", "ticker-console.log")) { [void]$sLogPick.Items.Add($n) }
$pS.Controls.Add($sLogPick)
$sLogShow = Add-Button $pS "Show last lines" 248 283 130
$sLog = New-Object Windows.Forms.TextBox
$sLog.Multiline = $true; $sLog.ReadOnly = $true; $sLog.ScrollBars = "Both"; $sLog.WordWrap = $false; $sLog.Font = $F.mono
$sLog.SetBounds(0, 318, 670, 206); $sLog.BackColor = [Drawing.Color]::FromArgb(246, 247, 249)
$pS.Controls.Add($sLog)

function Update-StatusPage {
    $web = Get-TaskState $TaskWeb; $tick = Get-TaskState $TaskTicker
    $v = Get-Answering
    $port = Get-Port
    $word = @{ Running = "running"; Ready = "stopped"; Disabled = "turned off in Task Scheduler"; Missing = "not set up (run Setup.exe)"; Queued = "starting" }
    $sWeb.Text = "Web app (screens):  " + $(if ($word[$web]) { $word[$web] } else { $web })
    $sTicker.Text = "Minute ticker (machine data, pulls, alerts):  " + $(if ($word[$tick]) { $word[$tick] } else { $tick })
    if ($v) {
        $sVersion.Text = "Running  " + [char]0x00B7 + "  v$($v.version) rev $($v.revision)"; $sVersion.ForeColor = $C.ok
        $sAnswer.Text = "Answering on port $port. Screens open http://$($env:COMPUTERNAME):$port/operator/"; $sAnswer.ForeColor = $C.ink
    } else {
        if ($web -eq "Running") { $sVersion.Text = "Starting or not answering"; $sVersion.ForeColor = $C.warn }
        else { $sVersion.Text = "Stopped"; $sVersion.ForeColor = $C.bad }
        $sAnswer.Text = "Not answering on port $port. If it was just started, give it a minute; otherwise see web-console.log below."; $sAnswer.ForeColor = $C.dim
    }
    $sStart.Enabled = -not $State.busy -and -not $v -and $web -ne "Running"
    $sStop.Enabled = -not $State.busy -and ($web -eq "Running" -or $tick -eq "Running" -or $v)
}
$sStart.add_Click({ [void](Run-Step $sStatus "Starting the suite..." { $v = Start-SuiteNow; if ($v) { @{ ok = $true; msg = "Started: v$($v.version) rev $($v.revision) is answering." } } else { @{ ok = $false; error = "Started, but it isn't answering yet. See web-console.log." } } }); Update-StatusPage })
$sStop.add_Click({
    if (-not (Confirm-Box "Stop the suite? Every screen goes offline and machine readings aren't recorded until it is started again.")) { return }
    [void](Run-Step $sStatus "Stopping the suite..." { Stop-SuiteNow; @{ ok = $true; msg = "Stopped. It starts again by itself when the server restarts, or with Start." } }); Update-StatusPage })
$sRestart.add_Click({
    if (-not (Confirm-Box "Restart the suite? Screens go offline for about a minute and reconnect by themselves.")) { return }
    [void](Run-Step $sStatus "Restarting the suite..." { Stop-SuiteNow; $v = Start-SuiteNow; if ($v) { @{ ok = $true; msg = "Restarted: v$($v.version) rev $($v.revision) is answering." } } else { @{ ok = $false; error = "Restarted, but it isn't answering yet. See web-console.log." } } })
    Update-StatusPage })
$sRefresh.add_Click({ Update-StatusPage })
$sOpen.add_Click({ Start-Process "http://localhost:$(Get-Port)/operator/" })
$sLogs.add_Click({ Start-Process explorer.exe $LogDir })
$sBackups.add_Click({ $u = Join-Path $InstallDir "updates"; if (-not (Test-Path $u)) { New-Item -ItemType Directory $u -Force | Out-Null }; Start-Process explorer.exe $u })
$sLogShow.add_Click({
    $name = "" + $sLogPick.SelectedItem; if (-not $name) { $name = "web.log" }
    $f = Join-Path $LogDir $name
    if (Test-Path $f) { $sLog.Text = ((Get-Content $f -Tail 200 -ErrorAction SilentlyContinue) -join "`r`n"); $sLog.SelectionStart = $sLog.Text.Length; $sLog.ScrollToCaret() }
    else { $sLog.Text = "$name hasn't been written yet." }
})

# ================================================================ Records database
$pR = New-Page "records" "Records database" "The suite's own MariaDB database: every shift, reading, downtime and setting is kept here. Changing it moves the suite to another database; the old one is left as it was."
$rHost = Add-Field $pR "MariaDB server" 0
$rPort = Add-Field $pR "Port" 32 -width 90
$rName = Add-Field $pR "Database" 64
$rUser = Add-Field $pR "User" 96
$rPass = Add-Field $pR "Password" 128 -Password
Add-Text $pR "Leave the password blank to keep the saved one. If the database or user doesn't exist yet, you can use a MariaDB administrator login once to create them (it isn't saved)." 160 36 | Out-Null
$rTest = Add-Button $pR "Test" 186 202 100
$rSave = Add-Button $pR "Save and restart" 294 202 150
$rStatus = Add-Status $pR 242 80

function Get-RecordAnswers {
    $nets = ""; if ($State.info) { $nets = (@($State.info.network) -join ",") }
    return @{ plant_network = $nets; db_host = $rHost.Text.Trim(); db_port = $rPort.Text.Trim(); db_name = $rName.Text.Trim(); db_user = $rUser.Text.Trim(); db_password = $rPass.Text; create_database = "n" }
}
function Test-Records {
    $a = Get-RecordAnswers
    $r = Invoke-Suite @("install_plant", "--check", "db") $a "--answers"
    if (-not $r.ok -and $r.missing) {
        if (Confirm-Box "There is no database named `"$($a.db_name)`" on that MariaDB server yet.`n`nCreate it now?") {
            $a.create_database = "y"; $r = Invoke-Suite @("install_plant", "--check", "db") $a "--answers"
        }
    }
    if (-not $r.ok -and $r.needsAdmin) {
        if (Confirm-Box ("The MariaDB user `"$($a.db_user)`" can't use that database yet:`n$($r.msg)`n`nUse a MariaDB administrator login (for example root) once, to create the database and the user and give that user rights on that database only? The administrator login is not saved.")) {
            $cred = Show-AdminLogin
            if ($cred) {
                $b = $a.Clone(); $b.admin_user = $cred.user; $b.admin_password = $cred.password; $cred = $null
                $x = Invoke-Suite @("install_plant", "--check", "dbadmin") $b "--answers"
                $b = $null
                if ($x.ok) { $r = Invoke-Suite @("install_plant", "--check", "db") $a "--answers" } else { $r = $x }
            }
        }
    }
    return $r
}
function Show-AdminLogin {
    $dlg = New-Object Windows.Forms.Form
    $dlg.Text = "MariaDB administrator login"; $dlg.FormBorderStyle = "FixedDialog"; $dlg.StartPosition = "CenterParent"
    $dlg.MaximizeBox = $false; $dlg.MinimizeBox = $false; $dlg.ClientSize = New-Object Drawing.Size(420, 200); $dlg.Font = $F.base
    $dlg.AutoScaleDimensions = New-Object Drawing.SizeF(96, 96); $dlg.AutoScaleMode = "Dpi"
    $l0 = New-Object Windows.Forms.Label; $l0.Text = "Used once to set up the suite's database and user. It is not saved."; $l0.SetBounds(16, 14, 390, 36); $dlg.Controls.Add($l0)
    $l1 = New-Object Windows.Forms.Label; $l1.Text = "Administrator user"; $l1.SetBounds(16, 60, 140, 22); $dlg.Controls.Add($l1)
    $u = New-Object Windows.Forms.TextBox; $u.Text = "root"; $u.SetBounds(160, 56, 240, 24); $dlg.Controls.Add($u)
    $l2 = New-Object Windows.Forms.Label; $l2.Text = "Password"; $l2.SetBounds(16, 96, 140, 22); $dlg.Controls.Add($l2)
    $pw = New-Object Windows.Forms.TextBox; $pw.UseSystemPasswordChar = $true; $pw.SetBounds(160, 92, 240, 24); $dlg.Controls.Add($pw)
    $ok = New-Object Windows.Forms.Button; $ok.Text = "Set it up"; $ok.SetBounds(196, 150, 100, 30); $ok.DialogResult = "OK"; $dlg.Controls.Add($ok)
    $no = New-Object Windows.Forms.Button; $no.Text = "Cancel"; $no.SetBounds(304, 150, 100, 30); $no.DialogResult = "Cancel"; $dlg.Controls.Add($no)
    $dlg.AcceptButton = $ok; $dlg.CancelButton = $no
    $res = $dlg.ShowDialog($form)
    $out = $null
    if ($res -eq "OK" -and $u.Text.Trim()) { $out = @{ user = $u.Text.Trim(); password = $pw.Text } }
    $pw.Text = ""; $dlg.Dispose()
    return $out
}
$rTest.add_Click({ [void](Run-Step $rStatus "Checking the records database..." { Test-Records }) })
$rSave.add_Click({
    if (-not (Confirm-Box "Move the suite to this records database and restart it? Screens go offline for a minute or two.")) { return }
    [void](Run-Step $rStatus "Checking, saving and restarting..." {
        $t = Test-Records
        if (-not $t.ok) { return $t }
        $p = @{ host = $rHost.Text.Trim(); port = $rPort.Text.Trim(); name = $rName.Text.Trim(); user = $rUser.Text.Trim(); password = $rPass.Text }
        $s = Invoke-Suite @("suiteadmin", "records-save") $p
        if (-not $s.ok) { return $s }
        Stop-SuiteNow
        $m = Invoke-Manage @("migrate", "--noinput", "--database", "default")
        if ($m.code -ne 0) {
            [void](Invoke-Suite @("suiteadmin", "records-undo"))
            $tail = (@($m.lines) | Select-Object -Last 3) -join " | "
            [void](Start-SuiteNow)
            return @{ ok = $false; error = "Building the tables in the new database failed, so the previous settings were put back and the suite restarted. ($tail)" }
        }
        [void](Invoke-Manage @("collectstatic", "--noinput"))
        Write-AdminLog "Records database changed to $($p.name) on $($p.host):$($p.port) as $($p.user)"
        $v = Start-SuiteNow
        $rPass.Text = ""
        if ($v) { return @{ ok = $true; msg = "Saved. The suite now uses $($p.name) on $($p.host) and is answering." } }
        return @{ ok = $false; error = "Saved, but the suite isn't answering yet. Check web-console.log on the Status page." }
    })
    Load-Info
})

# ================================================================ S2K and asset databases
$pD = New-Page "readonly" "S2K and asset databases" "Read-only connections. The suite signs in read-only and only runs SELECT queries; nothing is ever created or changed in these databases. Use read-only accounts."
$DB = @{}
$yy = 0
foreach ($g in @(@("s2k", "S2K database (open MOs)"), @("assets", "Asset database (changeover steps, specs)"))) {
    $k = $g[0]
    Add-Head $pD $g[1] $yy | Out-Null
    $DB[$k] = @{
        host = Add-Field $pD "Server" ($yy + 30); port = Add-Field $pD "Port" ($yy + 60) -width 90; name = Add-Field $pD "Database" ($yy + 90)
        user = Add-Field $pD "Read-only user" ($yy + 120); password = Add-Field $pD "Password" ($yy + 150) -Password
    }
    $DB[$k].test = Add-Button $pD "Test" 186 ($yy + 184) 90; $DB[$k].test.Tag = $k
    $DB[$k].save = Add-Button $pD "Save" 282 ($yy + 184) 90; $DB[$k].save.Tag = $k
    $DB[$k].remove = Add-Button $pD "Remove" 378 ($yy + 184) 90; $DB[$k].remove.Tag = $k
    $DB[$k].status = Add-Status $pD ($yy + 218) 36
    $yy += 266
}
function Get-DbFields([string]$k) { $d = $DB[$k]; return @{ host = $d.host.Text.Trim(); port = $d.port.Text.Trim(); name = $d.name.Text.Trim(); user = $d.user.Text.Trim(); password = $d.password.Text } }
foreach ($k in @("s2k", "assets")) {
    $DB[$k].test.add_Click({ param($src) $k = $src.Tag
        $r = Run-Step $DB[$k].status "Testing..." { $x = Invoke-SuiteCmd "connTest" @{ group = $k; fields = (Get-DbFields $k) }; if ($x.ok) { $x.data } else { $x } } })
    $DB[$k].save.add_Click({ param($src) $k = $src.Tag
        [void](Run-Step $DB[$k].status "Checking and saving..." { Invoke-SuiteCmd "connSave" @{ group = $k; fields = (Get-DbFields $k) } }); $DB[$k].password.Text = "" })
    $DB[$k].remove.add_Click({ param($src) $k = $src.Tag
        if (Confirm-Box "Remove this connection? Anything that uses it stops until it is set up again.") { [void](Run-Step $DB[$k].status "Removing..." { Invoke-SuiteCmd "connRemove" @{ group = $k } }); Load-Info } })
}

# ================================================================ InfluxDB
$pI = New-Page "influx" "InfluxDB" "Read machine speed and counts from InfluxDB, and/or copy the suite's records into a bucket so Grafana dashboards on InfluxDB use the suite's downtime rules. InfluxDB 3: choose 1.x and use the token as the password."
$iVer = Add-Combo $pI "Version" 0 @("2.x (Flux, organization and token)", "1.x or 3.x (InfluxQL)")
$iUrl = Add-Field $pI "Address" 32 -width 380
$iOrg = Add-Field $pI "Organization (2.x)" 64
$iUser = Add-Field $pI "User (1.x / 3.x)" 96
$iTok = Add-Field $pI "Token or password" 128 -Password -width 380
$iDb = Add-Field $pI "Database to read (1.x / 3.x)" 160
$iOut = Add-Field $pI "Copy suite records to" 192
Add-Text $pI "Bucket (2.x) or database (1.x) the suite writes its records to every minute. Leave blank to not copy. Create it in InfluxDB first. Machines are set to read from InfluxDB in Operator Kiosk > Admin > Connections > Machine data." 222 52 | Out-Null
$iTest = Add-Button $pI "Test" 186 278 90
$iSave = Add-Button $pI "Save" 282 278 90
$iRemove = Add-Button $pI "Remove" 378 278 90
$iStatus = Add-Status $pI 316 60
function Get-InfluxFields { return @{ url = $iUrl.Text.Trim(); version = $(if ($iVer.SelectedIndex -eq 1) { "1" } else { "2" }); org = $iOrg.Text.Trim(); user = $iUser.Text.Trim(); token = $iTok.Text; db = $iDb.Text.Trim(); out = $iOut.Text.Trim() } }
$iTest.add_Click({ [void](Run-Step $iStatus "Testing..." { $x = Invoke-SuiteCmd "connTest" @{ group = "influx"; fields = (Get-InfluxFields) }; if ($x.ok) { $x.data } else { $x } }) })
$iSave.add_Click({ [void](Run-Step $iStatus "Checking and saving..." { Invoke-SuiteCmd "connSave" @{ group = "influx"; fields = (Get-InfluxFields) } }); $iTok.Text = "" })
$iRemove.add_Click({ if (Confirm-Box "Remove the InfluxDB connection? Machines reading from it stop until it is set up again.") { [void](Run-Step $iStatus "Removing..." { Invoke-SuiteCmd "connRemove" @{ group = "influx" } }); Load-Info } })

# ================================================================ Paycom, Grafana, machine feeds
$pA = New-Page "accounts" "Paycom, Grafana and machine feeds" "Accounts the suite uses. Secrets are kept in the server's protected settings file and are never shown again; leave a field blank to keep the saved one."
Add-Head $pA "Paycom (operator list; an internet connection)" 0 | Out-Null
$aSid = Add-Field $pA "SID" 30
$aTok = Add-Field $pA "API token" 60 -Password
$aPSave = Add-Button $pA "Save" 186 94 90
$aPRemove = Add-Button $pA "Remove" 282 94 90
$aPStatus = Add-Status $pA 128 36
Add-Head $pA "Grafana service account token (for Reports)" 172 | Out-Null
$aGTok = Add-Field $pA "Token" 202 -Password -width 380
$aGSave = Add-Button $pA "Save" 186 236 90
$aGRemove = Add-Button $pA "Remove" 282 236 90
$aGStatus = Add-Status $pA 270 36
Add-Head $pA "Machine feed key (machines that push readings)" 314 | Out-Null
$aFNew = Add-Button $pA "Make a new key" 186 344 140
$aFRemove = Add-Button $pA "Remove" 332 344 90
$aFKey = Add-Field $pA "New key (copy it now)" 382 -width 380; $aFKey.ReadOnly = $true
$aFStatus = Add-Status $pA 414 36
$aPSave.add_Click({ [void](Run-Step $aPStatus "Saving..." { Invoke-SuiteCmd "connSave" @{ group = "paycom"; fields = @{ sid = $aSid.Text.Trim(); token = $aTok.Text } } }); $aTok.Text = "" })
$aPRemove.add_Click({ if (Confirm-Box "Remove the Paycom login?") { [void](Run-Step $aPStatus "Removing..." { Invoke-SuiteCmd "connRemove" @{ group = "paycom" } }) } })
$aGSave.add_Click({ [void](Run-Step $aGStatus "Saving..." { Invoke-SuiteCmd "connSave" @{ group = "grafana"; fields = @{ token = $aGTok.Text.Trim() } } }); $aGTok.Text = "" })
$aGRemove.add_Click({ if (Confirm-Box "Remove the Grafana token?") { [void](Run-Step $aGStatus "Removing..." { Invoke-SuiteCmd "connRemove" @{ group = "grafana" } }) } })
$aFNew.add_Click({
    if (-not (Confirm-Box "Make a new feed key? Gateways using the old key stop being accepted until they get the new one.")) { return }
    $r = Run-Step $aFStatus "Making a key..." { Invoke-SuiteCmd "connSave" @{ group = "feed"; fields = @{} } }
    if ($r -and $r.ok) { $aFKey.Text = "" + $r.key; Set-Status $aFStatus $true "New key saved. Copy it into each gateway (X-Feed-Key header); it isn't shown again." }
})
$aFRemove.add_Click({ if (Confirm-Box "Remove the feed key? Machines that push readings stop being accepted.") { [void](Run-Step $aFStatus "Removing..." { Invoke-SuiteCmd "connRemove" @{ group = "feed" } }); $aFKey.Text = "" } })

# ================================================================ Downtime alerts
$pL = New-Page "alerts" "Downtime alerts" "How alert emails and texts are sent. Who gets them, and Send a test, are in Operator Kiosk > Admin > Alerts."
Add-Head $pL "Mail server" 0 | Out-Null
$lHost = Add-Field $pL "Server" 30
$lPort = Add-Field $pL "Port" 60 -width 90
$lSec = Add-Combo $pL "Security" 90 @("STARTTLS", "SSL/TLS", "None") 160
$lUser = Add-Field $pL "User (optional)" 122
$lPass = Add-Field $pL "Password" 152 -Password
$lFrom = Add-Field $pL "Send from" 182
$lNet = Add-Check $pL "The mail server is on the internet (for example Office 365)" 212
$lSave = Add-Button $pL "Save" 186 242 90
$lRemove = Add-Button $pL "Remove" 282 242 90
$lStatus = Add-Status $pL 276 36
Add-Head $pL "Text messages (Twilio; an internet connection)" 320 | Out-Null
$tSid = Add-Field $pL "Account SID" 350
$tTok = Add-Field $pL "Auth token" 380 -Password
$tFrom = Add-Field $pL "Twilio number" 410
$tSave = Add-Button $pL "Save" 186 442 90
$tRemove = Add-Button $pL "Remove" 282 442 90
$tStatus = Add-Status $pL 476 36
$lSave.add_Click({
    $sec = @("starttls", "ssl", "none")[[Math]::Max(0, $lSec.SelectedIndex)]
    [void](Run-Step $lStatus "Saving..." { Invoke-SuiteCmd "alertConfigSave" @{ group = "smtp"; fields = @{ host = $lHost.Text.Trim(); port = $lPort.Text.Trim(); security = $sec; user = $lUser.Text.Trim(); password = $lPass.Text; from = $lFrom.Text.Trim(); internet = [bool]$lNet.Checked } } })
    $lPass.Text = "" })
$lRemove.add_Click({ if (Confirm-Box "Remove the mail server? Alert emails stop.") { [void](Run-Step $lStatus "Removing..." { Invoke-SuiteCmd "alertConfigSave" @{ group = "smtp-remove" } }); Load-Info } })
$tSave.add_Click({ [void](Run-Step $tStatus "Saving..." { Invoke-SuiteCmd "alertConfigSave" @{ group = "sms"; fields = @{ sid = $tSid.Text.Trim(); token = $tTok.Text.Trim(); from = $tFrom.Text.Trim() } } }); $tTok.Text = "" })
$tRemove.add_Click({ if (Confirm-Box "Remove Twilio? Alert texts stop.") { [void](Run-Step $tStatus "Removing..." { Invoke-SuiteCmd "alertConfigSave" @{ group = "sms-remove" } }); Load-Info } })

# ================================================================ Network and server
$pN = New-Page "network" "Network and server" "Which addresses screens may connect from (and the server may connect to), and how screens reach this server."
Add-Head $pN "Plant network" 0 | Out-Null
Add-Text $pN "One range or address per line, for example 192.168.100.0/24. Private ranges only. This server itself is always allowed. Saving also updates the Windows Firewall rule." 26 36 | Out-Null
$nNets = New-Object Windows.Forms.TextBox
$nNets.Multiline = $true; $nNets.ScrollBars = "Vertical"; $nNets.SetBounds(186, 66, 300, 84)
$pN.Controls.Add($nNets)
$nSave = Add-Button $pN "Save network" 186 156 130
$nStatus = Add-Status $pN 192 40
Add-Head $pN "This server" 240 | Out-Null
$nPort = Add-Field $pN "Port screens connect to" 270 -width 90
$nTz = Add-Field $pN "Plant time zone" 302
$nKey = Add-Field $pN "Station key (optional)" 334
Add-Text $pN "A new port, time zone or station key takes effect after a restart. A new port also updates the firewall rule and the desktop shortcuts; screens then need the new address." 364 36 | Out-Null
$nSrvSave = Add-Button $pN "Save" 186 404 90
$nSrvStatus = Add-Status $pN 440 52
$nSave.add_Click({
    $list = @($nNets.Text -split "[`r`n,]+" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($list.Count -eq 0) { Set-Status $nStatus $false "Keep at least one range."; return }
    [void](Run-Step $nStatus "Saving..." { Invoke-SuiteCmd "networkSet" @{ allowed = $list } })
    Load-Info
})
$nSrvSave.add_Click({
    $r = Run-Step $nSrvStatus "Saving..." { Invoke-Suite @("suiteadmin", "server-save") @{ port = $nPort.Text.Trim(); tz = $nTz.Text.Trim(); stationKey = $nKey.Text.Trim() } }
    if (-not $r -or -not $r.ok) { return }
    $notes = @()
    if ($r.portChanged) {
        try {
            Get-NetFirewallRule -DisplayName $FirewallRule -ErrorAction Stop | Set-NetFirewallRule -LocalPort $nPort.Text.Trim() -ErrorAction Stop
            $notes += "Firewall rule moved to port $($nPort.Text.Trim())."
        } catch { $notes += "Couldn't move the firewall rule to the new port ($($_.Exception.Message)); allow it by hand." }
        $desk = $(if ($env:PUBLIC) { Join-Path $env:PUBLIC "Desktop" } else { "" })
        @($(if ($desk) { Get-ChildItem -Path $desk -Filter "Production Suite - *.url" -ErrorAction SilentlyContinue })) | ForEach-Object {
            $t = Get-Content $_.FullName -Raw
            Set-Content -Path $_.FullName -Value ($t -replace "localhost:\d+", "localhost:$($nPort.Text.Trim())") -Encoding ASCII
        }
    }
    Write-AdminLog "Server settings saved (port $($nPort.Text.Trim()), time zone $($nTz.Text.Trim()))"
    if ($r.restart -and (Confirm-Box (("Saved. " + ($notes -join " ") + "`n`nRestart the suite now to use the new settings? Screens go offline for about a minute.").Trim()))) {
        [void](Run-Step $nSrvStatus "Restarting..." { Stop-SuiteNow; $v = Start-SuiteNow; if ($v) { @{ ok = $true; msg = "Restarted with the new settings." } } else { @{ ok = $false; error = "Restarted, but it isn't answering yet. See the Status page." } } })
    } elseif ($notes) { Set-Status $nSrvStatus $true ("Saved. " + ($notes -join " ")) }
})

# ---------------------------------------------------------------- filling in what's saved
function Load-Info {
    $r = Invoke-Suite @("suiteadmin", "info")
    if (-not $r.ok) { Set-Status $sStatus $false ("Couldn't read the suite's settings: " + (Get-Msg $r)); return }
    $State.info = $r
    $verLabel.Text = "Server admin  " + [char]0x00B7 + "  v$($r.version) rev $($r.revision)"
    $rec = $r.records
    $rHost.Text = "" + $rec.host; $rPort.Text = "" + $rec.port; $rName.Text = "" + $rec.name; $rUser.Text = "" + $rec.user
    $rPass.Text = ""
    if ($rec.hasPassword) { Set-Status $rStatus $null "Using $($rec.engine): $($rec.name) on $($rec.host) as $($rec.user)." }
    foreach ($k in @("s2k", "assets")) {
        $d = $r.conn.$k
        $DB[$k].host.Text = "" + $d.host; $DB[$k].port.Text = "" + $d.port; $DB[$k].name.Text = "" + $d.name; $DB[$k].user.Text = "" + $d.user; $DB[$k].password.Text = ""
        if (-not $DB[$k].status.Text) { Set-Status $DB[$k].status $null $(if ($d.on) { "Set up." } else { "Not set up." }) }
    }
    $i = $r.conn.influx
    $iVer.SelectedIndex = $(if ("" + $i.version -eq "1") { 1 } else { 0 })
    $iUrl.Text = "" + $i.url; $iOrg.Text = "" + $i.org; $iUser.Text = "" + $i.user; $iDb.Text = "" + $i.db; $iOut.Text = "" + $i.out; $iTok.Text = ""
    if (-not $iStatus.Text) { Set-Status $iStatus $null $(if ($i.on) { "Set up." } else { "Not set up." }) }
    $aSid.Text = "" + $r.conn.paycom.sid
    if (-not $aPStatus.Text) { Set-Status $aPStatus $null $(if ($r.conn.paycom.on) { "Set up." } else { "Not set up." }) }
    if (-not $aGStatus.Text) { Set-Status $aGStatus $null $(if ($r.conn.grafana.hasToken) { "A token is saved." } else { "No token saved." }) }
    if (-not $aFStatus.Text) { Set-Status $aFStatus $null $(if ($r.conn.feed.on) { "A key is set." } else { "No key set." }) }
    $sm = $r.conn.smtp
    $lHost.Text = "" + $sm.host; $lPort.Text = "" + $sm.port; $lUser.Text = "" + $sm.user; $lFrom.Text = "" + $sm.from; $lNet.Checked = [bool]$sm.internet
    $lSec.SelectedIndex = [Math]::Max(0, [array]::IndexOf(@("starttls", "ssl", "none"), "" + $sm.security))
    $tSid.Text = "" + $r.conn.sms.sid; $tFrom.Text = "" + $r.conn.sms.from
    $nNets.Text = (@($r.network) -join "`r`n")
    $nPort.Text = "" + $r.server.port; $nTz.Text = "" + $r.server.tz; $nKey.Text = "" + $r.server.stationKey
}

function Show-Page([string]$key) {
    foreach ($k in $pages.Keys) { $pages[$k].Visible = ($k -eq $key) }
    foreach ($k in $navLabels.Keys) {
        $on = $k -eq $key
        $navLabels[$k].ForeColor = $(if ($on) { $C.sideInk } else { $C.sideDim })
        $navLabels[$k].Font = $(if ($on) { $F.navOn } else { $F.nav })
        $navLabels[$k].BackColor = $(if ($on) { $C.sideOn } else { $C.side })
    }
    $title.Text = $pageText[$key][0]; $subtitle.Text = $pageText[$key][1]
    $State.page = $key
    if ($key -eq "status") { Update-StatusPage }
}

# ---------------------------------------------------------------- start
if (-not (Test-Path $Py) -or -not (Test-Path (Join-Path $InstallDir "manage.py"))) {
    [Windows.Forms.MessageBox]::Show("The suite isn't fully installed in $InstallDir (no Python environment). Run Setup.exe first.", "Production Suite Admin", "OK", "Warning") | Out-Null
    exit 1
}
Write-AdminLog "Production Suite Admin opened"
$form.add_Shown({
    Show-Page "status"
    Set-Busy $true; Set-Status $sStatus $null "Reading the settings..."
    Load-Info
    Set-Busy $false; if ($sStatus.Text -eq "Reading the settings...") { $sStatus.Text = "" }
    Update-StatusPage
    try {
        $timer = New-Object Windows.Forms.Timer
        $timer.Interval = 5000
        $timer.add_Tick({ if ($State.page -eq "status" -and -not $State.busy) { Update-StatusPage } })
        $timer.Start()
    } catch { }
})
[void]$form.ShowDialog()
Write-AdminLog "Production Suite Admin closed"
