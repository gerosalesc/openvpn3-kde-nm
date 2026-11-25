#!/usr/bin/env bash
# Build an Arch Linux (.pkg.tar.zst) package, PKGBUILD, .install, and .SRCINFO.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PKG_NAME="openvpn3-kde-nm"
VERSION=""
PKGREL="1"
REPO="gerosalesc/openvpn3-kde-nm"
TAG=""
SOURCE_MODE="local"
OUTPUT_DIR="${REPO_ROOT}/dist/arch"

usage() {
    cat <<EOF
Usage: $0 [options]
  --version <ver>           Upstream version (default: read from pyproject.toml)
  --pkgrel <rel>            Arch pkgrel (default: 1)
  --repo <owner/repo>       GitHub repository slug (default: gerosalesc/openvpn3-kde-nm)
  --tag <vX.Y.Z>            Release git tag name (default: v<version>)
  --source-mode <mode>      'github' (download release tag tarball) or 'local' (git archive)
  --output-dir <dir>        Directory for resulting Arch artifacts (default: dist/arch)
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --version)
            VERSION="$2"
            shift 2
            ;;
        --pkgrel)
            PKGREL="$2"
            shift 2
            ;;
        --repo)
            REPO="$2"
            shift 2
            ;;
        --tag)
            TAG="$2"
            shift 2
            ;;
        --source-mode)
            SOURCE_MODE="$2"
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

if ! command -v makepkg >/dev/null 2>&1; then
    echo "Error: makepkg is required (run on Arch Linux or inside archlinux:latest container)." >&2
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

UPSTREAM_VERSION="${VERSION#v}"
PKGVER="${UPSTREAM_VERSION//-/_}"
if [ -z "$TAG" ]; then
    TAG="v${UPSTREAM_VERSION}"
fi

mkdir -p "$OUTPUT_DIR"
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT

TARBALL_NAME="${PKG_NAME}-${PKGVER}.tar.gz"
TARBALL_PATH="${BUILD_DIR}/${TARBALL_NAME}"

if [ "$SOURCE_MODE" = "github" ]; then
    TARBALL_URL="https://github.com/${REPO}/archive/refs/tags/${TAG}.tar.gz"
    echo "Downloading GitHub release tarball: ${TARBALL_URL}"
    for attempt in 1 2 3 4 5; do
        if curl -fsSL --retry 3 --retry-delay 2 -o "$TARBALL_PATH" "$TARBALL_URL"; then
            break
        fi
        if [ "$attempt" -eq 5 ]; then
            echo "Error: Failed to download ${TARBALL_URL}" >&2
            exit 1
        fi
        sleep 3
    done
    SOURCE_ENTRY="\${pkgname}-\${pkgver}.tar.gz::https://github.com/${REPO}/archive/refs/tags/${TAG}.tar.gz"
elif [ "$SOURCE_MODE" = "local" ]; then
    echo "Creating local source archive for ${PKG_NAME}-${PKGVER}..."
    if command -v git >/dev/null 2>&1 && git -c safe.directory="$REPO_ROOT" -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        git -c safe.directory="$REPO_ROOT" -C "$REPO_ROOT" archive --format=tar.gz --prefix="${PKG_NAME}-${PKGVER}/" HEAD > "$TARBALL_PATH"
    else
        tar -czf "$TARBALL_PATH" \
            --exclude='./.git' \
            --exclude='./dist' \
            --exclude='./build' \
            --exclude='__pycache__' \
            --exclude='*.pyc' \
            --transform="s,^\.,${PKG_NAME}-${PKGVER}," \
            -C "$REPO_ROOT" .
    fi
    SOURCE_ENTRY="\${pkgname}-\${pkgver}.tar.gz"
else
    echo "Error: --source-mode must be 'github' or 'local', got '${SOURCE_MODE}'" >&2
    exit 2
fi

SHA256SUM="$(sha256sum "$TARBALL_PATH" | awk '{print $1}')"

sed \
    -e "s|@PKGVER@|${PKGVER}|g" \
    -e "s|@PKGREL@|${PKGREL}|g" \
    -e "s|@REPO@|${REPO}|g" \
    -e "s|@SOURCE_ENTRY@|${SOURCE_ENTRY}|g" \
    -e "s|@SHA256SUM@|${SHA256SUM}|g" \
    "${SCRIPT_DIR}/arch/PKGBUILD.in" > "${BUILD_DIR}/PKGBUILD"

cp "${SCRIPT_DIR}/arch/openvpn3-kde-nm.install" "${BUILD_DIR}/openvpn3-kde-nm.install"

if [ "$(id -u)" -eq 0 ]; then
    if ! id -u builder >/dev/null 2>&1; then
        useradd -m builder
    fi
    chown -R builder:builder "$BUILD_DIR"
    su -s /bin/bash builder -c "cd '${BUILD_DIR}' && makepkg -d --noconfirm && makepkg --printsrcinfo > .SRCINFO"
else
    (
        cd "$BUILD_DIR"
        makepkg -d --noconfirm
        makepkg --printsrcinfo > .SRCINFO
    )
fi

PKG_FILE="$(find "$BUILD_DIR" -maxdepth 1 -name "${PKG_NAME}-${PKGVER}-${PKGREL}-*.pkg.tar.*" | head -n 1)"
if [ -z "$PKG_FILE" ]; then
    echo "Error: makepkg did not produce a package archive." >&2
    exit 1
fi

cp "$PKG_FILE" "${OUTPUT_DIR}/"
cp "${BUILD_DIR}/PKGBUILD" "${OUTPUT_DIR}/PKGBUILD"
cp "${BUILD_DIR}/openvpn3-kde-nm.install" "${OUTPUT_DIR}/openvpn3-kde-nm.install"

# Bundle PKGBUILD, .install, and .SRCINFO together so dotfile .SRCINFO survives GitHub Releases cleanly
tar -czf "${OUTPUT_DIR}/${PKG_NAME}-${PKGVER}-aur-src.tar.gz" \
    -C "${BUILD_DIR}" PKGBUILD openvpn3-kde-nm.install .SRCINFO

echo "Built Arch Linux artifacts in ${OUTPUT_DIR}:"
ls -la "${OUTPUT_DIR}"

if command -v namcap >/dev/null 2>&1; then
    echo "Running namcap audit..."
    namcap "${OUTPUT_DIR}/PKGBUILD" || true
    namcap "${OUTPUT_DIR}/$(basename "$PKG_FILE")" || true
fi
