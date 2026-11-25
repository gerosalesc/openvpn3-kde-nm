#!/usr/bin/env bash
# Local (non-root, no-Docker) verification suite for packaging scripts, nm-hook.sh, and .deb build.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

echo "=== [1/4] Shell syntax checks ==="
bash -n "${REPO_ROOT}/packaging/common/nm-hook.sh"
bash -n "${REPO_ROOT}/packaging/build-deb.sh"
bash -n "${REPO_ROOT}/packaging/build-arch.sh"
bash -n "${REPO_ROOT}/packaging/tests/test-deb.sh"
bash -n "${REPO_ROOT}/packaging/tests/test-arch.sh"
sh -n "${REPO_ROOT}/packaging/debian/postinst"
sh -n "${REPO_ROOT}/packaging/debian/prerm"
sh -n "${REPO_ROOT}/packaging/debian/postrm"
bash -n "${REPO_ROOT}/packaging/arch/openvpn3-kde-nm.install"
echo "All shell scripts passed syntax check."

echo "=== [2/4] Testing nm-hook.sh backup-mode lifecycle (install -> upgrade -> legacy migration -> restore) ==="
MOCK_ROOT="${WORK_DIR}/mock-arch"
mkdir -p \
    "${MOCK_ROOT}/usr/lib/NetworkManager/VPN" \
    "${MOCK_ROOT}/etc/NetworkManager/VPN" \
    "${MOCK_ROOT}/usr/lib" \
    "${MOCK_ROOT}/var/lib/openvpn3-kde-nm" \
    "${MOCK_ROOT}/var/backups"

TARGET_SVC="${MOCK_ROOT}/usr/lib/nm-openvpn3-service"
ORIG_SVC="${MOCK_ROOT}/usr/lib/nm-openvpn-service"
NM_NAME="${MOCK_ROOT}/usr/lib/NetworkManager/VPN/nm-openvpn-service.name"
ETC_NAME="${MOCK_ROOT}/etc/NetworkManager/VPN/nm-openvpn-service.name"

printf '#!/usr/bin/python3\n# openvpn3_kde_nm service\n' > "$TARGET_SVC"
chmod 0755 "$TARGET_SVC"

printf 'ORIGINAL_OPENVPN2_BINARY_V1\n' > "$ORIG_SVC"
chmod 0755 "$ORIG_SVC"

cat > "$NM_NAME" <<EOF
[VPN Connection]
name=openvpn
service=org.freedesktop.NetworkManager.openvpn
program=${ORIG_SVC}
supports-multiple-connections=true

[libnm]
plugin=/usr/lib/NetworkManager/libnm-vpn-plugin-openvpn.so

[GNOME]
auth-dialog=/usr/lib/nm-openvpn-auth-dialog
EOF

# Also plant a stale /etc/NetworkManager/VPN override referencing nm-openvpn3-service
printf 'program=/usr/local/libexec/nm-openvpn3-service\n' > "$ETC_NAME"

ORIG_NAME_V1_SHA="$(sha256sum "$NM_NAME" | awk '{print $1}')"
ORIG_SVC_V1_SHA="$(sha256sum "$ORIG_SVC" | awk '{print $1}')"

run_hook() {
    OPENVPN3_KDE_NM_HOOK_CONF="/nonexistent" \
    OPENVPN3_KDE_NM_INOTIFY_SLEEP="0" \
    HOOK_MODE="backup" \
    TARGET_SERVICE="$TARGET_SVC" \
    DEFAULT_ORIG_PROG="$ORIG_SVC" \
    NM_VPN_DIR="${MOCK_ROOT}/usr/lib/NetworkManager/VPN" \
    ETC_NM_VPN_DIR="${MOCK_ROOT}/etc/NetworkManager/VPN" \
    LEGACY_BACKUP_DIR="${MOCK_ROOT}/var/backups" \
    STATE_DIR="${MOCK_ROOT}/var/lib/openvpn3-kde-nm" \
    "${REPO_ROOT}/packaging/common/nm-hook.sh" "$1"
}

# Apply #1
run_hook apply
grep -qx "program=${TARGET_SVC}" "$NM_NAME"
grep -qx "auth-dialog=/usr/lib/nm-openvpn-auth-dialog" "$NM_NAME"
test -L "$ORIG_SVC"
test "$(readlink "$ORIG_SVC")" = "$TARGET_SVC"
test ! -e "$ETC_NAME"

# Idempotent Apply #2 (must preserve V1 backup!)
run_hook apply
test "$(sha256sum "${MOCK_ROOT}/var/lib/openvpn3-kde-nm/backups/nm-openvpn-service.openvpn2" | awk '{print $1}')" = "$ORIG_SVC_V1_SHA"

# Simulate upstream networkmanager-openvpn upgrade to V2 (pacman overwrites files, ALPM hook calls apply)
rm -f "$ORIG_SVC"
printf 'ORIGINAL_OPENVPN2_BINARY_V2\n' > "$ORIG_SVC"
chmod 0755 "$ORIG_SVC"
cat > "$NM_NAME" <<EOF
[VPN Connection]
name=openvpn
service=org.freedesktop.NetworkManager.openvpn
program=${ORIG_SVC}
supports-multiple-connections=true
supports-safe-private-file-access=true

[libnm]
plugin=/usr/lib/NetworkManager/libnm-vpn-plugin-openvpn.so

[GNOME]
auth-dialog=/usr/lib/nm-openvpn-auth-dialog
EOF
ORIG_NAME_V2_SHA="$(sha256sum "$NM_NAME" | awk '{print $1}')"
ORIG_SVC_V2_SHA="$(sha256sum "$ORIG_SVC" | awk '{print $1}')"

run_hook apply
grep -qx "program=${TARGET_SVC}" "$NM_NAME"
grep -qx "supports-safe-private-file-access=true" "$NM_NAME"
test -L "$ORIG_SVC"

# Restore must bring back V2 bit-for-bit!
run_hook restore
test ! -L "$ORIG_SVC"
test "$(sha256sum "$NM_NAME" | awk '{print $1}')" = "$ORIG_NAME_V2_SHA"
test "$(sha256sum "$ORIG_SVC" | awk '{print $1}')" = "$ORIG_SVC_V2_SHA"
test ! -d "${MOCK_ROOT}/var/lib/openvpn3-kde-nm"
echo "Backup-mode lifecycle passed (including upstream upgrade & bit-for-bit V2 restoration)."

echo "=== [3/4] Testing legacy ./install.sh backup migration ==="
cp "$NM_NAME" "${MOCK_ROOT}/var/backups/nm-openvpn-service.name.openvpn2.bak"
cp "$ORIG_SVC" "${MOCK_ROOT}/var/backups/nm-openvpn-service.openvpn2.bak"
printf 'program=/usr/local/libexec/nm-openvpn3-service\n' > "$NM_NAME"
printf '#!/usr/bin/python3\nfrom openvpn3_kde_nm.nm_vpn_service import main\n' > "$ORIG_SVC"

run_hook apply
grep -qx "program=${TARGET_SVC}" "$NM_NAME"
grep -qx "supports-safe-private-file-access=true" "$NM_NAME"
run_hook restore
test "$(sha256sum "$NM_NAME" | awk '{print $1}')" = "$ORIG_NAME_V2_SHA"
test "$(sha256sum "$ORIG_SVC" | awk '{print $1}')" = "$ORIG_SVC_V2_SHA"
echo "Legacy ./install.sh migration passed."

echo "=== [4/4] Building and inspecting .deb package locally ==="
DEB_OUT="${WORK_DIR}/deb-out"
"${REPO_ROOT}/packaging/build-deb.sh" --output-dir "$DEB_OUT"
DEB_FILE="$(find "$DEB_OUT" -name '*.deb' | head -n 1)"
test -f "$DEB_FILE"

echo "--- dpkg-deb -I ---"
dpkg-deb -I "$DEB_FILE"
echo "--- dpkg-deb -c ---"
dpkg-deb -c "$DEB_FILE"

# Assert key required files exist inside the .deb
CONTENTS="$(dpkg-deb -c "$DEB_FILE")"
for required in \
    "./usr/bin/openvpn3-kde-cli" \
    "./usr/bin/openvpn3-kde-sync" \
    "./usr/libexec/nm-openvpn3-service" \
    "./usr/lib/python3/dist-packages/openvpn3_kde_nm/__init__.py" \
    "./usr/lib/python3/dist-packages/openvpn3_kde_nm/cli.py" \
    "./usr/lib/systemd/user/openvpn3-kde-sync.service" \
    "./usr/share/openvpn3-kde-nm/nm-hook.sh" \
    "./usr/share/openvpn3-kde-nm/hook.conf" \
    "./usr/share/doc/openvpn3-kde-nm/copyright" \
    "./usr/share/doc/openvpn3-kde-nm/README.md.gz" \
    "./usr/share/doc/openvpn3-kde-nm/changelog.Debian.gz"; do
    echo "$CONTENTS" | grep -qF "$required" || {
        echo "ERROR: Missing expected file in .deb: ${required}" >&2
        exit 1
    }
done

# Ensure all directories inside the .deb have canonical 0755 permissions (not 0775)
if echo "$CONTENTS" | grep -q '^drwxrwxr-x'; then
    echo "ERROR: Found group-writable (0775) directory in .deb archive!" >&2
    exit 1
fi

# Ensure conflicting upstream NM files are NOT shipped directly inside data.tar
if echo "$CONTENTS" | grep -qF "nm-openvpn-service.name"; then
    echo "ERROR: .deb must not ship nm-openvpn-service.name directly (would conflict with network-manager-openvpn)" >&2
    exit 1
fi

echo "=== PASS: All local packaging checks succeeded ==="
