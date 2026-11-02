"""Update files (Admin > Updates): reading, checking and building them.

An update file is a zip holding
    manifest.json   product, version, revision, packages hash, and the SHA-256 of every file below
    manifest.sig    an Ed25519 signature of manifest.json (base64)
    app/...         the suite's program files
    wheels/...      the Python packages, only when they changed since the last release

Nothing in it is trusted until the signature checks out against a public key built into the suite and
every file matches the manifest. Standard library only: the updater uses this module too, while the
suite itself is stopped.
"""
import base64
import hashlib
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

try:
    from . import ed25519
except ImportError:  # run by the updater from updates/run, outside the package
    import ed25519

PRODUCT = "production-suite"
MAX_FILES = 5000
MAX_TOTAL = 400 * 1024 * 1024
# Kept in the install folder by updates: settings, records, logs, the Python environment and update files.
PRESERVE = {".venv", "logs", "staticfiles", "updates", "backups", "wheels", "installer", ".env", "install-info.json",
            "Uninstall Production Suite.bat", "media"}
PRESERVE_GLOBS = (".env.backup-*", "*.sqlite3", "*.sqlite3-journal")
SKIP_DIRS = {"__pycache__", "node_modules", ".git", ".pytest_cache", ".github"}
# Only in the source repository: used to build the installer and update files, not to run the suite.
SOURCE_ONLY = {"packaging", "dist"}
SKIP_FILES = ("*.pyc", "*.pyo", ".DS_Store", "Thumbs.db")


class UpdateError(Exception):
    pass


def preserved(name):
    return name in PRESERVE or any(PurePosixPath(name).match(g) for g in PRESERVE_GLOBS)


def app_files(root):
    """The program files under an install (or source) folder, as relative posix paths."""
    root = Path(root)
    out = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        parts = rel.parts
        if preserved(parts[0]) or parts[0] in SOURCE_ONLY or any(x in SKIP_DIRS for x in parts) or not p.is_file():
            continue
        if any(PurePosixPath(parts[-1]).match(g) for g in SKIP_FILES):
            continue
        out.append(rel.as_posix())
    return out


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def packages_hash(requirements_text, wheel_names):
    """Same value the installer stores in install-info.json (SHA-256 of requirements + sorted wheel names)."""
    sig = requirements_text + "|" + ",".join(sorted(wheel_names, key=str.lower))
    return hashlib.sha256(sig.encode("utf-8")).hexdigest().upper()


def read_version(app_root):
    """(version, revision) of the program files in a folder, from suite/version.py, without importing it."""
    try:
        text = (Path(app_root) / "suite" / "version.py").read_text(encoding="utf-8")
    except OSError:
        return "", 0
    v = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.M)
    r = re.search(r"^REVISION\s*=\s*(\d+)", text, re.M)
    return (v.group(1) if v else ""), (int(r.group(1)) if r else 0)


def _safe_name(name):
    p = PurePosixPath(name)
    if name.startswith("/") or "\\" in name or ".." in p.parts or ":" in name or not p.parts:
        raise UpdateError(f"The update file has an unsafe path in it ({name[:80]}).")
    return p


def verify(path, public_keys):
    """Check an update file. Returns its manifest, or raises UpdateError saying what is wrong."""
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        raise UpdateError("That isn't a Production Suite update file (not a zip).")
    with z:
        names = z.namelist()
        if "manifest.json" not in names or "manifest.sig" not in names:
            raise UpdateError("That isn't a Production Suite update file (no manifest). The full installer zip is installed with Setup.exe.")
        raw = z.read("manifest.json")
        try:
            sig = base64.b64decode(z.read("manifest.sig").strip(), validate=True)
        except Exception:
            raise UpdateError("The update file's signature is damaged.")
        if not any(ed25519.verify(base64.b64decode(k), raw, sig) for k in public_keys):
            raise UpdateError("The update file's signature doesn't match. Only update files built for this suite can be installed; "
                              "this one was changed after it was built, or signed with a different key.")
        try:
            m = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise UpdateError("The update file's manifest is damaged.")
        if m.get("product") != PRODUCT or not isinstance(m.get("files"), dict):
            raise UpdateError("That update file is for a different product.")
        files = m["files"]
        if len(files) > MAX_FILES:
            raise UpdateError("The update file has too many files in it.")
        listed = set(files)
        present = {n for n in names if not n.endswith("/") and n not in ("manifest.json", "manifest.sig")}
        for n in present | listed:
            p = _safe_name(n)
            if p.parts[0] not in ("app", "wheels"):
                raise UpdateError(f"The update file has an unexpected file in it ({n[:80]}).")
        if present != listed:
            extra = sorted(present - listed)[:3] or sorted(listed - present)[:3]
            raise UpdateError(f"The update file doesn't match its manifest ({', '.join(extra)}).")
        total = 0
        for info in z.infolist():
            if info.filename in files:
                total += info.file_size
                if total > MAX_TOTAL:
                    raise UpdateError("The update file is too large once unpacked.")
                h = hashlib.sha256()
                with z.open(info) as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                if h.hexdigest() != files[info.filename]:
                    raise UpdateError(f"{info.filename} in the update file was changed after it was built.")
        if "app/suite/version.py" not in files or "app/manage.py" not in files:
            raise UpdateError("The update file is missing the suite's program files.")
        m["hasPackages"] = any(n.startswith("wheels/") and n.endswith(".whl") for n in files)
        return m


def extract(path, dest, prefix):
    """Extract the files under prefix/ ("app" or "wheels") into dest. The file must have been verified."""
    dest = Path(dest)
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            if info.filename.endswith("/") or not info.filename.startswith(prefix + "/"):
                continue
            rel = PurePosixPath(info.filename).relative_to(prefix)
            target = dest.joinpath(*rel.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)


def build(app_root, out_path, secret, wheels_dir=None, notes=""):
    """Make a signed update file from a source folder. Returns the manifest."""
    app_root = Path(app_root)
    version, revision = read_version(app_root)
    if not version or not revision:
        raise UpdateError("suite/version.py needs VERSION and REVISION.")
    req = (app_root / "requirements-windows.txt").read_bytes().decode("utf-8") if (app_root / "requirements-windows.txt").exists() else ""
    wheel_names = sorted(p.name for p in Path(wheels_dir).glob("*.whl")) if wheels_dir else []
    entries = [(f"app/{rel}", app_root / rel) for rel in app_files(app_root)]
    entries += [(f"wheels/{n}", Path(wheels_dir) / n) for n in wheel_names]
    manifest = {"product": PRODUCT, "version": version, "revision": revision,
                "built": datetime.now(timezone.utc).replace(microsecond=0).isoformat(), "notes": str(notes)[:2000],
                "packagesHash": packages_hash(req, wheel_names) if wheel_names else "",
                "requirements": hashlib.sha256(req.encode()).hexdigest(),
                "files": {name: sha256_file(src) for name, src in entries}}
    raw = json.dumps(manifest, indent=1, sort_keys=True).encode("utf-8")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("manifest.json", raw)
        z.writestr("manifest.sig", base64.b64encode(ed25519.sign(secret, raw)).decode() + "\n")
        for name, src in entries:
            z.write(src, name)
    return manifest
