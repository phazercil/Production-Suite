"""Build a signed update file (installed in Operator Kiosk > Admin > Updates, or the update folder).

    python tools/make_update.py --key production-suite-signing-key.txt
    python tools/make_update.py --key KEY --packages ..\\wheels --notes "Adds InfluxDB"

Raise REVISION in suite/version.py first (and VERSION for a release): a server only accepts a revision
newer than the one it has. Add --packages with the wheels folder when requirements-windows.txt changed;
otherwise the file is small and holds just the program files.

    python tools/make_update.py --new-key KEYFILE    makes a new signing key (then put its public key in
                                                     plant/update_key.py and ship that with Setup.exe)
"""
import argparse
import base64
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from plant import ed25519, updatefile  # noqa: E402


def load_secret(path):
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.startswith("SECRET="):
            return base64.b64decode(line[7:].strip())
    raise SystemExit(f"No SECRET= line in {path}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build a signed Production Suite update file.")
    ap.add_argument("--key", help="the signing key file")
    ap.add_argument("--packages", help="folder of .whl files to include (only when the packages changed)")
    ap.add_argument("--notes", default="", help="what's in this update, shown on Admin > Updates")
    ap.add_argument("--out", default=str(ROOT.parent), help="folder to write the update file to")
    ap.add_argument("--new-key", help="make a new signing key in this file and stop")
    a = ap.parse_args(argv)
    if a.new_key:
        sk = ed25519.new_secret()
        pk = base64.b64encode(ed25519.public_key(sk)).decode()
        Path(a.new_key).write_text("Production Suite update signing key. KEEP THIS PRIVATE.\n"
                                   f"Public key (built into the suite): {pk}\nSECRET={base64.b64encode(sk).decode()}\n", encoding="utf-8")
        print(f"New key in {a.new_key}. Public key: {pk}")
        return 0
    if not a.key:
        ap.error("--key is required")
    secret = load_secret(a.key)
    from plant.update_key import PUBLIC_KEYS
    pk = base64.b64encode(ed25519.public_key(secret)).decode()
    if pk not in PUBLIC_KEYS:
        raise SystemExit("That key isn't one the suite trusts (plant/update_key.py). Servers would refuse the file.")
    version, revision = updatefile.read_version(ROOT)
    out = Path(a.out) / f"ProductionSuite-Update-{version}-r{revision}{'-with-packages' if a.packages else ''}.zip"
    m = updatefile.build(ROOT, out, secret, a.packages, a.notes)
    print(f"{out}  v{version} rev {revision}, {len(m['files'])} files, {out.stat().st_size / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
