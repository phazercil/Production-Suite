<#
  Run by the "Windows" check on GitHub (a real Windows Server with Windows PowerShell 5.1, as administrator):
  1. Code signing: signs a full build with a throwaway self-signed certificate and checks every file.
  2. Scheduled tasks: registers the suite's four tasks with the installer's own functions (schtasks.exe and
     task XML), checks their settings in Task Scheduler, finds the install, stops and deletes them.
#>
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
Set-Location $repo

Write-Host "== 1. Code signing"
$cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject "CN=Production Suite CI test" -CertStoreLocation Cert:\CurrentUser\My -NotAfter (Get-Date).AddDays(2)
& python packaging/build.py --sign --thumbprint $cert.Thumbprint
if ($LASTEXITCODE -ne 0) { throw "signed build failed" }
$stage = Join-Path $repo "dist\ProductionSuite-Setup"
$files = @(Get-ChildItem $stage -Recurse -File -Include *.exe, *.ps1 | Where-Object { $_.FullName -notmatch '\\wheels\\' })
$unsigned = @($files | Where-Object { -not (Get-AuthenticodeSignature $_.FullName).SignerCertificate })
if ($unsigned.Count) { throw "Not signed: $($unsigned.FullName -join ', ')" }
foreach ($need in @("Setup.exe", "installer\setup-gui.ps1", "installer\common.ps1", "app\desktop\SuiteAdmin.exe", "app\desktop\SuiteAdmin.ps1", "app\desktop\SuiteTray.ps1")) {
    $s = Get-AuthenticodeSignature (Join-Path $stage $need)
    if ($s.SignerCertificate.Thumbprint -ne $cert.Thumbprint) { throw "$need is not signed with the test certificate" }
    if (-not $s.TimeStamperCertificate) { throw "$need has no time stamp" }
}
# a signed script still parses and runs in Windows PowerShell 5.1
& powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "`$e=`$null; [void][Management.Automation.Language.Parser]::ParseFile('$stage\installer\common.ps1',[ref]`$null,[ref]`$e); if (`$e) { exit 1 }; . '$stage\installer\common.ps1'; exit 0"
if ($LASTEXITCODE -ne 0) { throw "the signed common.ps1 doesn't load in Windows PowerShell" }
Write-Host "All $($files.Count) .exe and .ps1 files are signed and time-stamped."
Remove-Item $cert.PSPath -Force

Write-Host "== 2. Scheduled tasks"
$install = Join-Path $env:TEMP "PSuiteCI"
New-Item -ItemType Directory -Force $install | Out-Null
foreach ($f in @("run-web.cmd", "run-ticker.cmd", "run-control.cmd", "run-update.cmd")) { Set-Content (Join-Path $install $f) "@echo off`r`nping -n 600 127.0.0.1 >nul" -Encoding ASCII }
. (Join-Path $repo "packaging\installer\common.ps1")
$script:LogFile = Join-Path $install "install.log"
$script:DefaultDir = Join-Path $install "nope"
if (Get-ExistingInstall) { throw "found an install before registering" }
Register-SuiteTasks $install "8000"
$found = Get-ExistingInstall
if ($found -ne $install) { throw "Get-ExistingInstall found '$found', expected '$install'" }
foreach ($n in @("Production Suite Web", "Production Suite Ticker", "Production Suite Control", "Production Suite Updater")) {
    $t = Get-ScheduledTask -TaskName $n
    if ($t.Principal.UserId -notin @("SYSTEM", "S-1-5-18") -and $t.Principal.UserId -notlike "*SYSTEM") { throw "$n runs as $($t.Principal.UserId)" }
    if ($t.Settings.DisallowStartIfOnBatteries -or $t.Settings.StopIfGoingOnBatteries) { throw "$n stops on batteries" }
    $boot = @($t.Triggers | Where-Object { $_.CimClass.CimClassName -eq "MSFT_TaskBootTrigger" }).Count
    if ($n -eq "Production Suite Updater") {
        if ($boot) { throw "the updater must not start with Windows" }
    } else {
        if (-not $boot) { throw "$n doesn't start with Windows" }
        if ($t.Settings.ExecutionTimeLimit -ne "PT0S") { throw "$n has a time limit ($($t.Settings.ExecutionTimeLimit))" }
        if ($t.Settings.RestartCount -lt 1) { throw "$n doesn't restart when it stops" }
    }
    Write-Host "  $n : OK ($($t.State))"
}
Stop-Suite $install
foreach ($n in @("Production Suite Web", "Production Suite Ticker", "Production Suite Control", "Production Suite Updater")) { & schtasks.exe /Delete /TN $n /F | Out-Null }
Write-Host "Scheduled tasks: registered, checked, found, stopped and removed."
