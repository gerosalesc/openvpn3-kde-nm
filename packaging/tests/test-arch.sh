#!/usr/bin/env bash
# End-to-end verification of the Arch Linux .pkg.tar.zst inside a clean archlinux:latest container.
set -euo pipefail

PKG_PATH="${1:?Usage: $0 <path-to.pkg.tar.zst> <expected-version>}"
EXPECTED_VERSION="${2:?Usage: $0 <path-to.pkg.tar.zst> <expected-version>}"

echo "=== Testing ${PKG_PATH} on Arch Linux ==="

pacman -Sy --noconfirm --needed \
    python \
    python-gobject \
    python-dbus \
    libnm \
    networkmanager \
    networkmanager-openvpn \
    openssl \
    xdg-utils \
    iproute2 \
    systemd

ORIG_NAME_FILE="/usr/lib/NetworkManager/VPN/nm-openvpn-service.name"
ORIG_PROG_PATH="$(sed -n 's/^[[:space:]]*program[[:space:]]*=[[:space:]]*//p' "$ORIG_NAME_FILE" | head -n 1)"
ORIG_NAME_SHA="$(sha256sum "$ORIG_NAME_FILE" | awk '{print $1}')"
ORIG_PROG_SHA="$(sha256sum "$ORIG_PROG_PATH" | awk '{print $1}')"
echo "Captured original Arch OpenVPN 2 plugin: ${ORIG_PROG_PATH}"

echo "[1/5] Installing .pkg.tar.zst via pacman -U (--assume-installed openvpn3 for AUR dep)..."
pacman -U --noconfirm --assume-installed openvpn3 "$PKG_PATH"

echo "[2/5] Verifying installed executables, Python package, and GI/D-Bus imports..."
test -x /usr/bin/openvpn3-kde-cli
test -x /usr/bin/openvpn3-kde-sync
test -x /usr/lib/nm-openvpn3-service
test -f /usr/share/libalpm/hooks/90-openvpn3-kde-nm.hook
test -f /usr/share/licenses/openvpn3-kde-nm/LICENSE

python - "$EXPECTED_VERSION" <<'PYEOF'
import importlib.metadata
import sys
import dbus
import gi
gi.require_version("NM", "1.0")
from gi.repository import NM
import openvpn3_kde_nm

expected = sys.argv[1]
actual = importlib.metadata.version("openvpn3-kde-nm")
assert actual == expected or openvpn3_kde_nm.__version__ == expected, f"Expected {expected}, got {actual}"
print("Verified Arch Python package version:", actual)
PYEOF

echo "[3/5] Verifying NetworkManager hook, backups, and global systemd user unit..."
grep -qx "program=/usr/lib/nm-openvpn3-service" "$ORIG_NAME_FILE"
test -L "$ORIG_PROG_PATH"
test "$(readlink -f "$ORIG_PROG_PATH")" = "/usr/lib/nm-openvpn3-service"
test -f /var/lib/openvpn3-kde-nm/backups/nm-openvpn-service.name.openvpn2
test -f /var/lib/openvpn3-kde-nm/backups/nm-openvpn-service.openvpn2
test -L /etc/systemd/user/default.target.wants/openvpn3-kde-sync.service

echo "[4/5] Simulating upstream networkmanager-openvpn upgrade (pacman ALPM hook survival)..."
pacman -S --noconfirm networkmanager-openvpn
grep -qx "program=/usr/lib/nm-openvpn3-service" "$ORIG_NAME_FILE"
test -L "$ORIG_PROG_PATH"
test "$(readlink -f "$ORIG_PROG_PATH")" = "/usr/lib/nm-openvpn3-service"

echo "[5/5] Verifying clean pacman -R removal and bit-for-bit OpenVPN 2 restoration..."
pacman -R --noconfirm openvpn3-kde-nm
test ! -L "$ORIG_PROG_PATH"
test "$(sha256sum "$ORIG_NAME_FILE" | awk '{print $1}')" = "$ORIG_NAME_SHA"
test "$(sha256sum "$ORIG_PROG_PATH" | awk '{print $1}')" = "$ORIG_PROG_SHA"
test ! -e /etc/systemd/user/default.target.wants/openvpn3-kde-sync.service
test ! -d /var/lib/openvpn3-kde-nm

echo "=== PASS: All Arch Linux package verification checks succeeded ==="
