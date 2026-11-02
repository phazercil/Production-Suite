<#
  Signs every program and PowerShell script in a built Production Suite folder with a code-signing
  certificate (Authenticode, SHA-256, time-stamped so signatures stay valid after the certificate expires).
  Run on Windows. Used by packaging/build.py --sign and the Release workflow; can also be run by hand:

    powershell -ExecutionPolicy Bypass -File packaging\sign.ps1 -Path dist\ProductionSuite-Setup -Thumbprint 1A2B...
    powershell -ExecutionPolicy Bypass -File packaging\sign.ps1 -Path dist\ProductionSuite-Setup -PfxFile codesign.pfx
        (the .pfx password comes from the CODESIGN_PFX_PASSWORD environment variable)

  Signed: *.exe, *.dll, *.ps1, *.psm1, *.psd1 (Setup.exe, desktop\SuiteAdmin.exe, the installer, Production Suite
  Admin and the tray icon). Not signable, so not signed: Python files and .cmd files; security software trusts
  those through the signed programs that start them, or by folder.
  Exit code 0 when every file is signed; 1 otherwise (the build stops).
#>
param(
    [Parameter(Mandatory = $true)][string]$Path,
    [string]$Thumbprint = "",
    [string]$PfxFile = "",
    [string]$TimestampServer = "http://timestamp.digicert.com"
)
$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------- the certificate
$cert = $null
if ($PfxFile) {
    $pw = $env:CODESIGN_PFX_PASSWORD
    $flags = [Security.Cryptography.X509Certificates.X509KeyStorageFlags]::Exportable
    $cert = New-Object Security.Cryptography.X509Certificates.X509Certificate2((Resolve-Path $PfxFile).Path, $pw, $flags)
} elseif ($Thumbprint) {
    $t = $Thumbprint -replace '[^0-9A-Fa-f]', ''
    $cert = @(Get-ChildItem Cert:\CurrentUser\My, Cert:\LocalMachine\My -CodeSigningCert -ErrorAction SilentlyContinue | Where-Object { $_.Thumbprint -eq $t }) | Select-Object -First 1
    if (-not $cert) { throw "No code-signing certificate with thumbprint $t in the current user's or this computer's certificate store." }
} else {
    $all = @(Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert -ErrorAction SilentlyContinue | Where-Object { $_.NotAfter -gt (Get-Date) })
    if ($all.Count -ne 1) { throw "Pass -Thumbprint or -PfxFile ($($all.Count) code-signing certificates found in the current user's store)." }
    $cert = $all[0]
}
if (-not $cert.HasPrivateKey) { throw "The certificate for $($cert.Subject) has no private key here, so it can't sign." }
$eku = @($cert.EnhancedKeyUsageList | ForEach-Object { $_.ObjectId })
if ($eku.Count -and $eku -notcontains "1.3.6.1.5.5.7.3.3") { throw "$($cert.Subject) is not a code-signing certificate." }
if ($cert.NotAfter -lt (Get-Date)) { throw "The certificate for $($cert.Subject) expired on $($cert.NotAfter)." }
Write-Host "Signing with: $($cert.Subject)  (thumbprint $($cert.Thumbprint), valid until $($cert.NotAfter.ToString('yyyy-MM-dd')))"

# ---------------------------------------------------------------- the files
$files = @(Get-ChildItem -Path $Path -Recurse -File -Include *.exe, *.dll, *.ps1, *.psm1, *.psd1 |
    Where-Object { $_.FullName -notmatch '\\(wheels|\.venv|__pycache__)\\' })
if ($files.Count -eq 0) { throw "Nothing to sign in $Path." }
$bad = 0
foreach ($f in $files) {
    $ok = $false
    for ($try = 1; $try -le 3 -and -not $ok; $try++) {   # time-stamp servers are sometimes busy
        try {
            $r = Set-AuthenticodeSignature -FilePath $f.FullName -Certificate $cert -HashAlgorithm SHA256 -TimestampServer $TimestampServer -IncludeChain NotRoot
            $ok = $r.Status -eq "Valid" -or ($r.SignerCertificate -and $r.Status -eq "UnknownError")
        } catch { Write-Host "  retry $try for $($f.Name): $($_.Exception.Message)"; Start-Sleep -Seconds (3 * $try) }
    }
    $s = Get-AuthenticodeSignature -FilePath $f.FullName
    $rel = $f.FullName.Substring((Resolve-Path $Path).Path.Length).TrimStart('\')
    if ($s.SignerCertificate -and $s.SignerCertificate.Thumbprint -eq $cert.Thumbprint -and $s.Status -in @("Valid", "UnknownError")) {
        $note = $(if ($s.Status -eq "Valid") { "" } else { "  (signed; this computer doesn't trust the certificate's issuer)" })
        Write-Host ("  signed  {0}{1}" -f $rel, $note)
    } else {
        Write-Host ("  FAILED  {0}: {1} {2}" -f $rel, $s.Status, $s.StatusMessage)
        $bad++
    }
}
if ($bad) { Write-Host "$bad of $($files.Count) files were not signed."; exit 1 }
Write-Host "All $($files.Count) files signed."
exit 0
