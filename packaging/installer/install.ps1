<#
  Production Suite installer, text mode (Windows 10/11, Windows Server 2016 or newer).

  Most people use Setup.exe, which shows the same steps in a window with a progress bar.
  This text-mode version is for unattended installs and for servers without a desktop:

      powershell -ExecutionPolicy Bypass -File installer\install.ps1
      powershell -ExecutionPolicy Bypass -File installer\install.ps1 -AnswersFile answers.json

  Running it again installs an update: your settings (.env), records and logs are kept.
  The steps themselves are in common.ps1, shared with the setup window.
#>
param(
    [string]$InstallDir = "",
    [string]$AnswersFile = ""
)

$ErrorActionPreference = "Continue"   # native programs are checked by exit code
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Here
. (Join-Path $Here "common.ps1")

function Say([string]$msg, [string]$color = "Gray") { Write-Host $msg -ForegroundColor $color; Write-SuiteLog $msg }
function Step([string]$msg) { Write-Host ""; Write-Host $msg -ForegroundColor Cyan; Write-SuiteLog "== $msg" }
function Pause-Close { if (-not $AnswersFile) { Write-Host ""; Read-Host "Press Enter to close" | Out-Null } }
function Fail([string]$msg) {
    Write-Host ""
    Write-Host $msg -ForegroundColor Red
    Write-SuiteLog "ERROR: $msg"
    Write-Host "A log of this install is in $script:LogFile" -ForegroundColor Yellow
    Pause-Close
    exit 1
}
function Ask([string]$prompt, [string]$default) {
    if ($AnswersFile) { return $default }
    $v = Read-Host "$prompt [$default]"
    if ([string]::IsNullOrWhiteSpace($v)) { return $default }
    return $v.Trim()
}
function Ask-YesNo([string]$prompt, [bool]$default) {
    if ($AnswersFile) { return $default }
    $hint = "y/N"; if ($default) { $hint = "Y/n" }
    while ($true) {
        $v = Read-Host "$prompt ($hint)"
        if ([string]::IsNullOrWhiteSpace($v)) { return $default }
        if ($v -match '^(y|yes)$') { return $true }
        if ($v -match '^(n|no)$') { return $false }
        Write-Host "Answer y or n." -ForegroundColor Yellow
    }
}
function Show-Pct([string]$activity, [double]$pct) {
    Write-Progress -Activity $activity -PercentComplete ([int][Math]::Max(0, [Math]::Min(100, $pct)))
}

# ---------------------------------------------------------------- run as administrator
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "The installer needs administrator rights. Windows will ask for permission." -ForegroundColor Yellow
    $argList = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    if ($InstallDir) { $argList += " -InstallDir `"$InstallDir`"" }
    if ($AnswersFile) { $argList += " -AnswersFile `"$AnswersFile`"" }
    try { Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList $argList | Out-Null }
    catch { Write-Host "Windows did not give administrator rights, so nothing was installed." -ForegroundColor Red; Pause-Close }
    exit
}

$Host.UI.RawUI.WindowTitle = "Production Suite installer"
Write-SuiteLog "---- Production Suite $script:SuiteVersion text-mode installer started by $([Environment]::UserName)"
Say ""
Say "Production Suite $script:SuiteVersion installer" White
Say "Press Enter to accept the value in [brackets]."

if (-not (Test-Path (Join-Path $Root "app\manage.py"))) {
    Fail "The 'app' folder is missing next to the installer. Extract the whole download first (right-click the zip > Extract All), then run setup from the extracted folder."
}
# Files from a downloaded zip are marked as coming from the internet; clear that so they can run.
Get-ChildItem -Path $Root -Recurse -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue

# ---------------------------------------------------------------- install folder
$existingDir = Get-ExistingInstall
if (-not $InstallDir) {
    if ($existingDir) {
        Say ""
        Say "Production Suite is already installed in $existingDir. This will update it; settings and records are kept." Yellow
        $InstallDir = $existingDir
    } else {
        $InstallDir = Ask "Install folder" $script:DefaultDir
    }
}
$InstallDir = $InstallDir.TrimEnd("\")
if ($InstallDir -notmatch '^[A-Za-z]:\\.+') { Fail "Use a full folder path such as C:\ProductionSuite." }
if ($InstallDir -match '\\Program Files') { Fail "Pick a folder outside Program Files (the suite writes its logs and settings in its own folder), for example C:\ProductionSuite." }

try {
    # ------------------------------------------------------------ Python
    Step "1. Python"
    $Python = Find-Python
    if (-not $Python) {
        Say "Python 3.10 to 3.14 (64-bit) was not found on this server."
        if (-not (Ask-YesNo "Download Python $script:PyVersion from python.org and install it for all users now" $true)) {
            Fail "Install 64-bit Python 3.12 for all users from https://www.python.org/downloads/windows/ and run this installer again."
        }
        $Python = Install-Python { param($p) Show-Pct "Installing Python $script:PyVersion" $p }
        Write-Progress -Activity "Installing Python" -Completed
    }
    Say "Using $Python" Green

    # ------------------------------------------------------------ files
    if ($existingDir) { Step "Stopping the running copy for the update"; Stop-Suite $existingDir; Say "Stopped." Green }
    Step "2. Copying the suite to $InstallDir"
    Copy-Suite $Root $InstallDir
    Say "Files copied." Green

    # ------------------------------------------------------------ packages
    Step "3. Installing the packages the suite needs"
    $VenvPy = Install-Packages $Root $InstallDir $Python { param($p) Show-Pct "Installing packages" $p }
    Write-Progress -Activity "Installing packages" -Completed
    Say "Packages installed." Green

    # ------------------------------------------------------------ setup questions
    Step "4. Setup"
    if ($AnswersFile) {
        $answers = @{}
        (Get-Content $AnswersFile -Raw | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $answers[$_.Name] = $_.Value }
        Invoke-SuiteSetup $InstallDir $VenvPy $answers { param($p) Show-Pct "Setting up" $p } { param($t) Say "  $t" }
        Write-Progress -Activity "Setting up" -Completed
    } else {
        # the questions are asked right here in this window
        Push-Location $InstallDir
        $global:LASTEXITCODE = -1
        try { & $VenvPy manage.py install_plant } catch { Pop-Location; throw "Couldn't run setup ($($_.Exception.Message))." }
        $setupCode = $LASTEXITCODE
        Pop-Location
        if ($setupCode -ne 0) { throw "Setup did not finish (see the message above). Fix it and run the installer again; anything you already typed is saved." }
    }
    $envNow = Read-EnvFile (Join-Path $InstallDir ".env")
    $Port = "8000"; if ($envNow["WEB_PORT"]) { $Port = $envNow["WEB_PORT"] }
    $StationKey = "" + $envNow["STATION_KEY"]

    # ------------------------------------------------------------ Windows
    Step "5. Starting the suite with Windows"
    Register-SuiteTasks $InstallDir $Port
    Say "Registered and started." Green
    Step "6. Windows Firewall (plant network only)"
    $warnings = Set-SuiteFirewall $InstallDir $VenvPy $Port
    if ($warnings.Count -eq 0) { Say "Port $Port is open to the plant network only." Green }
    foreach ($w in $warnings) { Say $w Yellow }
    Step "7. Checking the suite answers"
    if (-not (Wait-SuiteAnswers $Port $StationKey { param($p) Show-Pct "Waiting for the suite to start" $p })) {
        $log = Join-Path $InstallDir "logs\web-console.log"
        if (Test-Path $log) { Say "Last lines of $log :" Yellow; Get-Content $log -Tail 25 | ForEach-Object { Say "  $_" } }
        throw "The suite did not answer on port $Port within 90 seconds. The log lines above usually say why; the full logs are in $InstallDir\logs."
    }
    Write-Progress -Activity "Waiting for the suite to start" -Completed
    Say "The suite is running." Green
    Add-SuiteShortcuts $InstallDir $Port $StationKey $Python
} catch {
    Fail $_.Exception.Message
}

$hostName = $env:COMPUTERNAME
Say ""
Say "Production Suite is installed and running." Green
Say ""
Say "  On this server:      http://localhost:$Port/operator/   (shortcuts are on the desktop)"
Say "  From plant screens:  http://${hostName}:$Port/operator/    Operator Kiosk"
Say "                       http://${hostName}:$Port/scheduler/   Scheduler"
Say "                       http://${hostName}:$Port/data/        Data Kiosk"
Say "                       http://${hostName}:$Port/reports/     Reports"
Say "                       http://${hostName}:$Port/labor/       Labor Budget"
Say ""
if ($StationKey) { Say "  Each screen needs the station key once: add ?key=$StationKey to the address the first time." }
Say "  It starts by itself whenever this server starts. Logs: $InstallDir\logs"
Say "  To update later, run Setup.exe from a newer download. To remove it, run"
Say "  'Uninstall Production Suite.bat' in $InstallDir."
Pause-Close
exit 0
