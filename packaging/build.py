"""Build the installer zip (and, with --key, the signed update file) from this repository.

    python packaging/build.py                       -> dist/ProductionSuite-Setup.zip (Windows) and
                                                       dist/ProductionSuite-Linux.tar.gz (Rocky / Alma / CentOS Stream / RHEL 9, Fedora)
    python packaging/build.py --key KEYFILE         -> also dist/ProductionSuite-Update-<version>-r<revision>.zip
    python packaging/build.py --sign ...            -> code-signs every .exe and PowerShell script first (Windows only):
        --sign                                         the one code-signing certificate in your certificate store
        --sign --thumbprint 1A2B...                    a certificate in the store, by thumbprint
        --sign --pfx codesign.pfx                      a .pfx file (password in the CODESIGN_PFX_PASSWORD environment variable)

The update file is made from the same (signed) files as the installer.

Before a release: raise REVISION in suite/version.py (and VERSION), and set the same numbers as
$script:SuiteVersion / $script:SuiteRevision in packaging/installer/common.ps1. The build stops if they differ.
Setup.exe and desktop/SuiteAdmin.exe are prebuilt from packaging/launcher (Go; see packaging/README.md).
"""
import argparse
import os
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
    ap.add_argument("--sign", action="store_true", help="code-sign the .exe and PowerShell files (Windows, needs a code-signing certificate)")
    ap.add_argument("--thumbprint", default="", help="with --sign: the certificate's thumbprint in the Windows certificate store")
    ap.add_argument("--pfx", default="", help="with --sign: a .pfx file; its password in CODESIGN_PFX_PASSWORD")
    a = ap.parse_args()
    if a.sign and os.name != "nt":
        sys.exit("Code signing runs on Windows (Set-AuthenticodeSignature). Run the build there, or use the Release workflow on GitHub.")
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
    # compiled wheels: Windows ones in the Windows package, Linux ones in the Linux package
    shutil.copytree(PKG / "wheels", stage / "wheels", ignore=lambda d, names: [n for n in names if "manylinux" in n])
    for f in ("Setup.exe", "README-INSTALL.txt", "Install Production Suite.bat", "Uninstall Production Suite.bat"):
        shutil.copy2(PKG / f, stage / f)
    if a.sign:
        cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(PKG / "sign.ps1"), "-Path", str(stage)]
        if a.thumbprint:
            cmd += ["-Thumbprint", a.thumbprint]
        if a.pfx:
            cmd += ["-PfxFile", a.pfx]
        if subprocess.run(cmd).returncode != 0:
            sys.exit("Code signing failed (see above); nothing was zipped.")
    else:
        print("Not code-signed (add --sign on Windows with the company code-signing certificate).")
    out = dist / "ProductionSuite-Setup.zip"
    out.unlink(missing_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(stage.rglob("*")):
            if p.is_file():
                z.write(p, Path("ProductionSuite-Setup") / p.relative_to(stage))
    print(f"{out}  v{version} rev {revision}, {out.stat().st_size / 1e6:.1f} MB")
    # Linux (EL9): the same program files and packages, with install.sh, systemd units and the Apache site
    import tarfile
    lin = dist / "ProductionSuite-Linux.tar.gz"
    lin.unlink(missing_ok=True)

    def mode(ti):
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = "root"
        if ti.isdir():
            ti.mode = 0o755
        elif ti.name.endswith(".sh"):
            ti.mode = 0o755
        else:
            ti.mode = 0o644
        return ti
    with tarfile.open(lin, "w:gz") as t:
        top = "ProductionSuite-Linux"
        t.add(stage / "app", f"{top}/app", filter=mode)
        for w in sorted((PKG / "wheels").glob("*.whl")):
            if "win_amd64" not in w.name:
                t.add(w, f"{top}/wheels/{w.name}", filter=mode)
        for f in ("install.sh", "uninstall.sh", "get.sh", "README-LINUX.txt"):
            t.add(PKG / "linux" / f, f"{top}/{f}", filter=mode)
        t.add(PKG / "linux" / "templates", f"{top}/templates", filter=mode)
    print(f"{lin}  v{version} rev {revision}, {lin.stat().st_size / 1e6:.1f} MB")
    if a.key:
        import base64
        from plant import ed25519
        from plant.update_key import PUBLIC_KEYS
        secret = next((base64.b64decode(x[7:].strip()) for x in Path(a.key).read_text(encoding="utf-8").splitlines()
                       if x.startswith("SECRET=")), None)
        if not secret:
            sys.exit(f"No SECRET= line in {a.key}")
        if base64.b64encode(ed25519.public_key(secret)).decode() not in PUBLIC_KEYS:
            sys.exit("That update key isn't one the suite trusts (plant/update_key.py). Servers would refuse the file.")
        upd = dist / f"ProductionSuite-Update-{version}-r{revision}{'-with-packages' if a.packages else ''}.zip"
        m = updatefile.build(stage / "app", upd, secret, (PKG / "wheels") if a.packages else None, a.notes)
        print(f"{upd}  v{version} rev {revision}, {len(m['files'])} files, {upd.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
