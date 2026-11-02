<#
  The Production Suite tray icon (notification area, next to the clock). Started at every login by the
  "Production Suite" shortcut in the all-users Startup folder (SuiteAdmin.exe --tray). Dot-sourced by
  SuiteAdmin.ps1 -Tray, which provides Invoke-Control, Get-SuiteStatus, Write-UiLog, $Here and the colours.

  Green: running and answering. Amber: starting, or running but not answering yet. Red: stopped.
  Grey: the control service can't be reached. A balloon says when the suite stops or comes back.
  Right-click for Production Suite Admin, the Operator Kiosk, Start and Restart; double-click opens Admin.
#>
$trayMutex = New-Object Threading.Mutex($false, "Local\ProductionSuiteTray")
if (-not $trayMutex.WaitOne(0)) { Write-UiLog "tray icon already running"; return }

$TrayState = @{ last = ""; first = $true; port = "8000"; busy = $false }
$baseIcon = $null
try { $baseIcon = New-Object Drawing.Icon((Join-Path $Here "suite.ico"), 16, 16) } catch { $baseIcon = [Drawing.SystemIcons]::Application }

# The suite icon with a coloured dot in the corner.
function New-StatusIcon($dot) {
    $bmp = New-Object Drawing.Bitmap(16, 16)
    $g = [Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = "AntiAlias"
    $g.DrawIcon($baseIcon, (New-Object Drawing.Rectangle(0, 0, 16, 16)))
    $g.FillEllipse((New-Object Drawing.SolidBrush([Drawing.Color]::White)), 8, 8, 8, 8)
    $g.FillEllipse((New-Object Drawing.SolidBrush($dot)), 9, 9, 6, 6)
    $g.Dispose()
    return [Drawing.Icon]::FromHandle($bmp.GetHicon())
}
$Icons = @{
    running = New-StatusIcon ([Drawing.Color]::FromArgb(34, 160, 80))
    starting = New-StatusIcon ([Drawing.Color]::FromArgb(230, 160, 20))
    stopped = New-StatusIcon ([Drawing.Color]::FromArgb(210, 45, 45))
    unknown = New-StatusIcon ([Drawing.Color]::FromArgb(140, 140, 140))
}

$ni = New-Object Windows.Forms.NotifyIcon
$ni.Icon = $Icons.unknown
$ni.Text = "Production Suite"
$menu = New-Object Windows.Forms.ContextMenuStrip
$miStatus = $menu.Items.Add("Checking...")
$miStatus.Enabled = $false
[void]$menu.Items.Add((New-Object Windows.Forms.ToolStripSeparator))
$miAdmin = $menu.Items.Add("Open Production Suite Admin")
$miAdmin.Font = New-Object Drawing.Font($miAdmin.Font, [Drawing.FontStyle]::Bold)
$miKiosk = $menu.Items.Add("Open the Operator Kiosk")
$miStart = $menu.Items.Add("Start the suite")
$miRestart = $menu.Items.Add("Restart the suite")
[void]$menu.Items.Add((New-Object Windows.Forms.ToolStripSeparator))
$miHide = $menu.Items.Add("Hide this icon until next login")
$ni.ContextMenuStrip = $menu
$ni.Visible = $true

function Open-Admin {
    $exe = Join-Path $Here "SuiteAdmin.exe"
    try {
        if (Test-Path $exe) { Start-Process $exe }
        else { Start-Process powershell.exe -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", "`"$(Join-Path $Here 'SuiteAdmin.ps1')`"") }
    } catch { Write-UiLog "couldn't open Production Suite Admin: $($_.Exception.Message)" }
}

function Update-Tray {
    if ($TrayState.busy) { return }
    $st = Get-SuiteStatus
    $kind = "unknown"; $text = "Can't reach the control service"
    if (-not $st.offline) {
        $TrayState.port = "" + $st.port
        if ($st.answering) { $kind = "running"; $text = "Running  v$($st.answering.version) rev $($st.answering.revision)" }
        elseif ("" + $st.tasks.web -eq "Running") { $kind = "starting"; $text = "Starting, or not answering" }
        else { $kind = "stopped"; $text = "Stopped" }
    }
    $ni.Icon = $Icons[$kind]
    $tip = "Production Suite: $text"
    if ($tip.Length -gt 63) { $tip = $tip.Substring(0, 63) }
    $ni.Text = $tip
    $miStatus.Text = $text
    $miStart.Enabled = $kind -eq "stopped"
    $miRestart.Enabled = $kind -ne "unknown"
    if (-not $TrayState.first -and $kind -ne $TrayState.last) {
        if ($kind -eq "stopped") { $ni.ShowBalloonTip(8000, "Production Suite stopped", "Screens are offline and machine readings aren't being recorded. Right-click this icon to start it.", "Warning") }
        elseif ($kind -eq "running" -and $TrayState.last -in @("stopped", "starting", "unknown")) { $ni.ShowBalloonTip(5000, "Production Suite is running", $text, "Info") }
        elseif ($kind -eq "unknown") { $ni.ShowBalloonTip(8000, "Production Suite", "Can't reach the suite's control service on this server.", "Warning") }
    }
    if ($kind -ne $TrayState.last) { Write-UiLog "suite is $kind ($text)" }
    $TrayState.last = $kind; $TrayState.first = $false
}

$ni.add_DoubleClick({ Open-Admin })
$miAdmin.add_Click({ Open-Admin })
$miKiosk.add_Click({ Start-Process "http://localhost:$($TrayState.port)/operator/" })
$miStart.add_Click({
    $TrayState.busy = $true; $ni.Icon = $Icons.starting; $ni.Text = "Production Suite: starting"
    $r = Invoke-Control "/start"
    $TrayState.busy = $false; Update-Tray
    if (-not $r.ok) { $ni.ShowBalloonTip(8000, "Production Suite", (Get-Msg $r), "Error") }
})
$miRestart.add_Click({
    $q = [Windows.Forms.MessageBox]::Show("Restart Production Suite? Screens go offline for about a minute and reconnect by themselves.", "Production Suite", "YesNo", "Question")
    if ($q -ne "Yes") { return }
    $TrayState.busy = $true; $ni.Icon = $Icons.starting; $ni.Text = "Production Suite: restarting"
    $r = Invoke-Control "/restart"
    $TrayState.busy = $false; Update-Tray
    if (-not $r.ok) { $ni.ShowBalloonTip(8000, "Production Suite", (Get-Msg $r), "Error") }
})
$miHide.add_Click({ $ni.Visible = $false; [Windows.Forms.Application]::Exit() })

$trayTimer = New-Object Windows.Forms.Timer
$trayTimer.Interval = 15000
$trayTimer.add_Tick({ Update-Tray })
$trayTimer.Start()
Write-UiLog "tray icon shown"
Update-Tray
[Windows.Forms.Application]::Run()
$ni.Visible = $false
$ni.Dispose()
Write-UiLog "tray icon closed"
