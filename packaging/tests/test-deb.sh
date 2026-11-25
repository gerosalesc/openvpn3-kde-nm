#!/usr/bin/env bash
# End-to-end verification of the .deb package inside a clean Debian or Ubuntu container.
set -euo pipefail

DEB_PATH="${1:?Usage: $0 <path-to.deb> <expected-version>}"
EXPECTED_VERSION="${2:?Usage: $0 <path-to.deb> <expected-version>}"

export DEBIAN_FRONTEND=noninteractive

# shellcheck source=/dev/null
. /etc/os-release
echo "=== Testing ${DEB_PATH} on ${PRETTY_NAME:-$ID $VERSION_ID} ==="

# Ensure Ubuntu's universe component is enabled in minimal container images
if [ "${ID:-}" = "ubuntu" ] && [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then
    if ! grep -q "universe" /etc/apt/sources.list.d/ubuntu.sources; then
        sed -i 's/^Components: \(.*\)$/Components: \1 universe/' /etc/apt/sources.list.d/ubuntu.sources
    fi
fi

apt-get update -qq

# On older LTS releases without native openvpn3-client (e.g., Ubuntu 24.04 Noble),
# configure OpenVPN's official APT repository so openvpn3-client resolves cleanly.
if ! apt-cache show openvpn3-client >/dev/null 2>&1 && ! apt-cache show openvpn3 >/dev/null 2>&1; then
    echo "Adding official OpenVPN 3 APT repository for ${VERSION_CODENAME}..."
    apt-get install -y -qq --no-install-recommends ca-certificates curl
    install -d -m 0755 /etc/apt/keyrings
    curl -fsSL https://packages.openvpn.net/packages-repo.gpg -o /etc/apt/keyrings/openvpn.asc
    chmod 0644 /etc/apt/keyrings/openvpn.asc
    echo "deb [signed-by=/etc/apt/keyrings/openvpn.asc] https://packages.openvpn.net/openvpn3/debian ${VERSION_CODENAME} main" \
        > /etc/apt/sources.list.d/openvpn3.list
    apt-get update -qq
fi

# Pre-install network-manager-openvpn so we can capture its exact original OpenVPN 2 hashes
apt-get install -y -qq --no-install-recommends network-manager-openvpn
ORIG_NAME_FILE="/usr/lib/NetworkManager/VPN/nm-openvpn-service.name"
ORIG_PROG_PATH="$(sed -n 's/^[[:space:]]*program[[:space:]]*=[[:space:]]*//p' "$ORIG_NAME_FILE" | head -n 1)"
ORIG_NAME_SHA="$(sha256sum "$ORIG_NAME_FILE" | awk '{print $1}')"
ORIG_PROG_SHA="$(sha256sum "$ORIG_PROG_PATH" | awk '{print $1}')"
echo "Captured original OpenVPN 2 plugin: ${ORIG_PROG_PATH}"

# Copy .deb into /tmp with world-readable perms so apt's _apt sandbox user can read it cleanly
LOCAL_DEB="/tmp/$(basename "$DEB_PATH")"
cp "$DEB_PATH" "$LOCAL_DEB"
chmod 0644 "$LOCAL_DEB"

echo "[1/6] Installing .deb package via apt-get..."
apt-get install -y -qq --no-install-recommends "$LOCAL_DEB"

echo "[2/6] Verifying CLI, Python module, and runtime imports..."
CLI_VER="$(openvpn3-kde-cli --version)"
echo "CLI output: ${CLI_VER}"
case "$CLI_VER" in
    *"${EXPECTED_VERSION}"*) ;;
    *)
        echo "ERROR: Expected version ${EXPECTED_VERSION}, got '${CLI_VER}'" >&2
        exit 1
        ;;
esac

python3 - <<'PYEOF'
import importlib.metadata
import dbus
import gi
import openvpn3
gi.require_version("NM", "1.0")
from gi.repository import NM
import openvpn3_kde_nm
from openvpn3_kde_nm.session_watcher import is_nm_openvpn3_plugin_installed

assert is_nm_openvpn3_plugin_installed(), "NM OpenVPN 3 plugin not detected as installed!"
print("Verified Python imports and metadata version:", importlib.metadata.version("openvpn3-kde-nm"))
PYEOF

echo "[3/6] Verifying NetworkManager hook, dpkg-divert state, and systemd global user unit..."
grep -qx "program=/usr/libexec/nm-openvpn3-service" "$ORIG_NAME_FILE"
test -f "${ORIG_NAME_FILE}.openvpn2"
test -L "$ORIG_PROG_PATH"
test "$(readlink -f "$ORIG_PROG_PATH")" = "/usr/libexec/nm-openvpn3-service"
test -f "${ORIG_PROG_PATH}.openvpn2"
test "$(dpkg-divert --listpackage "$ORIG_NAME_FILE")" = "openvpn3-kde-nm"
test "$(dpkg-divert --listpackage "$ORIG_PROG_PATH")" = "openvpn3-kde-nm"
test -L /etc/systemd/user/default.target.wants/openvpn3-kde-sync.service

echo "[4/6] Simulating upstream network-manager-openvpn upgrade (dpkg-divert + trigger survival)..."
apt-get install -y -qq --reinstall --no-install-recommends network-manager-openvpn
grep -qx "program=/usr/libexec/nm-openvpn3-service" "$ORIG_NAME_FILE"
test -L "$ORIG_PROG_PATH"
test "$(readlink -f "$ORIG_PROG_PATH")" = "/usr/libexec/nm-openvpn3-service"

echo "[5/6] Simulating package reinstall/upgrade of openvpn3-kde-nm..."
apt-get install -y -qq --reinstall --no-install-recommends "$LOCAL_DEB"
grep -qx "program=/usr/libexec/nm-openvpn3-service" "$ORIG_NAME_FILE"

echo "[6/6] Verifying clean removal and bit-for-bit OpenVPN 2 restoration..."
apt-get remove -y -qq openvpn3-kde-nm
test -z "$(dpkg-divert --listpackage "$ORIG_NAME_FILE")"
test -z "$(dpkg-divert --listpackage "$ORIG_PROG_PATH")"
test ! -L "$ORIG_PROG_PATH"
test "$(sha256sum "$ORIG_NAME_FILE" | awk '{print $1}')" = "$ORIG_NAME_SHA"
test "$(sha256sum "$ORIG_PROG_PATH" | awk '{print $1}')" = "$ORIG_PROG_SHA"
test ! -e /etc/systemd/user/default.target.wants/openvpn3-kde-sync.service

echo "=== PASS: All .deb verification checks succeeded on ${PRETTY_NAME:-$ID $VERSION_ID} ==="
