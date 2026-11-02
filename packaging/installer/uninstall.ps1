<#
  Removes Production Suite from this server: stops it, removes the scheduled tasks, the firewall rule
  and the desktop shortcuts, and (only if you say so) the program folder.
  Your records stay in MariaDB; nothing in the database is touched.
#>
param([string]$InstallDir = "")

$ErrorActionPreference = "Continue"
$TaskWeb = "Production Suite Web"
$TaskTicker = "Production Suite Ticker"
$TaskUpdater = "Production Suite Updater"
$FirewallRule = "Production Suite"

function Say([string]$msg, [string]$color = "Gray") { Write-Host $msg -ForegroundColor $color }
function Pause-Close { Write-Host ""; Read-Host "Press Enter to close" | Out-Null }

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Say "Removing the suite needs administrator rights. Windows will ask for permission." Yellow
    try { Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`"" | Out-Null }
    catch { Say "Windows did not give administrator rights, so nothing was removed." Red; Pause-Close }
    exit
}
$Host.UI.RawUI.WindowTitle = "Production Suite uninstaller"

if (-not $InstallDir) {
    $t = Get-ScheduledTask -TaskName $TaskWeb -ErrorAction SilentlyContinue
    if ($t) { $InstallDir = $t.Actions[0].WorkingDirectory }
    elseif (Test-Path "C:\ProductionSuite\manage.py") { $InstallDir = "C:\ProductionSuite" }
}
Say ""
Say "Production Suite uninstaller" White
if ($InstallDir) { Say "Installed in: $InstallDir" }
Say "This stops the suite on this server. Your records in MariaDB are not touched."
$v = Read-Host "Continue (y/N)"
if ($v -notmatch '^(y|yes)$') { Say "Nothing was changed."; Pause-Close; exit 0 }

foreach ($name in @($TaskWeb, $TaskTicker, $TaskUpdater)) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
        Say "Removed scheduled task '$name'." Green
    }
}
if ($InstallDir) {
    $pattern = "*" + $InstallDir.TrimEnd("\") + "*"
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'cmd.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like $pattern -and ($_.CommandLine -like "*serve.py*" -or $_.CommandLine -like "*run-web.cmd*" -or $_.CommandLine -like "*run-ticker.cmd*") } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
}
Get-NetFirewallRule -DisplayName $FirewallRule -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction SilentlyContinue
Say "Removed the firewall rule." Green
Get-ChildItem -Path (Join-Path $env:PUBLIC "Desktop") -Filter "Production Suite - *.url" -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
Remove-Item -Path (Join-Path $env:PUBLIC "Desktop\Production Suite Admin.lnk") -Force -ErrorAction SilentlyContinue
Say "Removed the desktop shortcuts." Green

if ($InstallDir -and (Test-Path $InstallDir)) {
    $v = Read-Host "Also delete the program folder $InstallDir, including its logs (y/N)"
    if ($v -match '^(y|yes)$') {
        # make sure administrators can delete every file (setup 1.0.0 could leave files with no access at all)
        & takeown.exe /F "$InstallDir" /R /A /D Y 2>&1 | Out-Null
        & icacls.exe "$InstallDir" /reset /T /C /Q 2>&1 | Out-Null
        $envFile = Join-Path $InstallDir ".env"
        if (Test-Path $envFile) {
            $docs = [Environment]::GetFolderPath("MyDocuments")
            $keep = Join-Path $docs ("ProductionSuite-settings-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".env")
            Copy-Item $envFile $keep -Force
            Say "Your settings (including the database password) were saved to $keep so a reinstall can reuse them. Delete that file if you don't need it." Yellow
        }
        Set-Location $env:SystemRoot
        Remove-Item -Path $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
        if (Test-Path $InstallDir) { Say "Some files in $InstallDir couldn't be deleted (in use). Restart the server and delete the folder." Yellow }
        else { Say "Deleted $InstallDir." Green }
    } else {
        Say "The folder $InstallDir was kept. Running the installer again puts the suite back with the same settings."
    }
}
Say ""
Say "Production Suite has been removed from this server." Green
Pause-Close
