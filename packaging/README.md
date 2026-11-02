# Packaging

How the Windows installer and update files are built from this repository.

- `installer/` — the setup window (`setup-gui.ps1`), text-mode installer, shared steps (`common.ps1`), uninstaller.
- `wheels/` — the Python packages the server installs offline (pure-Python wheels).
- `launcher/setup`, `launcher/admin` — Go sources of `Setup.exe` and `desktop/SuiteAdmin.exe`
  (standard library only; the `.syso` holds the icon and the "run as administrator" manifest). Build with
  `GOOS=windows GOARCH=amd64 go build -ldflags "-s -w -H windowsgui"` and copy the result to
  `packaging/Setup.exe` or `desktop/SuiteAdmin.exe`.
- `build.py` — makes `dist/ProductionSuite-Setup.zip`, and with `--key` the signed update file.

Release checklist: raise `REVISION` (and `VERSION`) in `suite/version.py` and the same numbers in
`installer/common.ps1`; run `python manage.py test tests`; run `python packaging/build.py --key <key file>`.
The signing key is never committed (see `.gitignore`).

## Code signing

Every program and PowerShell script that ships is code-signed: `Setup.exe`, `desktop/SuiteAdmin.exe`, the installer
scripts, Production Suite Admin and the tray icon (Authenticode, SHA-256, time-stamped, so signatures stay valid after
the certificate expires). Security software such as Elastic can then trust them by publisher. Python and `.cmd` files
can't carry a signature; they run under the signed programs, or are trusted by folder (`C:\ProductionSuite`).

Use a code-signing certificate from your company's certificate authority (IT) or a commercial one. A self-signed
certificate signs, but nothing trusts it unless IT deploys it to the servers.

**On GitHub (recommended):** add three repository secrets (Settings › Secrets and variables › Actions):
`CODESIGN_PFX_BASE64` (the .pfx as base64: `[Convert]::ToBase64String([IO.File]::ReadAllBytes("codesign.pfx"))`),
`CODESIGN_PFX_PASSWORD`, and `UPDATE_SIGNING_KEY` (the whole update key file). Then Actions › Release › Run workflow,
or push a tag such as `v1.3.5`: a Windows machine builds, signs, and attaches `ProductionSuite-Setup.zip` and the update
file to the run (and to a GitHub release, for tags). Releases are never built unsigned.

**On a Windows PC:** `python packaging/build.py --sign --key <update key file>`, with the certificate in your
certificate store (`--thumbprint 1A2B…` to pick one) or as a file (`--pfx codesign.pfx`, password in
`CODESIGN_PFX_PASSWORD`). `packaging/sign.ps1 -Path <folder>` signs any built folder by hand.

The "Windows" check on every pull request signs a full build with a throwaway certificate and checks every file, and
registers the suite's scheduled tasks on a real Windows machine with the installer's own code.
