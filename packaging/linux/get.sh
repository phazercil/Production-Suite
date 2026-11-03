#!/usr/bin/env bash
# Install or update Production Suite on Linux straight from GitHub (Fedora, Rocky / Alma / CentOS Stream / RHEL 9).
#
# It downloads ProductionSuite-Linux.tar.gz from a release of the repository, checks it against the
# checksum GitHub keeps for it, and runs its install.sh. The repository is private, so it needs a GitHub
# token that can read it (a fine-grained token with "Contents: Read-only" on this one repository is
# enough). The token is used for the download only: it is never saved and never given to the suite.
#
#   sudo bash get.sh                            the latest release
#   sudo bash get.sh --version v1.7.0           a given release
#   sudo bash get.sh --with-mariadb ...         anything else is passed on to install.sh
#   GITHUB_TOKEN=... sudo -E bash get.sh        token from the environment instead of being asked
#
# Only this download talks to the internet (github.com); the suite itself stays on the plant network.
set -euo pipefail

REPO="${PS_REPO:-phazercil/Production-Suite}"
API="${PS_GITHUB_API:-https://api.github.com}"   # changed only by the automatic checks
ASSET=ProductionSuite-Linux.tar.gz
WORK=/var/lib/production-suite-installer
TAG=latest
args=()

fail() { printf '\nStopped: %s\n' "$*" >&2; exit 1; }
while [ $# -gt 0 ]; do
    case "$1" in
        --version) [ -n "${2:-}" ] || fail "--version needs a release name, like v1.7.0."; TAG="$2"; shift 2 ;;
        -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
        *) args+=("$1"); shift ;;
    esac
done
[ "$(id -u)" -eq 0 ] || fail "Run it as root: sudo bash $0"
need=(); for c in python3 tar; do command -v "$c" >/dev/null || need+=("$c"); done
command -v curl >/dev/null || need+=(curl)
[ ${#need[@]} -eq 0 ] || dnf -y -q install "${need[@]}" || fail "Couldn't install ${need[*]} with dnf."
[[ "$TAG" =~ ^[A-Za-z0-9._-]+$ ]] || fail "That release name doesn't look right: $TAG"

token="${GITHUB_TOKEN:-${GH_TOKEN:-}}"
if [ -z "$token" ]; then
    [ -r /dev/tty ] || fail "Set GITHUB_TOKEN (a token that can read $REPO)."
    printf 'GitHub token that can read %s (typing is hidden): ' "$REPO" >/dev/tty
    IFS= read -rs token </dev/tty; echo >/dev/tty
fi
[ -n "$token" ] || fail "A GitHub token is needed: the repository is private."

tmp="$(mktemp -d)"; chmod 700 "$tmp"
trap 'rm -rf "$tmp"' EXIT
# the token goes to curl in a private file, so it never shows in the list of running programs
printf 'Authorization: Bearer %s\n' "$token" > "$tmp/auth"; chmod 600 "$tmp/auth"; unset token
gh() { curl -fsSL --proto '=https,http' --retry 3 -H @"$tmp/auth" -H 'X-GitHub-Api-Version: 2022-11-28' "$@"; }

if [ "$TAG" = latest ]; then url="$API/repos/$REPO/releases/latest"; else url="$API/repos/$REPO/releases/tags/$TAG"; fi
echo "Looking up the $TAG release of $REPO…"
code=$(gh -o "$tmp/release.json" -w '%{http_code}' -H 'Accept: application/vnd.github+json' "$url" 2>/dev/null) || code=${code:-000}
case "$code" in
    200) ;;
    401) fail "GitHub refused the token (expired, or mistyped)." ;;
    404) fail "No $TAG release found, or the token can't read $REPO. Releases are made by pushing a tag like v1.7.0 (see README-LINUX.txt)." ;;
    000) fail "Couldn't reach GitHub. This server needs to reach api.github.com and github.com over HTTPS while installing." ;;
    *) fail "GitHub answered $code." ;;
esac

read -r tag id digest < <(python3 - "$tmp/release.json" "$ASSET" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
a = next((x for x in r.get("assets", []) if x.get("name") == sys.argv[2]), None)
if not a:
    sys.exit(f"The release {r.get('tag_name')} has no {sys.argv[2]}.")
print(r["tag_name"], a["id"], (a.get("digest") or "-").removeprefix("sha256:"))
PY
) || fail "The release doesn't have the Linux package."
[[ "$tag" =~ ^[A-Za-z0-9._-]+$ ]] && [[ "$id" =~ ^[0-9]+$ ]] || fail "GitHub's answer didn't look right."

echo "Downloading $ASSET from release $tag…"
gh -H 'Accept: application/octet-stream' -o "$tmp/$ASSET" "$API/repos/$REPO/releases/assets/$id" || fail "The download failed."
if [ "$digest" = - ]; then   # older releases: the checksum file published next to the package
    sid=$(python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); print(next((a["id"] for a in r["assets"] if a["name"]==sys.argv[2]+".sha256"), ""))' "$tmp/release.json" "$ASSET")
    [ -n "$sid" ] || fail "The release has no checksum for $ASSET, so the download can't be checked."
    digest=$(gh -H 'Accept: application/octet-stream' "$API/repos/$REPO/releases/assets/$sid" | awk '{print $1}')
fi
have=$(sha256sum "$tmp/$ASSET" | awk '{print $1}')
[ "$have" = "$digest" ] || fail "The download doesn't match its checksum (expected $digest, got $have). Nothing was installed."
echo "Checked: sha256 $have"

install -d -m 700 "$WORK"
rm -rf "$WORK/$tag"; install -d -m 700 "$WORK/$tag"
tar xzf "$tmp/$ASSET" -C "$WORK/$tag" --no-same-owner
[ -x "$WORK/$tag/ProductionSuite-Linux/install.sh" ] || fail "The package has no install.sh."
# keep the last 3 downloads, for going back by hand
ls -1dt "$WORK"/*/ 2>/dev/null | tail -n +4 | xargs -r rm -rf
cd "$WORK/$tag/ProductionSuite-Linux"
echo
exec ./install.sh "${args[@]}"
