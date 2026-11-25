#!/usr/bin/env bash
# Build a standards-compliant Debian/Ubuntu (.deb) package for openvpn3-kde-nm.
set -euo pipefail
umask 022

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PKG_NAME="openvpn3-kde-nm"
VERSION=""
DEB_REL="1"
WHEEL_PATH=""
OUTPUT_DIR="${REPO_ROOT}/dist/deb"

usage() {
    cat <<EOF
Usage: $0 [options]
  --version <ver>       Upstream version (default: read from pyproject.toml)
  --release <rel>       Debian package revision (default: 1)
  --wheel <path.whl>    Prebuilt Python wheel to unpack into dist-packages (optional)
  --output-dir <dir>    Directory for resulting .deb package (default: dist/deb)
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --version)
            VERSION="$2"
            shift 2
            ;;
        --release)
            DEB_REL="$2"
            shift 2
            ;;
        --wheel)
            WHEEL_PATH="$2"
            shift 2
            ;;
        --output-dir)
            OUTPUT_DIR="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if ! command -v dpkg-deb >/dev/null 2>&1; then
    echo "Error: dpkg-deb is required to build a .deb package." >&2
    exit 1
fi

if [ -z "$VERSION" ]; then
    VERSION="$(python3 -c '
import re, pathlib
text = pathlib.Path("'"${REPO_ROOT}/pyproject.toml"'").read_text(encoding="utf-8")
m = re.search(r"^version\s*=\s*\"([^\"]+)\"", text, re.M)
if not m:
    raise SystemExit("Could not find version in pyproject.toml")
print(m.group(1))
')"
fi

# Strip optional leading "v" and convert PEP 440 pre-releases (a/b/rc) to Debian tilde sorting
UPSTREAM_VERSION="${VERSION#v}"
DEB_UPSTREAM="$(python3 -c '
import re, sys
v = sys.argv[1]
v = re.sub(r"([0-9]+)(a|b|rc)([0-9]+)", r"\1~\2\3", v)
print(v)
' "$UPSTREAM_VERSION")"
DEB_VERSION="${DEB_UPSTREAM}-${DEB_REL}"

mkdir -p "$OUTPUT_DIR"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

STAGE="${WORK_DIR}/${PKG_NAME}_${DEB_VERSION}_all"
DIST_PKGS="${STAGE}/usr/lib/python3/dist-packages"

mkdir -p \
    "${STAGE}/DEBIAN" \
    "${DIST_PKGS}" \
    "${STAGE}/usr/bin" \
    "${STAGE}/usr/libexec" \
    "${STAGE}/usr/lib/systemd/user" \
    "${STAGE}/usr/share/${PKG_NAME}" \
    "${STAGE}/usr/share/doc/${PKG_NAME}"

# 1. Stage Python package and .dist-info metadata
if [ -n "$WHEEL_PATH" ]; then
    python3 - "$WHEEL_PATH" "$DIST_PKGS" <<'PYEOF'
import os
import pathlib
import sys
import zipfile

wheel_path = pathlib.Path(sys.argv[1]).resolve()
dest_dir = pathlib.Path(sys.argv[2]).resolve()

with zipfile.ZipFile(wheel_path, "r") as zf:
    for info in zf.infolist():
        name = info.filename
        if name.endswith("/") or "__pycache__" in name.split("/") or name.endswith(".pyc"):
            continue
        # Strip RECORD, direct_url.json, and duplicate license files; dpkg owns integrity & /usr/share/doc/*/copyright
        base = os.path.basename(name)
        if ".dist-info/" in name and (base in ("RECORD", "direct_url.json") or "/licenses/" in name):
            continue
        out_path = dest_dir / name
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(zf.read(name))

for dist_info in dest_dir.glob("*.dist-info"):
    (dist_info / "INSTALLER").write_text("dpkg\n", encoding="utf-8")
PYEOF
else
    cp -r "${REPO_ROOT}/openvpn3_kde_nm" "${DIST_PKGS}/openvpn3_kde_nm"
    find "${DIST_PKGS}/openvpn3_kde_nm" -name '__pycache__' -type d -exec rm -rf {} +
    find "${DIST_PKGS}/openvpn3_kde_nm" -name '*.pyc' -delete

    DIST_INFO="${DIST_PKGS}/openvpn3_kde_nm-${UPSTREAM_VERSION}.dist-info"
    mkdir -p "$DIST_INFO"
    cat > "${DIST_INFO}/METADATA" <<EOF
Metadata-Version: 2.4
Name: openvpn3-kde-nm
Version: ${UPSTREAM_VERSION}
Summary: Native KDE Connection Manager (plasma-nm / NetworkManager) integration for OpenVPN 3 Linux
Author-email: German Rosales <rosalesgermane@gmail.com>
License-Expression: MIT
Requires-Python: >=3.10
EOF
    cat > "${DIST_INFO}/WHEEL" <<EOF
Wheel-Version: 1.0
Generator: build-deb.sh
Root-Is-Purelib: true
Tag: py3-none-any
EOF
    printf 'dpkg\n' > "${DIST_INFO}/INSTALLER"
    printf 'openvpn3_kde_nm\n' > "${DIST_INFO}/top_level.txt"
fi

# Ensure strict 0644/0755 permissions inside dist-packages
find "${DIST_PKGS}" -type d -exec chmod 0755 {} +
find "${DIST_PKGS}" -type f -exec chmod 0644 {} +

# 2. Install system entry-point executables (using system /usr/bin/python3)
cat > "${STAGE}/usr/bin/openvpn3-kde-cli" <<'EOF'
#!/usr/bin/python3
import sys
from openvpn3_kde_nm.cli import main

if __name__ == "__main__":
    sys.exit(main())
EOF
chmod 0755 "${STAGE}/usr/bin/openvpn3-kde-cli"

cat > "${STAGE}/usr/bin/openvpn3-kde-sync" <<'EOF'
#!/usr/bin/python3
import sys
from openvpn3_kde_nm.session_watcher import main

if __name__ == "__main__":
    sys.exit(main())
EOF
chmod 0755 "${STAGE}/usr/bin/openvpn3-kde-sync"

cat > "${STAGE}/usr/libexec/nm-openvpn3-service" <<'EOF'
#!/usr/bin/python3
import sys
from openvpn3_kde_nm.nm_vpn_service import main

if __name__ == "__main__":
    sys.exit(main())
EOF
chmod 0755 "${STAGE}/usr/libexec/nm-openvpn3-service"

# 3. Install systemd user service and NetworkManager hook helper
install -m 0644 "${SCRIPT_DIR}/common/openvpn3-kde-sync.service" \
    "${STAGE}/usr/lib/systemd/user/openvpn3-kde-sync.service"
install -m 0755 "${SCRIPT_DIR}/common/nm-hook.sh" \
    "${STAGE}/usr/share/${PKG_NAME}/nm-hook.sh"
install -m 0644 "${SCRIPT_DIR}/debian/hook.conf" \
    "${STAGE}/usr/share/${PKG_NAME}/hook.conf"

# 4. Install documentation, copyright, and Debian changelog
install -m 0644 "${SCRIPT_DIR}/debian/copyright" \
    "${STAGE}/usr/share/doc/${PKG_NAME}/copyright"
gzip -9n -c "${REPO_ROOT}/README.md" > "${STAGE}/usr/share/doc/${PKG_NAME}/README.md.gz"
chmod 0644 "${STAGE}/usr/share/doc/${PKG_NAME}/README.md.gz"

CHANGELOG_DATE="$(LC_ALL=C date -Ru)"
cat > "${WORK_DIR}/changelog.Debian" <<EOF
${PKG_NAME} (${DEB_VERSION}) unstable; urgency=medium

  * Release ${UPSTREAM_VERSION}.

 -- German Rosales <rosalesgermane@gmail.com>  ${CHANGELOG_DATE}
EOF
gzip -9n -c "${WORK_DIR}/changelog.Debian" > "${STAGE}/usr/share/doc/${PKG_NAME}/changelog.Debian.gz"
chmod 0644 "${STAGE}/usr/share/doc/${PKG_NAME}/changelog.Debian.gz"

# 5. Install DEBIAN maintainer scripts, triggers, md5sums, and control
install -m 0755 "${SCRIPT_DIR}/debian/postinst" "${STAGE}/DEBIAN/postinst"
install -m 0755 "${SCRIPT_DIR}/debian/prerm" "${STAGE}/DEBIAN/prerm"
install -m 0755 "${SCRIPT_DIR}/debian/postrm" "${STAGE}/DEBIAN/postrm"
install -m 0644 "${SCRIPT_DIR}/debian/triggers" "${STAGE}/DEBIAN/triggers"

INSTALLED_SIZE="$(du -sk "${STAGE}/usr" | awk '{print $1}')"
sed \
    -e "s|@DEB_VERSION@|${DEB_VERSION}|g" \
    -e "s|@INSTALLED_SIZE@|${INSTALLED_SIZE}|g" \
    "${SCRIPT_DIR}/debian/control.in" > "${STAGE}/DEBIAN/control"
chmod 0644 "${STAGE}/DEBIAN/control"

# Ensure canonical 0755 permissions on all directories before computing md5sums and building
find "${STAGE}" -type d -exec chmod 0755 {} +

(
    cd "$STAGE"
    find usr -type f -print0 | LC_ALL=C sort -z | xargs -0 md5sum > DEBIAN/md5sums
)
chmod 0644 "${STAGE}/DEBIAN/md5sums"

# 6. Build .deb with xz compression for universal Debian/Ubuntu compatibility
OUT_DEB="${OUTPUT_DIR}/${PKG_NAME}_${DEB_VERSION}_all.deb"
dpkg-deb --root-owner-group -Zxz --build "$STAGE" "$OUT_DEB"
echo "Built Debian/Ubuntu package: ${OUT_DEB}"
