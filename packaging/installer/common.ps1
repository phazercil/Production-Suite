<#
  Shared install steps for Production Suite. Used by the setup window (setup-gui.ps1) and the
  text-mode installer (install.ps1). Nothing here talks to the user directly:
    - progress and log lines go through the $script:OnLog / $script:Pump hooks the caller sets;
    - every step throws a plain-English message when it fails.
  Windows PowerShell 5.1 compatible.
#>

$script:SuiteVersion = "1.9.5"
$script:SuiteRevision = 26
$script:PyVersion = "3.12.7"
$script:TaskWeb = "Production Suite Web"
$script:TaskTicker = "Production Suite Ticker"
$script:TaskControl = "Production Suite Control"
$script:TaskUpdater = "Production Suite Updater"
$script:FirewallRule = "Production Suite"
$script:DefaultDir = "C:\ProductionSuite"
$script:LogFile = Join-Path $env:ProgramData "ProductionSuite-install.log"

if (-not $script:OnLog) { $script:OnLog = { param([string]$line) } }
if (-not $script:Pump) { $script:Pump = { } }

function Write-SuiteLog([string]$line) {
    try { Add-Content -Path $script:LogFile -Value ("{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $line) -Encoding UTF8 } catch { }
    & $script:OnLog $line
}

function Wait-Pumped([int]$milliseconds) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.ElapsedMilliseconds -lt $milliseconds) { & $script:Pump; Start-Sleep -Milliseconds 40 }
}

# Quote one argument the way Windows programs (and Python) split a command line.
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

# Run a program without a console window, hand every output line to $OnLine, keep the window responsive.
# Returns the exit code.
function Invoke-Program([string]$exe, [string[]]$arguments, [string]$workDir = "", [scriptblock]$OnLine = $null, [int]$timeoutSec = 3600, [switch]$NoLog) {
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName = $exe
    $psi.Arguments = ConvertTo-ArgString $arguments
    if ($workDir) { $psi.WorkingDirectory = $workDir }
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [Text.Encoding]::UTF8
    $psi.EnvironmentVariables["PYTHONUTF8"] = "1"
    $psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"
    $psi.EnvironmentVariables["PYTHONUNBUFFERED"] = "1"
    $psi.EnvironmentVariables["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    $p = New-Object Diagnostics.Process
    $p.StartInfo = $psi
    try { [void]$p.Start() } catch { throw "Couldn't start $exe ($($_.Exception.Message))." }
    $out = $p.StandardOutput.ReadLineAsync()
    $err = $p.StandardError.ReadLineAsync()
    $sw = [Diagnostics.Stopwatch]::StartNew()
    try {
    while ($out -or $err) {
        $busy = $true
        if ($out -and $out.IsCompleted) {
            $line = $out.Result
            if ($null -eq $line) { $out = $null } else {
                if (-not $NoLog) { Write-SuiteLog $line }
                if ($OnLine) { & $OnLine $line }
                $out = $p.StandardOutput.ReadLineAsync(); $busy = $false
            }
        }
        if ($err -and $err.IsCompleted) {
            $line = $err.Result
            if ($null -eq $line) { $err = $null } else {
                if (-not $NoLog) { Write-SuiteLog $line }
                if ($OnLine) { & $OnLine $line }
                $err = $p.StandardError.ReadLineAsync(); $busy = $false
            }
        }
        & $script:Pump
        if ($busy) { Start-Sleep -Milliseconds 30 }
        if ($sw.Elapsed.TotalSeconds -gt $timeoutSec) {
            try { $p.Kill() } catch { }
            throw "$([IO.Path]::GetFileName($exe)) did not finish within $([int]($timeoutSec / 60)) minutes."
        }
    }
    } catch {
        # stopped (Cancel) or timed out: don't leave the program running in the background
        try { if (-not $p.HasExited) { $p.Kill() } } catch { }
        throw
    }
    $p.WaitForExit()
    return $p.ExitCode
}

# ---------------------------------------------------------------- what is already here
function Get-ExistingInstall {
    try {
        $r = Invoke-Schtasks @("/Query", "/TN", $script:TaskWeb, "/XML") 30 -Quiet
        if ($r.code -eq 0 -and (($r.out -join "`n") -match '<WorkingDirectory>([^<]+)</WorkingDirectory>')) { return $Matches[1].Trim() }
    } catch { Write-SuiteLog "Couldn't ask Task Scheduler about an existing install: $($_.Exception.Message)" }
    if (Test-Path (Join-Path $script:DefaultDir ".env")) { return $script:DefaultDir }
    return $null
}

# ---------------------------------------------------------------- scheduled tasks
# Everything goes through schtasks.exe with a time limit. The ScheduledTasks and CIM cmdlets go through
# Windows Management (WMI), which can hang on some servers and freeze setup with no error at all.
function Invoke-Schtasks([string[]]$arguments, [int]$timeoutSec = 60, [switch]$Quiet) {
    $out = New-Object Collections.ArrayList
    $sink = { param($l) [void]$out.Add($l) }.GetNewClosure()
    $code = Invoke-Program "schtasks.exe" $arguments "" $sink -timeoutSec $timeoutSec -NoLog:$Quiet
    return @{ code = $code; out = $out }
}

# A task definition for schtasks /Create /XML: runs as the system account, at start-up (or only on request),
# restarts if it stops, never stops on batteries, and never overlaps itself.
function New-SuiteTaskXml([string]$description, [string]$cmdFile, [string]$workDir, [bool]$atStartup, [string]$timeLimit) {
    $e = { param($v) [Security.SecurityElement]::Escape("$v") }
    $trigger = ""
    $restart = ""
    if ($atStartup) {
        $trigger = "<Triggers><BootTrigger><Enabled>true</Enabled></BootTrigger></Triggers>"
        $restart = "<RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>"
    }
    return @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>$(& $e $description)</Description></RegistrationInfo>
  $trigger
  <Principals><Principal id="Author"><UserId>S-1-5-18</UserId><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>$timeLimit</ExecutionTimeLimit>
    $restart
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>cmd.exe</Command><Arguments>/c "$(& $e $cmdFile)"</Arguments><WorkingDirectory>$(& $e $workDir)</WorkingDirectory></Exec></Actions>
</Task>
"@
}

function Register-SuiteTask([string]$name, [string]$xml) {
    $file = Join-Path $env:TEMP ("ProductionSuite-task-" + [guid]::NewGuid().ToString("N") + ".xml")
    [IO.File]::WriteAllText($file, $xml, [Text.Encoding]::Unicode)
    try { $r = Invoke-Schtasks @("/Create", "/F", "/TN", $name, "/XML", $file) 60 }
    finally { Remove-Item $file -Force -ErrorAction SilentlyContinue }
    if ($r.code -ne 0) { throw "Couldn't register the scheduled task '$name': $(($r.out | Where-Object { $_ }) -join ' ')" }
}

function Read-EnvFile([string]$path) {
    $h = @{}
    if (Test-Path $path) {
        foreach ($line in Get-Content $path -Encoding UTF8) {
            if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
                $v = $Matches[2].Trim()
                if ($v.Length -ge 2 -and ($v[0] -eq '"' -or $v[0] -eq "'") -and $v[-1] -eq $v[0]) { $v = $v.Substring(1, $v.Length - 2) }
                $h[$Matches[1]] = $v
            }
        }
    }
    return $h
}

# The version already installed in a folder (from install-info.json), or $null.
function Get-InstalledInfo([string]$dir) {
    $f = Join-Path $dir "install-info.json"
    if (-not (Test-Path $f)) { return $null }
    try { return (Get-Content $f -Raw | ConvertFrom-Json) } catch { return $null }
}

# -1, 0 or 1, comparing version strings like 1.2.0
function Compare-SuiteVersion([string]$a, [string]$b) {
    $va = $null; $vb = $null
    if (-not [Version]::TryParse(("" + $a), [ref]$va)) { $va = [Version]"0.0" }
    if (-not [Version]::TryParse(("" + $b), [ref]$vb)) { $vb = [Version]"0.0" }
    return $va.CompareTo($vb)
}

# ---------------------------------------------------------------- Python
function Test-Python([string]$exe, [string[]]$pre) {
    $cmd = Get-Command $exe -ErrorAction SilentlyContinue
    if (-not $cmd) { return $null }
    if ($cmd.Source -like "*\WindowsApps\*") { return $null }   # the Microsoft Store "python" shortcut, not a real install
    try {
        $global:LASTEXITCODE = -1
        $out = & $exe @pre -c "import sys; print(sys.version_info[0]*100+sys.version_info[1]); print(sys.executable); print(64 if sys.maxsize > 2**32 else 32)" 2>$null
    } catch { return $null }
    if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
    $lines = @($out)
    if ($lines.Count -lt 3) { return $null }
    $ver = 0
    if (-not [int]::TryParse($lines[0], [ref]$ver)) { return $null }
    $path = $lines[1].Trim()
    if ($ver -lt 310 -or $ver -gt 314) { return $null }
    if ($lines[2].Trim() -ne "64") { return $null }
    if ($path -like "*\WindowsApps\*") { return $null }   # Microsoft Store shortcut, not a real install
    if (-not (Test-Path $path)) { return $null }
    return $path
}

function Find-Python {
    foreach ($v in @("3.12", "3.13", "3.14", "3.11", "3.10")) { $p = Test-Python "py" @("-$v"); if ($p) { return $p } }
    $p = Test-Python "python" @(); if ($p) { return $p }
    foreach ($d in @("Python312", "Python313", "Python314", "Python311")) {
        $exe = Join-Path $env:ProgramFiles "$d\python.exe"
        if (Test-Path $exe) { $p = Test-Python $exe @(); if ($p) { return $p } }
    }
    return $null
}

# Downloads Python from python.org (reporting 0-100 through $OnPercent), checks the signature, installs it for all users.
function Install-Python([scriptblock]$OnPercent) {
    $url = "https://www.python.org/ftp/python/$script:PyVersion/python-$script:PyVersion-amd64.exe"
    $tmp = Join-Path $env:TEMP "python-$script:PyVersion-amd64.exe"
    Write-SuiteLog "Downloading $url"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        $req = [Net.HttpWebRequest]::Create($url)
        $req.Proxy = [Net.WebRequest]::GetSystemWebProxy()
        $req.Proxy.Credentials = [Net.CredentialCache]::DefaultCredentials
        $req.Timeout = 30000
        $resp = $req.GetResponse()
        $total = $resp.ContentLength
        $in = $resp.GetResponseStream()
        $fs = [IO.File]::Create($tmp)
        try {
            $buf = New-Object byte[] 131072
            $done = 0
            while (($n = $in.Read($buf, 0, $buf.Length)) -gt 0) {
                $fs.Write($buf, 0, $n); $done += $n
                if ($total -gt 0 -and $OnPercent) { & $OnPercent ([int](70 * $done / $total)) }
                & $script:Pump
            }
        } finally { $fs.Close(); $in.Close(); $resp.Close() }
    } catch {
        throw "Couldn't download Python ($($_.Exception.Message)). If this server has no internet, install 64-bit Python 3.12 for all users by hand (from python.org, copied over on a USB stick), then run setup again."
    }
    $sig = Get-AuthenticodeSignature -FilePath $tmp
    if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notmatch "Python Software Foundation") {
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
        throw "The downloaded Python installer is not signed by the Python Software Foundation, so it was not run."
    }
    Write-SuiteLog "Installing Python $script:PyVersion"
    $proc = Start-Process -FilePath $tmp -ArgumentList "/quiet", "InstallAllUsers=1", "PrependPath=1", "Include_test=0", "Include_launcher=1" -PassThru
    $tick = 0
    while (-not $proc.HasExited) {
        Wait-Pumped 500; $tick++
        if ($OnPercent) { & $OnPercent ([Math]::Min(98, 70 + $tick / 2)) }
    }
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    if ($proc.ExitCode -ne 0) { throw "The Python installer stopped with code $($proc.ExitCode)." }
    $p = Test-Python (Join-Path $env:ProgramFiles "Python312\python.exe") @()
    if (-not $p) { throw "Python was installed but can't be found in $env:ProgramFiles\Python312. Restart the server and run setup again." }
    if ($OnPercent) { & $OnPercent 100 }
    return $p
}

# ---------------------------------------------------------------- the running copy
function Stop-Suite([string]$dir) {
    foreach ($t in @($script:TaskWeb, $script:TaskTicker, $script:TaskControl)) {
        try { [void](Invoke-Schtasks @("/End", "/TN", $t) 60 -Quiet) } catch { Write-SuiteLog "Couldn't stop '$t': $($_.Exception.Message)" }
    }
    if ($dir) {
        # Ending a task stops its cmd.exe; also stop the suite's Python processes and the tray icon / Admin windows
        # (any user), which would keep their files locked. Matched by program path, without Windows Management.
        $root = $dir.TrimEnd("\") + "\"
        foreach ($pf in @("web.pid", "ticker.pid")) {
            $f = Join-Path $dir "logs\$pf"
            if (Test-Path $f) { try { Stop-Process -Id ([int](Get-Content $f -Raw).Trim()) -Force -ErrorAction SilentlyContinue } catch { } }
        }
        Get-Process -Name python, pythonw, SuiteAdmin -ErrorAction SilentlyContinue |
            Where-Object { $_.Path -and $_.Path.StartsWith($root, [StringComparison]::OrdinalIgnoreCase) } |
            ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }
        Wait-Pumped 2000
    }
}

# ---------------------------------------------------------------- files
# Only administrators and the system account (which runs the suite) may read or change the install folder:
# its settings hold the database passwords. The rule is set on the folder itself and every file inside
# inherits it. Taking ownership and resetting the files first also repairs folders left unreadable by
# setup 1.0.0, whose permission step could strip all access from the files themselves.
function Set-FolderAccess([string]$dir) {
    if (-not (Test-Path $dir)) { return }
    Write-SuiteLog "Setting the permissions on $dir (Administrators and SYSTEM only)"
    [void](Invoke-Program "takeown.exe" @("/F", $dir, "/R", "/A", "/D", "Y"))
    $code = Invoke-Program "icacls.exe" @($dir, "/inheritance:r", "/grant:r", "*S-1-5-32-544:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F", "/C", "/Q")
    if ($code -ne 0) { Write-SuiteLog "Couldn't set the permissions on $dir (icacls code $code)." }
    [void](Invoke-Program "icacls.exe" @((Join-Path $dir "*"), "/reset", "/T", "/C", "/Q"))
    # check the files really are readable, so a permissions problem shows up here and not later
    $probe = Get-ChildItem -Path $dir -File -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($probe) {
        try { [void][IO.File]::ReadAllBytes($probe.FullName) } catch {
            throw "Windows refused access to the files in $dir ($($_.Exception.Message)). Make sure you are signed in as an administrator, then run setup again."
        }
    }
}

function Test-FolderReadable([string]$dir) {
    foreach ($f in @("manage.py", ".env", "requirements-windows.txt")) {
        $p = Join-Path $dir $f
        if (Test-Path $p) { try { [void][IO.File]::ReadAllBytes($p) } catch { return $false } }
    }
    return $true
}

function Copy-Suite([string]$root, [string]$installDir) {
    $source = Join-Path $root "app"
    $fresh = -not (Test-Path $installDir)
    if ($fresh) { New-Item -ItemType Directory -Path $installDir -Force | Out-Null }
    elseif (-not (Test-FolderReadable $installDir)) { Set-FolderAccess $installDir }   # repairs a folder left unreadable
    # robocopy only copies files that changed; settings, records, logs and the Python environment are left alone
    # /E copies everything; settings, records, logs and the Python environment already in the folder are left alone.
    $code = Invoke-Program "robocopy.exe" @($source, $installDir, "/E", "/R:2", "/W:2", "/XD", ".venv", "logs", "staticfiles", "__pycache__",
        "/XF", ".env", ".env.backup-*", "*.sqlite3", "*.sqlite3-journal", "/NFL", "/NDL", "/NJH", "/NJS", "/NP")
    if ($code -ge 8) { throw "Copying the files failed (robocopy code $code). Check that nothing has files in $installDir open, then try again." }
    $code = Invoke-Program "robocopy.exe" @((Join-Path $root "installer"), (Join-Path $installDir "installer"), "/E", "/R:2", "/W:2", "/NFL", "/NDL", "/NJH", "/NJS", "/NP")
    if ($code -ge 8) { throw "Copying the installer files failed (robocopy code $code)." }
    if (Test-Path (Join-Path $root "wheels")) {   # keep the packages with the install, so it can be repaired without internet
        $code = Invoke-Program "robocopy.exe" @((Join-Path $root "wheels"), (Join-Path $installDir "wheels"), "/MIR", "/R:2", "/W:2", "/NFL", "/NDL", "/NJH", "/NJS", "/NP")
        if ($code -ge 8) { throw "Copying the packages failed (robocopy code $code)." }
    }
    foreach ($f in @("Uninstall Production Suite.bat")) {
        $src = Join-Path $root $f
        if (Test-Path $src) { Copy-Item $src (Join-Path $installDir $f) -Force }
    }
    New-Item -ItemType Directory -Path (Join-Path $installDir "logs") -Force | Out-Null
    if ($fresh -or -not (Test-FolderReadable $installDir)) { Set-FolderAccess $installDir }
}

# ---------------------------------------------------------------- packages (reports 0-100 through $OnPercent)
function Install-Packages([string]$root, [string]$installDir, [string]$python, [scriptblock]$OnPercent) {
    $venv = Join-Path $installDir ".venv"
    $venvPy = Join-Path $venv "Scripts\python.exe"
    $ok = $false
    if (Test-Path $venvPy) {
        $ok = ((Invoke-Program $venvPy @("-c", "import sys")) -eq 0)
        if (-not $ok) { Write-SuiteLog "The existing Python environment no longer works (Python was probably updated); rebuilding it."; Remove-Item $venv -Recurse -Force }
    }
    if (-not $ok) {
        if ($OnPercent) { & $OnPercent 5 }
        $code = Invoke-Program $python @("-m", "venv", $venv)
        if ($code -ne 0 -or -not (Test-Path $venvPy)) { throw "Couldn't create the Python environment in $venv." }
    }
    if ($OnPercent) { & $OnPercent 20 }
    $req = Join-Path $installDir "requirements-windows.txt"
    $wheels = Join-Path $root "wheels"
    if (-not (Test-Path (Join-Path $wheels "*.whl"))) { $wheels = Join-Path $installDir "wheels" }
    # Same packages as last time and the environment still works: nothing to install.
    $sig = (Get-Content $req -Raw) + "|" + ((@(Get-ChildItem -Path $wheels -Filter *.whl -ErrorAction SilentlyContinue | ForEach-Object { $_.Name }) | Sort-Object) -join ",")
    $sha = [Security.Cryptography.SHA256]::Create()
    $script:PackagesHash = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($sig))).Replace("-", "")
    $info = Get-InstalledInfo $installDir
    if ($ok -and $info -and $info.packagesHash -eq $script:PackagesHash -and
        (Invoke-Program $venvPy @("-c", "import django, waitress, whitenoise, pymysql, openpyxl")) -eq 0) {
        Write-SuiteLog "The packages haven't changed since the last install; skipping this step."
        if ($OnPercent) { & $OnPercent 100 }
        return $venvPy
    }
    $count = [Math]::Max(1, @(Get-ChildItem -Path $wheels -Filter *.whl -ErrorAction SilentlyContinue).Count)
    $state = @{ n = 0 }
    $onLine = { param($l) if ($l -match '^(Processing|Collecting|Requirement already satisfied)') { $state.n++; if ($OnPercent) { & $OnPercent ([Math]::Min(90, 20 + 70 * $state.n / $count)) } } }.GetNewClosure()
    if (Test-Path (Join-Path $wheels "*.whl")) {
        # The packages ship inside this download, so no internet, proxy or pypi.org access is needed.
        Write-SuiteLog "Installing the packages that came with this download (no internet needed)."
        $code = Invoke-Program $venvPy @("-m", "pip", "install", "--no-index", "--find-links", $wheels, "--no-warn-script-location", "-r", $req) -OnLine $onLine
        if ($code -ne 0) {
            throw ("Installing the packages from the 'wheels' folder failed. Check that the whole download was extracted " +
                   "(the 'wheels' folder sits next to 'Setup.exe') and that antivirus did not quarantine files in it, then run setup again.")
        }
    } else {
        Write-SuiteLog "The 'wheels' folder is missing from this download, so the packages are downloaded from pypi.org."
        $code = Invoke-Program $venvPy @("-m", "pip", "install", "--no-warn-script-location", "-r", $req) -OnLine $onLine
        if ($code -ne 0) { throw "Installing packages failed: this server couldn't download them from pypi.org. Extract the full ProductionSuite-Setup.zip again; it includes a 'wheels' folder so no internet is needed." }
    }
    if ($OnPercent) { & $OnPercent 100 }
    return $venvPy
}

# ---------------------------------------------------------------- setup questions answered by the window
function Write-Answers([hashtable]$answers) {
    $file = Join-Path $env:TEMP ("ps-answers-" + [guid]::NewGuid().ToString("N") + ".json")
    # The answers hold database passwords: only this account may read the file, and it is deleted right after use.
    [IO.File]::WriteAllText($file, ($answers | ConvertTo-Json -Depth 3), (New-Object Text.UTF8Encoding($false)))
    try {
        $acl = Get-Acl $file
        $acl.SetAccessRuleProtection($true, $false)
        $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($me, "FullControl", "Allow")))
        Set-Acl $file $acl
    } catch { }
    return $file
}

# Checks one page of answers with the suite itself. Returns the parsed result (ok, msg, ...).
function Test-SetupPage([string]$installDir, [string]$venvPy, [string]$kind, [hashtable]$answers) {
    $file = Write-Answers $answers
    $state = @{ line = $null }
    try {
        $code = Invoke-Program $venvPy @("manage.py", "install_plant", "--check", $kind, "--answers", $file) $installDir {
            param($l) if ($l.StartsWith("@@RESULT ")) { $state.line = $l.Substring(9) }
        }.GetNewClosure() 120
    } finally { Remove-Item $file -Force -ErrorAction SilentlyContinue }
    if (-not $state.line) { return [pscustomobject]@{ ok = $false; msg = "The check didn't answer (code $code). See $script:LogFile."; detail = "" } }
    return ($state.line | ConvertFrom-Json)
}

# Writes the settings, builds the tables and the first login. Reports 0-100 through $OnPercent and status text through $OnStatus.
function Invoke-SuiteSetup([string]$installDir, [string]$venvPy, [hashtable]$answers, [scriptblock]$OnPercent, [scriptblock]$OnStatus) {
    $file = Write-Answers $answers
    $state = @{ pct = 0; errors = New-Object Collections.ArrayList }
    $onLine = {
        param($l)
        if ($l -match '^@@PROGRESS (\d+) (.*)$') {
            $state.pct = [int]$Matches[1]
            if ($OnPercent) { & $OnPercent $state.pct }
            if ($OnStatus) { & $OnStatus $Matches[2] }
        } elseif ($l -match '^\s*Applying ') {
            # migrations: creep forward between 28% and 78% of this stage
            $state.pct = [Math]::Min(78, $state.pct + 1)
            if ($OnPercent) { & $OnPercent $state.pct }
        } elseif ($l.StartsWith("@@ERROR ")) {
            if (-not $state.errors.Contains($l.Substring(8))) { [void]$state.errors.Add($l.Substring(8)) }
        }
    }.GetNewClosure()
    try {
        $code = Invoke-Program $venvPy @("manage.py", "install_plant", "--answers", $file, "--progress") $installDir $onLine 1800
    } finally { Remove-Item $file -Force -ErrorAction SilentlyContinue }
    if ($code -ne 0) {
        $why = ($state.errors | Select-Object -First 2) -join "`n"
        if (-not $why) { $why = "See the details below or $script:LogFile." }
        throw "Setup did not finish.`n$why"
    }
}

# ---------------------------------------------------------------- Windows: tasks, firewall, shortcuts
function Register-SuiteTasks([string]$installDir, [string]$port) {
    foreach ($t in @(@($script:TaskWeb, "run-web.cmd", "Production Suite screens: Operator Kiosk, Scheduler and Data Kiosk on port $port."),
                     @($script:TaskTicker, "run-ticker.cmd", "Production Suite minute ticker: machine readings and timed S2K / Paycom pulls."),
                     @($script:TaskControl, "run-control.cmd", "Production Suite control service: lets Production Suite Admin and the tray icon start, stop and set up the suite without Windows admin rights (this server only)."))) {
        if (-not (Test-Path (Join-Path $installDir $t[1]))) { continue }
        Write-SuiteLog "Registering the scheduled task '$($t[0])'"
        Register-SuiteTask $t[0] (New-SuiteTaskXml $t[2] (Join-Path $installDir $t[1]) $installDir $true "PT0S")
        & $script:Pump
    }
    foreach ($t in @($script:TaskWeb, $script:TaskTicker, $script:TaskControl)) {
        Write-SuiteLog "Starting '$t'"
        $r = Invoke-Schtasks @("/Run", "/TN", $t) 60
        if ($r.code -ne 0 -and $t -ne $script:TaskControl) { throw "Couldn't start the scheduled task '$t': $(($r.out | Where-Object { $_ }) -join ' ')" }
    }
    & $script:Pump
    Register-UpdaterTask $installDir
}

# The updater task: no schedule of its own. The suite starts it (Admin > Updates, or a file in updates\inbox) to stop
# the suite, install an update and start it again; run-update.cmd does nothing if no update was asked for.
# Made with schtasks.exe (with a time limit) after the suite is already running, so a problem here can never
# hold up or stop the install: at worst, updates from Admin > Updates need setup to be run again.
function Register-UpdaterTask([string]$installDir) {
    Write-SuiteLog "Registering the scheduled task '$($script:TaskUpdater)'"
    try {
        Register-SuiteTask $script:TaskUpdater (New-SuiteTaskXml "Production Suite updates: installs an update file when the suite asks for it (Operator Kiosk > Admin > Updates)." `
            (Join-Path $installDir "run-update.cmd") $installDir $false "PT2H")
        Write-SuiteLog "Registered the updater task."
    } catch {
        Write-SuiteLog "Couldn't register the updater task ($($_.Exception.Message)); updates from Admin > Updates won't start until setup runs again."
    }
}

# Runs a script block in a separate PowerShell process and waits at most $timeoutSec, keeping the window
# responsive. Returns what the script block output; throws if it didn't finish in time.
function Invoke-Bounded([scriptblock]$block, [object[]]$argList, [int]$timeoutSec = 90) {
    $j = Start-Job -ScriptBlock $block -ArgumentList $argList
    $sw = [Diagnostics.Stopwatch]::StartNew()
    try {
        while ($j.State -eq "Running" -or $j.State -eq "NotStarted") {
            & $script:Pump
            Start-Sleep -Milliseconds 100
            if ($sw.Elapsed.TotalSeconds -gt $timeoutSec) { throw "Windows didn't answer within $timeoutSec seconds" }
        }
        return (Receive-Job $j -ErrorAction SilentlyContinue)
    } finally { Stop-Job $j -ErrorAction SilentlyContinue; Remove-Job $j -Force -ErrorAction SilentlyContinue }
}

# Opens the port to the plant network only. Returns a list of warnings (empty when all is well).
function Set-SuiteFirewall([string]$installDir, [string]$venvPy, [string]$port) {
    $warn = New-Object Collections.ArrayList
    $state = @{ ranges = New-Object Collections.ArrayList }
    [void](Invoke-Program $venvPy @("manage.py", "network", "--plain") $installDir {
        param($l) $x = "$l".Trim(); if ($x -match '^\d{1,3}(\.\d{1,3}){3}/\d{1,2}$') { [void]$state.ranges.Add($x) }
    }.GetNewClosure())
    $ranges = @($state.ranges)
    if ($ranges.Count -eq 0) {
        $ranges = @("192.168.100.0/24")
        [void]$warn.Add("Couldn't read the plant network setting; the firewall allows 192.168.100.0/24. Check Operator Kiosk > Admin > Network.")
    }
    # The firewall cmdlets also go through Windows Management, so they run in a separate process with a time limit.
    $job = {
        param($rule, $port, $ranges)
        $out = @{ ok = $true; msg = ""; public = @() }
        try {
            Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction SilentlyContinue
            New-NetFirewallRule -DisplayName $rule -Description "Plant screens reach the Production Suite on this port from the plant network only (Operator Kiosk > Admin > Network). Public networks and internet addresses are not allowed." `
                -Direction Inbound -Protocol TCP -LocalPort $port -RemoteAddress $ranges -Action Allow -Profile Domain, Private -ErrorAction Stop | Out-Null
        } catch { $out.ok = $false; $out.msg = $_.Exception.Message }
        try { $out.public = @(Get-NetConnectionProfile -ErrorAction SilentlyContinue | Where-Object { $_.NetworkCategory -eq "Public" } | ForEach-Object { "" + $_.Name }) } catch { }
        $out
    }
    try {
        $r = Invoke-Bounded $job @($script:FirewallRule, $port, $ranges) 120
        if ($r.ok) {
            Write-SuiteLog "Port $port is open to the plant network only: $($ranges -join ', ')"
            try { [void](Invoke-Program $venvPy @("manage.py", "network", "--sync-firewall") $installDir -timeoutSec 120) } catch { }   # records the result for Admin > Network
        } else {
            [void]$warn.Add("Couldn't add the firewall rule ($($r.msg)). Allow TCP port $port inbound from $($ranges -join ', ') only, by hand.")
        }
        if (@($r.public).Count -gt 0) {
            [void]$warn.Add("Windows treats this network as Public ($(@($r.public) -join ', ')), so plant screens can't connect yet. " +
                            "If this is the plant network, set it to Private: Settings > Network & Internet > (your connection) > Network profile type: Private.")
        }
    } catch {
        [void]$warn.Add("Couldn't set up Windows Firewall ($($_.Exception.Message)). Allow TCP port $port inbound from $($ranges -join ', ') only, by hand.")
    }
    return ,$warn
}

# Waits until the web app answers (up to 90 s). Returns $true or $false.
function Wait-SuiteAnswers([string]$port, [string]$stationKey, [scriptblock]$OnPercent) {
    for ($i = 0; $i -lt 45; $i++) {
        Wait-Pumped 2000
        if ($OnPercent) { & $OnPercent ([int](100 * $i / 45)) }
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port/api/data/state" -Headers @{ "X-Station-Key" = $stationKey } -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
            if ($r.StatusCode -eq 200) { return $true }
        } catch { }
    }
    return $false
}

function Add-SuiteShortcuts([string]$installDir, [string]$port, [string]$stationKey, [string]$python) {
    $q = ""
    if ($stationKey) { $q = "?key=$stationKey" }
    $desktop = Join-Path $env:PUBLIC "Desktop"
    foreach ($item in @(@("Operator Kiosk", "operator"), @("Scheduler", "scheduler"), @("Data Kiosk", "data"), @("Reports", "reports"), @("Labor Budget", "labor"))) {
        $file = Join-Path $desktop ("Production Suite - " + $item[0] + ".url")
        Set-Content -Path $file -Value @("[InternetShortcut]", "URL=http://localhost:$port/$($item[1])/$q") -Encoding ASCII
    }
    try { $warn = @(Add-AdminShortcut $installDir) } catch { $warn = @("Couldn't add the Production Suite Admin shortcut: $($_.Exception.Message)") }
    try { Start-SuiteTray } catch { Write-SuiteLog "Couldn't start the tray icon: $($_.Exception.Message)" }
    $info = @{ version = $script:SuiteVersion; revision = $script:SuiteRevision; installed = (Get-Date).ToString("s"); python = $python; port = $port; folder = $installDir
               packagesHash = $script:PackagesHash } | ConvertTo-Json
    Set-Content -Path (Join-Path $installDir "install-info.json") -Value $info -Encoding ASCII
    return $warn
}

# Production Suite Admin and the tray icon run as whoever is signed in, so ordinary users may read (not change)
# the desktop folder they live in. The rest of the install folder stays administrators and the system account only.
function Grant-DesktopRead([string]$installDir) {
    $d = Join-Path $installDir "desktop"
    if (-not (Test-Path $d)) { return }
    try {
        $code = Invoke-Program "icacls.exe" @($d, "/grant", "*S-1-5-32-545:(OI)(CI)RX", "/T", "/C", "/Q")
        if ($code -ne 0) { Write-SuiteLog "Couldn't let users open Production Suite Admin (icacls code $code); only administrators will be able to." }
    } catch { Write-SuiteLog "Couldn't let users open Production Suite Admin ($($_.Exception.Message)); only administrators will be able to." }
}

# Starts the tray icon now for whoever is signed in. Opening the Startup shortcut through Explorer runs it as
# that user, not as the administrator running setup.
function Start-SuiteTray {
    $startup = [Environment]::GetFolderPath("CommonStartup")
    $file = $(if ($startup) { Join-Path $startup "Production Suite.lnk" } else { "" })
    if ($file -and (Test-Path $file)) {
        try { Start-Process explorer.exe -ArgumentList "`"$file`"" } catch { Write-SuiteLog "Couldn't start the tray icon: $($_.Exception.Message)" }
    }
}

# The "Production Suite Admin" shortcut (the server's settings window: connections, start / stop / restart),
# on the desktop every user of this server sees and in the Start menu. Made again on every install and
# update, so it always points at this install. Returns warnings (empty when both shortcuts are in place).
function Add-AdminShortcut([string]$installDir) {
    $warn = New-Object Collections.ArrayList
    $exe = Join-Path $installDir "desktop\SuiteAdmin.exe"
    if (-not (Test-Path $exe)) {
        [void]$warn.Add("Production Suite Admin is missing from $installDir\desktop, so its shortcut wasn't added. Extract the whole download again and re-run setup.")
        return @($warn)
    }
    Grant-DesktopRead $installDir
    $desk = [Environment]::GetFolderPath("CommonDesktopDirectory")
    if (-not $desk -and $env:PUBLIC) { $desk = Join-Path $env:PUBLIC "Desktop" }
    $menu = [Environment]::GetFolderPath("CommonPrograms")
    $places = @()
    if ($desk) { $places += ,@($desk, "the desktop") }
    if ($menu) { $places += ,@((Join-Path $menu "Production Suite"), "the Start menu") }
    $ico = Join-Path $installDir "desktop\suite.ico"
    foreach ($pl in $places) {
        $file = Join-Path $pl[0] "Production Suite Admin.lnk"
        try {
            if (-not (Test-Path $pl[0])) { New-Item -ItemType Directory -Path $pl[0] -Force | Out-Null }
            $shell = New-Object -ComObject WScript.Shell
            $lnk = $shell.CreateShortcut($file)
            $lnk.TargetPath = $exe
            $lnk.WorkingDirectory = $installDir
            $lnk.Description = "Production Suite server settings: connections, start, stop and restart"
            if (Test-Path $ico) { $lnk.IconLocation = "$ico,0" }
            $lnk.Save()
            try { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($shell) } catch { }
        } catch { Write-SuiteLog "Couldn't add the Production Suite Admin shortcut to $($pl[1]): $($_.Exception.Message)" }
        if (Test-Path $file) { Write-SuiteLog "Production Suite Admin shortcut: $file" }
        else { [void]$warn.Add("Couldn't put the Production Suite Admin shortcut on $($pl[1]). Open it from $exe instead.") }
    }
    # the tray icon, at every login for every user
    $startup = [Environment]::GetFolderPath("CommonStartup")
    if ($startup) {
        $file = Join-Path $startup "Production Suite.lnk"
        try {
            $shell = New-Object -ComObject WScript.Shell
            $lnk = $shell.CreateShortcut($file)
            $lnk.TargetPath = $exe; $lnk.Arguments = "--tray"; $lnk.WorkingDirectory = $installDir
            $lnk.Description = "Production Suite tray icon: shows whether the suite is running"
            if (Test-Path $ico) { $lnk.IconLocation = "$ico,0" }
            $lnk.Save()
            try { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($shell) } catch { }
        } catch { Write-SuiteLog "Couldn't add the tray icon to Startup: $($_.Exception.Message)" }
        if (-not (Test-Path $file)) { [void]$warn.Add("Couldn't add the Production Suite tray icon to Startup. Start it with `"$exe`" --tray.") }
    }
    return @($warn)
}
