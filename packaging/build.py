"""Build the installer zip (and, with --key, the signed update file) from this repository.

    python packaging/build.py                       -> dist/ProductionSuite-Setup.zip
    python packaging/build.py --key KEYFILE         -> also dist/ProductionSuite-Update-<version>-r<revision>.zip

Before a release: raise REVISION in suite/version.py (and VERSION), and set the same numbers as
$script:SuiteVersion / $script:SuiteRevision in packaging/installer/common.ps1. The build stops if they differ.
Setup.exe and desktop/SuiteAdmin.exe are prebuilt from packaging/launcher (Go; see packaging/README.md).
"""
import argparse
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "packaging"
sys.path.insert(0, str(ROOT))
from plant import updatefile  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", help="update signing key file: also build the update file")
    ap.add_argument("--notes", default="", help="what's in this update (shown on Admin > Updates)")
    ap.add_argument("--packages", action="store_true", help="put the wheels in the update file (when requirements changed)")
    a = ap.parse_args()
    version, revision = updatefile.read_version(ROOT)
    ps = (PKG / "installer" / "common.ps1").read_text(encoding="utf-8")
    pv = re.search(r'\$script:SuiteVersion = "([^"]+)"', ps).group(1)
    pr = int(re.search(r"\$script:SuiteRevision = (\d+)", ps).group(1))
    if (pv, pr) != (version, revision):
        sys.exit(f"suite/version.py says v{version} rev {revision} but common.ps1 says v{pv} rev {pr}. Make them match.")
    dist = ROOT / "dist"
    stage = dist / "ProductionSuite-Setup"
    shutil.rmtree(stage, ignore_errors=True)
    (stage / "app").mkdir(parents=True)
    for rel in updatefile.app_files(ROOT):
        t = stage / "app" / rel
        t.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, t)
    shutil.copytree(PKG / "installer", stage / "installer")
    shutil.copytree(PKG / "wheels", stage / "wheels")
    for f in ("Setup.exe", "README-INSTALL.txt", "Install Production Suite.bat", "Uninstall Production Suite.bat"):
        shutil.copy2(PKG / f, stage / f)
    out = dist / "ProductionSuite-Setup.zip"
    out.unlink(missing_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(stage.rglob("*")):
            if p.is_file():
                z.write(p, Path("ProductionSuite-Setup") / p.relative_to(stage))
    print(f"{out}  v{version} rev {revision}, {out.stat().st_size / 1e6:.1f} MB")
    if a.key:
        cmd = [sys.executable, str(ROOT / "tools" / "make_update.py"), "--key", a.key, "--out", str(dist), "--notes", a.notes]
        if a.packages:
            cmd += ["--packages", str(PKG / "wheels")]
        subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()
