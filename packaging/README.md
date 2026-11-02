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
